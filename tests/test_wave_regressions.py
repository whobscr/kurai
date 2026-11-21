"""The paths the first revision of the overhaul broke: the set skip threshold, and external-app events by username."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from kurai.db.models import Track, User, WaveEvent
from kurai.recommend import wave


def test_is_skip_uses_the_set_share():
    assert wave.is_skip(100, 240, False, skip_share=0.5) is True   # 100 < 120
    assert wave.is_skip(100, 240, False, skip_share=0.3) is False  # 100 >= 72
    assert wave.is_skip(10, 240, True, skip_share=0.0) is True     # a 👎 is a skip however long it played


async def test_record_stores_external_app_events_by_username(db):
    user = User(username="watcher-ann", password_hash="x")
    db.add(user)
    db.add_all([Track(id="watcher-t1", title="T", artist="A"), Track(id="watcher-t2", title="U", artist="B")])
    await db.commit()
    now = datetime.now(UTC)
    queued = await wave.record(db, [("watcher-ann", "watcher-t1", True, 15, False, False, False),
                                        ("watcher-ann", "watcher-t2", True, 12, True, True, False)], now)
    events = {e.track_id: e for e in
              (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id))).scalars()}
    assert events["watcher-t1"].skipped and events["watcher-t1"].seconds == 15
    assert not events["watcher-t1"].chosen and not events["watcher-t1"].disliked
    assert events["watcher-t2"].chosen and events["watcher-t2"].disliked
    assert queued == [user.id]
    # an accidental sub-floor skip is not stored
    queued2 = await wave.record(db, [("watcher-ann", "watcher-t1", True, 5, False, False, False)], now)
    stored = (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id))).scalars().all()
    assert len(stored) == 2
    assert queued2 == []  # nothing stored -> no rebuild queued


def test_watcher_logs_a_track_once_when_it_gives_way():
    w = wave.Watcher()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    def entry(tid):
        return [{"username": "ann", "playerId": "1", "id": tid, "duration": 240, "playerName": "DSub"}]
    events = []
    for tid, sec in [("a", 0), ("a", 15), ("b", 30), ("b", 45), ("b", 60), ("c", 75)]:
        events += w.update(t0 + timedelta(seconds=sec), entry(tid), 0.5)
    assert events == [("ann", "b", True, 45)]  # one event for b — not a 15 s «skip» on every poll


async def test_skip_context_respects_taste_window(db, monkeypatch):
    """Contextual skips outside the configured taste window must not affect permanent taste."""
    from types import SimpleNamespace

    from kurai.recommend import library

    user = User(username="skip-window-user", password_hash="x")
    db.add(user)
    db.add_all([
        Track(id="old-skip", title="Old", artist="Artist A"),
        Track(id="recent-skip", title="Recent", artist="Artist B"),
    ])
    await db.flush()

    now = datetime.now(UTC)
    since = now - timedelta(days=30)

    db.add_all([
        WaveEvent(
            user_id=user.id,
            track_id="old-skip",
            skipped=True,
            seconds=10,
            chosen=False,
            disliked=False,
            not_now=False,
            at=since - timedelta(days=1),
        ),
        WaveEvent(
            user_id=user.id,
            track_id="recent-skip",
            skipped=True,
            seconds=10,
            chosen=False,
            disliked=False,
            not_now=False,
            at=since + timedelta(days=1),
        ),
    ])
    await db.commit()

    async def fake_track_vectors(db, ids):
        return {}

    def fake_session_mood(events, moods, half_life, at):
        return SimpleNamespace(vector=None, regime="ok")

    monkeypatch.setattr(library.moodmod, "track_vectors", fake_track_vectors)
    monkeypatch.setattr(library.moodmod, "session_mood", fake_session_mood)
    monkeypatch.setattr(library, "skip_weight", lambda *args: -1.0)

    negatives, chosen = await library._skip_context(
        db,
        user,
        {
            "mix.wave.mood": True,
            "mix.wave.session_hours": 3,
            "mix.wave.session_half_life": 45,
        },
        now,
        half_life=30,
        floor=0.3,
        since=since,
    )

    assert negatives == {"recent-skip": -1.0}
    assert "old-skip" not in negatives
    assert chosen == set()
