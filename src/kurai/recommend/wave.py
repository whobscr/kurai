"""«Моя волна»: one playlist per user that follows what they're listening to right now.

The worker watches Navidrome's «now playing» (every music app reports the track it starts), so Kurai sees within
seconds which track was played on and which was skipped; Kurai's web player tells it directly. After a skip, or a few
tracks played, the wave is rebuilt as a job: near what was just liked and near the taste of all time, away from what
was skipped, without anything heard today or the artists skipped twice. It's the same playlist in Navidrome every
time: an app plays its own copy of a playlist, so the new version is heard when the wave is opened again (Kurai's web
player picks it up by itself)."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import db as database
from kurai import settings
from kurai.db.models import Listen, Playlist, Taste, Track, User, WaveEvent
from kurai.i18n import _
from kurai.integrations.navidrome import WEB_PLAYER
from kurai.jobs import queue
from kurai.recommend import library, playlists, retrieval, start
from kurai.recommend.vectors import mmr_select, order_smoothly, taste_vector

log = logging.getLogger(__name__)

POLL = 15  # seconds between looks at «now playing»
SKIP_SHARE = 0.5  # a track played for less than this share of it counts as skipped
DEFAULT_DURATION = 240
REBUILD_AFTER_PLAYS = 3  # or after one skip
MIN_GAP = timedelta(minutes=4)  # between two versions
SESSION = timedelta(hours=3)  # what's «just now»
SESSION_HALF_LIFE = 45  # minutes: the last tracks count most
HEARD = timedelta(hours=24)  # left out of the next version
KEEP_EVENTS = timedelta(days=30)
SIZE = 40
POPULAR_EVERY = 3  # a listener who hasn't played anything yet: every third track a popular one
NAME = _("Моя волна")


# ---------- watching ----------


@dataclass
class Playing:
    track_id: str
    duration: int
    since: datetime | None  # None: it was already playing when the watching began


class Watcher:
    """Turns the looks at «now playing» into tracks played on or skipped, per user and app. Pure: the time is given."""

    def __init__(self) -> None:
        self.playing: dict[tuple[str, str], Playing] = {}
        self.started = False

    def update(self, now: datetime, entries: list[dict]) -> list[tuple[str, str, bool, int]]:
        """(username, track id, skipped, seconds played) of each track that has just given way to another."""
        events = []
        # Kurai's own web player reports its events itself (web/routes/player.py)
        current = {(e.get("username", ""), str(e.get("playerId", ""))): e for e in entries
                   if e.get("id") and e.get("playerName") != WEB_PLAYER}
        for key, entry in current.items():
            before = self.playing.get(key)
            if before is not None and before.track_id == entry["id"]:
                continue
            if before is not None and before.since is not None:
                seconds = int((now - before.since).total_seconds())
                skipped = seconds < SKIP_SHARE * (before.duration or DEFAULT_DURATION)
                events.append((key[0], before.track_id, skipped, seconds))
            self.playing[key] = Playing(entry["id"], int(entry.get("duration") or 0), now if self.started else None)
        # an app that stopped: how its last track ended is unknown (a full play reaches the taste through Navidrome)
        for key in set(self.playing) - set(current):
            del self.playing[key]
        self.started = True
        return events


async def record(db: AsyncSession, events: list[tuple[str, str, bool, int]], now: datetime) -> list[int]:
    """Stores the events of Kurai's users and known tracks; queues a new wave for whoever is due. The users queued."""
    if not events:
        return []
    users = {u.username: u for u in await library.active_users(db)}
    known = set((await db.execute(select(Track.id).where(Track.id.in_([e[1] for e in events])))).scalars())
    touched: dict[int, User] = {}
    for username, track_id, skipped, seconds in events:
        user = users.get(username)
        if user is None or track_id not in known:
            continue
        db.add(WaveEvent(user_id=user.id, track_id=track_id, skipped=skipped, seconds=seconds, at=now))
        touched[user.id] = user
    await db.commit()
    queued = []
    for user in touched.values():
        if await due(db, user, now):
            await queue.enqueue(db, "wave", user_id=user.id, priority=20, dedupe_key=f"wave:{user.id}")
            queued.append(user.id)
    return queued


async def due(db: AsyncSession, user: User, now: datetime) -> bool:
    """A new version after a skip or a few tracks played since the last one, but not more often than MIN_GAP."""
    wave = await _playlist(db, user)
    if wave is None:
        return True
    if now - wave.created_at < MIN_GAP:
        return False
    since = (await db.execute(select(WaveEvent.skipped).where(WaveEvent.user_id == user.id,
                                                              WaveEvent.at > wave.created_at))).scalars().all()
    return any(since) or len(since) >= REBUILD_AFTER_PLAYS


async def watch() -> None:
    """The worker's watching loop (while «Моя волна» is on)."""
    watcher = Watcher()
    last_error = ""
    while True:
        try:
            async with database.session() as db:
                if await settings.get(db, "wave.enabled"):
                    entries = await library.navidrome().now_playing(await library.admin_account(db))
                    now = datetime.now(UTC)
                    await record(db, watcher.update(now, entries), now)
            last_error = ""
        except Exception as e:  # Navidrome restarting: the next look tries again (said once, not every 15 s)
            if str(e) != last_error:
                log.warning("«now playing» not read: %s", e)
                last_error = str(e)
        await asyncio.sleep(POLL)


# ---------- building ----------


async def _playlist(db: AsyncSession, user: User) -> Playlist | None:
    return (await db.execute(select(Playlist).where(Playlist.user_id == user.id, Playlist.kind == "wave")
                             .order_by(Playlist.id.desc()))).scalars().first()


async def recently_heard(db: AsyncSession, user: User, now: datetime) -> set[str]:
    """The tracks the user played or skipped within HEARD, in Kurai's player or any app (its last plays): the wave
    leaves them out, so it doesn't repeat itself."""
    events = select(WaveEvent.track_id).where(WaveEvent.user_id == user.id, WaveEvent.at >= now - HEARD)
    played = select(Listen.track_id).where(Listen.user_id == user.id, Listen.last_played >= now - HEARD)
    return set((await db.execute(events.union(played))).scalars())


async def build(db: AsyncSession, user: User) -> dict:
    """The next version of the user's wave, written into Navidrome over the last one. A listener who hasn't played
    anything yet gets what their answers point at with the library's popular tracks between (start.py); without
    answers either, the popular tracks alone."""
    now = datetime.now(UTC)
    j = await retrieval.joint(db, await retrieval.space_weights(db, "mix"))
    taste = await playlists.taste_of(db, user)
    events = list((await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id,
                                                            WaveEvent.at >= now - HEARD)
                                    .order_by(WaveEvent.at))).scalars())
    session = [e for e in events if e.at >= now - SESSION]
    vectors = await retrieval.track_vectors(db, {e.track_id for e in session})
    queries = {}
    for space in j.weights:
        found, weights = [], []
        for e in session:
            raw = vectors.get(e.track_id, ("", {}))[1].get(space)
            if raw is None:
                continue
            fade = 0.5 ** ((now - e.at).total_seconds() / 60 / SESSION_HALF_LIFE)
            found.append(np.asarray(raw, dtype=np.float32))
            weights.append(-fade if e.skipped else fade)
        just_now = taste_vector(found, weights) if found else None
        queries[space] = playlists.mix_query(j, space, [(just_now, 0.6), (taste.get(space), 0.4)])
    heard = await recently_heard(db, user, now)
    taste_row = await db.get(Taste, user.id)
    new = (taste_row is None or not taste_row.tracks_used) and not session
    if not any(q is not None for q in queries.values()):
        ids = await start.popular(db, SIZE, heard)
        if not ids:
            return {"skipped": _("в библиотеке пока нет треков")}
        return await _save(db, user, ids, now, {"tracks": len(ids), "popular": len(ids)})
    skips: dict[str, int] = {}
    for e in session:
        if e.skipped and (artist := vectors.get(e.track_id, ("", {}))[0]):
            skips[artist] = skips.get(artist, 0) + 1
    pool = [c for c in await retrieval.candidates(db, j, queries, user, exclude_recent=True,
                                                  exclude_artists={a for a, n in skips.items() if n >= 2})
            if c.id not in heard]
    if not pool:
        return {"skipped": _("нет подходящих треков")}
    ids = [c.id for c in order_smoothly(mmr_select(pool, SIZE))]
    popular = 0
    if new:  # nothing played yet: every third track one of the library's popular ones
        hits = await start.popular(db, SIZE // POPULAR_EVERY, heard | set(ids))
        ids, popular = _between(ids, hits, POPULAR_EVERY), len(hits)
        ids = ids[:SIZE]
    return await _save(db, user, ids, now, {"tracks": len(ids), "played": sum(not e.skipped for e in session),
                                            "skips": sum(e.skipped for e in session), "popular": popular})


def _between(ids: list[str], extra: list[str], every: int) -> list[str]:
    """`extra` put into `ids` as every `every`-th track."""
    out, rest = [], list(extra)
    for i, track_id in enumerate(ids, 1):
        out.append(track_id)
        if i % (every - 1) == 0 and rest:
            out.append(rest.pop(0))
    return out + rest


async def _save(db: AsyncSession, user: User, ids: list[str], now: datetime, result: dict) -> dict:
    wave = await _playlist(db, user) or Playlist(user_id=user.id, kind="wave", name=NAME)
    wave.track_ids = ids
    wave.description = _("Обновлено в {t} — по тому, что вы слушаете").format(t=now.astimezone().strftime("%H:%M"))
    wave.created_at = now
    db.add(wave)
    await playlists.write(db, user, wave)
    await db.execute(delete(WaveEvent).where(WaveEvent.user_id == user.id, WaveEvent.at < now - KEEP_EVENTS))
    await db.commit()
    return result
