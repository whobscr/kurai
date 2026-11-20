"""The paths the first revision of the overhaul broke: the set skip threshold, and external-app events by username."""

from datetime import UTC, datetime

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
    queued = await wave.record(db, [("watcher-ann", "watcher-t1", True, 15, False, False),
                                    ("watcher-ann", "watcher-t2", True, 5, True, True)], now)
    events = {e.track_id: e for e in
              (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id))).scalars()}
    assert events["watcher-t1"].skipped and events["watcher-t1"].seconds == 15
    assert not events["watcher-t1"].chosen and not events["watcher-t1"].disliked
    assert events["watcher-t2"].chosen and events["watcher-t2"].disliked
    assert queued == [user.id]  # no wave playlist yet: a rebuild is due
