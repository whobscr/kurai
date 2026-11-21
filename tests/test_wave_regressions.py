"""The paths the first revision of the overhaul broke: the set skip threshold, and external-app events by username."""

from datetime import UTC, datetime

from sqlalchemy import select

from datetime import timedelta
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
