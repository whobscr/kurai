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
    db.add(Track(id="watcher-t1", title="T", artist="A"))
    await db.commit()
    now = datetime.now(UTC)
    queued = await wave.record(db, [("watcher-ann", "watcher-t1", True, 15, False, False)], now)
    event = (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id))).scalar_one()
    assert event.skipped and event.seconds == 15 and not event.chosen and not event.disliked
    assert queued == [user.id]  # no wave playlist yet: a rebuild is due