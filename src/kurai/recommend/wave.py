"""«Моя волна»: one playlist per user that follows what they're listening to right now.
The worker watches Navidrome's «now playing» (every music app reports the track it starts), so Kurai sees within
seconds which track was played on and which was skipped; Kurai's web player tells it directly. After a skip, or a few
tracks played, the wave is rebuilt as a job: near what was just liked and near the taste of all time, away from what
was skipped, without anything heard today or the artists skipped twice. It's the same playlist in Navidrome every
time: an app plays its own copy of a playlist, so the new version is heard when the wave is opened again (Kurai's web
player picks it up by itself)."""

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import db as database
from kurai import settings
from kurai.artists import artist_key
from kurai.db.models import Listen, Playlist, Taste, Track, User, WaveEvent
from kurai.i18n import _
from kurai.integrations.navidrome import WEB_PLAYER
from kurai.jobs import queue
from kurai.recommend import library, mixing, playlists, related, retrieval, start
from kurai.recommend import mood as moodmod
from kurai.recommend.vectors import Candidate, arrange, mean_unit

log = logging.getLogger(__name__)

POLL = 15
DEFAULT_DURATION = 240
# a skip is at most this many seconds regardless of the track's length (very early skip = accidental)
SKIP_FLOOR_SECONDS = 10
# a track played this long (and at least a quarter of its length) counts as completed, not skipped
SKIP_CAP_SECONDS = 180
SKIP_CAP_FRACTION = 0.25
KEEP_EVENTS = timedelta(days=30)
NAME = _("Моя волна")
ANCHORS = 4
ANCHOR_LIKENESS = 0.5


# ---------- watching ----------


@dataclass
class Playing:
    track_id: str
    duration: int
    since: datetime | None


class Watcher:
    """Turns the looks at «now playing» into tracks played on or skipped, per user and app. Pure: the time is given."""

    def __init__(self) -> None:
        self.playing: dict[tuple[str, str], Playing] = {}
        self.started = False

    def update(self, now: datetime, entries: list[dict],
               skip_share: float | None = None) -> list[tuple[str, str, bool, int]]:
        """(username, track id, skipped, seconds played) of each track that has just given way to another.
        The skip test uses the shared `is_skip` (absolute floor and cap, not just a fraction)."""
        events = []
        current = {(e.get("username", ""), str(e.get("playerId", ""))): e for e in entries
                   if e.get("id") and e.get("playerName") != WEB_PLAYER}
        for key, entry in current.items():
            before = self.playing.get(key)
            if before is not None and before.track_id == entry["id"]:
                continue
            if before is not None and before.since is not None:
                seconds = int((now - before.since).total_seconds())
                events.append((key[0], before.track_id, is_skip(seconds, before.duration, False, skip_share), seconds))
            self.playing[key] = Playing(entry["id"], int(entry.get("duration") or 0), now if self.started else None)
        for key in set(self.playing) - set(current):
            del self.playing[key]
        self.started = True
        return events


def is_skip(seconds: int, duration: int | None, disliked: bool, skip_share: float | None = None) -> bool:
    """Whether `seconds` of playing counts as a skip. Shared by the web player and the Watcher so they agree.
    A 👎 is always a skip. Otherwise: a skip below the set fraction of the length («Порог пропуска», `skip_share`;
    None: the standard wave's) — unless it played at least SKIP_CAP_SECONDS and a quarter of its length, which counts
    as completed even when the relative fraction is low (long ambient tracks)."""
    if disliked:
        return True
    duration = duration or DEFAULT_DURATION
    if seconds >= SKIP_FLOOR_SECONDS and seconds >= SKIP_CAP_SECONDS and seconds >= SKIP_CAP_FRACTION * duration:
        return False
    share = mixing.standard("wave").skip_share if skip_share is None else skip_share
    return seconds < share * duration


async def record(db: AsyncSession,
                 events: list[tuple[int | str, str, bool, int, bool, bool]],
                 now: datetime) -> list[int]:
    """Stores the events of Kurai's users and known tracks; queues a new wave for whoever is due. The users queued.
    Each event is (user_id or username, track_id, skipped, seconds, chosen, disliked). `chosen`: the user picked
    the track themselves (a library-search set). `disliked`: an explicit 👎 regardless of how long it played."""
    if not events:
        return []
    users_by_name = {u.username: u for u in await library.active_users(db)}
    known = set((await db.execute(select(Track.id).where(
        Track.id.in_([e[1] for e in events])))).scalars())
    touched: dict[int, User] = {}
    for who, track_id, skipped, seconds, chosen, disliked in events:
        if isinstance(who, int):
            user = await db.get(User, who)
        else:
            user = users_by_name.get(who)
        if user is None or track_id not in known:
            continue
        db.add(WaveEvent(user_id=user.id, track_id=track_id, skipped=skipped, seconds=seconds,
                         chosen=chosen, disliked=disliked, at=now))
        touched[user.id] = user
    await db.commit()
    queued = []
    for user in touched.values():
        if await due(db, user, now):
            await queue.enqueue(db, "wave", user_id=user.id, priority=20, dedupe_key=f"wave:{user.id}")
            queued.append(user.id)
    return queued


async def due(db: AsyncSession, user: User, now: datetime) -> bool:
    """A new version after a skip or a few tracks played since the last one, but not more often than set."""
    wave = await _playlist(db, user)
    if wave is None:
        return True
    mix = await mixing.load(db, "wave")
    if now - wave.created_at < timedelta(minutes=mix.rebuild_minutes):
        return False
    since = (await db.execute(select(WaveEvent.skipped).where(WaveEvent.user_id == user.id,
                                                              WaveEvent.at > wave.created_at))).scalars().all()
    return any(since) or len(since) >= mix.rebuild_plays


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
                    mix = await mixing.load(db, "wave")
                    # the watcher can't tell chosen/disliked: those flags come from the web player's event()
                    await record(db, [(uname, tid, sk, sec, False, False)
                                      for uname, tid, sk, sec in watcher.update(now, entries, mix.skip_share)], now)
                    last_error = ""
        except Exception as e:
            if str(e) != last_error:
                log.warning("«now playing» not read: %s", e)
                last_error = str(e)
        await asyncio.sleep(POLL)


# ---------- building ----------


async def _playlist(db: AsyncSession, user: User) -> Playlist | None:
    return (await db.execute(select(Playlist).where(Playlist.user_id == user.id, Playlist.kind == "wave")
                             .order_by(Playlist.id.desc()))).scalars().first()


async def recently_heard(db: AsyncSession, user: User, now: datetime, days: float) -> set[str]:
    """The tracks the user played or skipped within `days`, in Kurai's player or any app: the wave leaves them out
    («Пауза для трека»), so it doesn't repeat itself."""
    since = now - timedelta(days=days)
    events = select(WaveEvent.track_id).where(WaveEvent.user_id == user.id, WaveEvent.at >= since)
    played = select(Listen.track_id).where(Listen.user_id == user.id, Listen.last_played >= since)
    return set((await db.execute(events.union(played))).scalars())


async def _track_moods(db: AsyncSession, ids: set[str]) -> dict[str, np.ndarray]:
    """Mood vectors of the given tracks from their audio tags; tracks without analysis are simply absent."""
    from kurai.db.models import TrackAudio
    rows = (await db.execute(select(TrackAudio.track_id, TrackAudio.moods, TrackAudio.danceability,
                                     TrackAudio.voice).where(TrackAudio.track_id.in_(list(ids)),
                                                             TrackAudio.error.is_(None)))).all()
    out: dict[str, np.ndarray] = {}
    for tid, moods, dance, voice in rows:
        if (v := moodmod.mood_of_tags(moods, dance, voice)) is not None:
            out[tid] = v
    return out


@dataclass
class Plan:
    """A version of the wave, before it's saved: its tracks (each with the part of the mix it came for) and what the
    job reports; `tracks` empty with `result["skipped"]`: nothing to make it of."""
    tracks: list[Candidate]
    result: dict


def _popular(ids: list[str]) -> list[Candidate]:
    return [Candidate(i, "", np.zeros(1, dtype=np.float32), 0.0, "popular") for i in ids]


async def plan(db: AsyncSession, user: User, mix: mixing.Mix | None = None, now: datetime | None = None,
               rng: random.Random | None = None) -> Plan:
    """The next version of the user's wave: «now_weight» of it near what was just played («сейчас»), the rest near the
    taste of all time («всегда»). Each kind gets its share of the tracks by its own queries (one query between them
    would give everything to whichever is a little closer): «сейчас» is up to ANCHORS tracks played lately, each
    with a part by how fresh it is («Затухание «сейчас»»). Tracks like the skipped ones are lowered («Отталкивание от
    пропущенного»); in each part, new, known and «слушают вместе» tracks in the set shares; nothing heard within the
    track's pause, the resting artists left out, the ones skipped again and again too. A listener who hasn't played
    anything yet gets what their answers point at with the library's popular tracks between (start.py); without
    answers either, the popular tracks alone. Nothing is saved (the admins' preview shows it as it is).
    The session's mood is computed from the accepted tracks and always reported in `result` (shadow); it steers the
    ranking only when «Учитывать настроение» (`mix.mood`) is on, by a capped bonus, never a hard filter."""
    mix = mix or await mixing.load(db, "wave")
    now = now or datetime.now(UTC)
    j = await retrieval.joint(db, await retrieval.space_weights(db, "mix"))
    taste = await playlists.taste_of(db, user)
    session_since = now - timedelta(hours=mix.session_hours)
    session = list((await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id,
                                                             WaveEvent.at >= session_since)
                                     .order_by(WaveEvent.at))).scalars())
    vectors = await retrieval.track_vectors(db, {e.track_id for e in session})
    fresh: dict[str, float] = {}
    away = []
    for e in session:
        raw = vectors.get(e.track_id, ("", {}))[1]
        if not any(raw.get(space) is not None for space in j.weights):
            continue
        if e.skipped:
            away.append(j.item(raw))
        else:
            fresh[e.track_id] = 0.5 ** ((now - e.at).total_seconds() / 60 / mix.session_half_life)
    anchors = _anchors(j, {tid: (vectors[tid][1], f) for tid, f in fresh.items()})
    taste_queries = {space: playlists.mix_query(j, space, [(taste.get(space), 1.0)]) for space in j.weights}
    has_taste = any(q is not None for q in taste_queries.values())
    heard = await recently_heard(db, user, now, mix.track_rest_days)
    taste_row = await db.get(Taste, user.id)
    new_listener = (taste_row is None or not taste_row.tracks_used) and not session
    if not anchors and not has_taste:
        ids = await start.popular(db, mix.size, heard)
        if not ids:
            return Plan([], {"skipped": _("в библиотеке пока нет треков")})
        return Plan(_popular(ids), {"tracks": len(ids), "popular": len(ids)})
    skips: dict[str, int] = {}
    for e in session:
        if e.skipped and (artist := artist_key(vectors.get(e.track_id, ("", {}))[0])):
            skips[artist] = skips.get(artist, 0) + 1
    banned = {a for a, n in skips.items() if mix.artist_skip_ban and n >= mix.artist_skip_ban}
    neighbours = related.artists_for(taste_row, mix.related_depth) if mix.related_share else {}
    resting = await mixing.resting_artists(db, user, now, mix.artist_rest_hours, mix.artist_rest_shape)
    places = mix.size // mix.popular_every if new_listener and mix.popular_every >= 2 else 0
    size = mix.size - places
    now_count = round(size * (mix.now_weight if has_taste else 1.0)) if anchors else 0
    parts = [({space: playlists.mix_query(j, space, [(raw.get(space), 1.0)]) for space in j.weights}, count)
             for (raw, _f), count in zip(anchors, _apportion(now_count, [f for _r, f in anchors]), strict=True)]
    parts.append((taste_queries, size - now_count))
    # the session's mood, before the parts are chosen: the ranking bonus needs it, the shadow only reports it
    mood_on = bool(getattr(mix, "mood", False))
    session_moods = await _track_moods(db, {e.track_id for e in session})
    sm = moodmod.session_mood(session, session_moods, mix.session_half_life, now)
    shadow = moodmod.shadow_correlation(session, session_moods, mix.session_half_life, now)
    chosen, found_any = [], False
    for queries, count in parts:
        if count <= 0 or not any(q is not None for q in queries.values()):
            continue
        pool = [c for c in await retrieval.candidates(db, j, queries, user, rest_days=mix.track_rest_days,
                                                      exclude_artists=banned) if c.id not in heard]
        near = [c for c in await retrieval.candidates(db, j, queries, user, rest_days=mix.track_rest_days,
                                                      exclude_artists=banned, only_artists=list(neighbours))
                if c.id not in heard] if neighbours else []
        found_any = found_any or bool(pool or near)
        known = await retrieval.played(db, user, {c.id for c in pool + near})
        track_moods = await _track_moods(db, {c.id for c in pool + near}) if mood_on else {}
        chosen = mixing.choose(pool, near, known, mix, count, resting, rng, chosen, away,
                               mood=sm if mood_on else None,
                               mood_strength=getattr(mix, "mood_strength", 0.0),
                               track_moods=track_moods)
    if not found_any:
        return Plan([], {"skipped": _("нет подходящих треков")})
    chosen = arrange(chosen, mix.smooth, mix.artist_gap)
    popular = 0
    if places:
        hits = await start.popular(db, places, heard | {c.id for c in chosen})
        chosen, popular = _between(chosen, _popular(hits), mix.popular_every), len(hits)
    kinds = {k: sum(c.kind == k for c in chosen) for k in ("new", "known", "related")}
    result = {"tracks": len(chosen),
              "played": sum(not e.skipped for e in session),
              "skips": sum(e.skipped for e in session),
              "chosen_plays": sum(not e.skipped and e.chosen for e in session),
              "disliked": sum(e.disliked for e in session),
              "popular": popular,
              # shadow mood: always reported; used for ranking only when mix.mood is on
              "mood": sm.vector.tolist() if sm.vector is not None else None,
              "mood_confidence": round(sm.confidence, 3),
              "mood_regime": sm.regime,
              "mood_shadow": shadow,
              "mood_enabled": mood_on,
              **kinds}
    return Plan(chosen, result)



async def build(db: AsyncSession, user: User) -> dict:
    """The next version of the user's wave, written into Navidrome over the last one."""
    now = datetime.now(UTC)
    made = await plan(db, user, now=now)
    if not made.tracks:
        return made.result
    return await _save(db, user, [c.id for c in made.tracks], now, made.result)


def _anchors(j, played: dict[str, tuple[dict, float]]) -> list[tuple[dict, float]]:
    """The directions of what was played lately: tracks that sound alike (ANCHOR_LIKENESS) grouped, the freshest
    first, at most ANCHORS (a track beyond joins the nearest). Each: its tracks' mean raw vector per space, and their
    freshness summed (its part of «сейчас»)."""
    groups: list[tuple[np.ndarray, list[dict], float]] = []
    for raw, f in sorted(played.values(), key=lambda p: -p[1]):
        v = j.item(raw)
        likeness = [float(np.dot(v, g[0])) for g in groups]
        best = int(np.argmax(likeness)) if groups else -1
        if groups and (likeness[best] >= ANCHOR_LIKENESS or len(groups) >= ANCHORS):
            first, raws, total = groups[best]
            groups[best] = (first, raws + [raw], total + f)
        else:
            groups.append((v, [raw], f))
    return [({space: mean_unit([r[space] for r in raws if r.get(space) is not None]) for space in j.weights}, f)
            for _v, raws, f in groups]


def _apportion(total: int, weights: list[float]) -> list[int]:
    """`total` split by `weights` in whole numbers (the largest remainders round up)."""
    if not weights or total <= 0:
        return [0] * len(weights)
    exact = [total * w / sum(weights) for w in weights]
    out = [int(x) for x in exact]
    for i in sorted(range(len(exact)), key=lambda i: out[i] - exact[i])[:total - sum(out)]:
        out[i] += 1
    return out


def _between(items: list, extra: list, every: int) -> list:
    """`extra` put into `items` as every `every`-th one."""
    out, rest = [], list(extra)
    for i, item in enumerate(items, 1):
        out.append(item)
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