"""The web player: what to play (a playlist's tracks, «Моя волна» as it is now), the music and covers from Navidrome as
the user, and what the listener did with each track.

A finished track is a play in Navidrome (scrobbled, so the tastes see it like a play in any app); a played or skipped
track is also an event of «Моя волна» at once (the worker's watching of «now playing» is for the other apps), so a skip
in the browser rebuilds the wave within moments and the player picks up the new version."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select

from kurai import settings
from kurai.db.models import Job, Listen, Playlist, Track
from kurai.i18n import _
from kurai.integrations.navidrome import WEB_PLAYER, Account, NavidromeError
from kurai.jobs import queue as jobs
from kurai.recommend import library, mixing, playlists, search, wave
from kurai.web import upstream
from kurai.web.deps import Ctx, user_required
from kurai.web.routes.misc import MEDIA_HEADERS

router = APIRouter(prefix="/player")
HISTORY = 100  # the tracks of «История» the player plays on through
RATINGS = ("like", "dislike", "none")  # ♥, 👎, neither
SAVED_MAX = 2000  # tracks in a saved player's list
NAME_MAX = 100
log = logging.getLogger(__name__)
COVER_SIZE = "600"


def _account(ctx: Ctx) -> Account:
    account = library.user_account(ctx.user)
    if account is None:
        raise HTTPException(409, _("У вашей учётной записи ещё нет доступа к музыке (Navidrome) — подождите минуту."))
    return account


def track_item(t: Track, listen: Listen | None) -> dict:
    """A track as the player needs it, with the user's ♥ and 👎 (a rating of 1–2)."""
    return {"id": t.id, "title": t.title, "artist": t.artist, "album": t.album, "duration": t.duration,
            "liked": bool(listen and listen.starred), "disliked": bool(listen and listen.rating in (1, 2))}


async def _tracks(ctx: Ctx, ids: list[str]) -> list[dict]:
    """The playlist's tracks Kurai knows, in its order."""
    rows = {t.id: (t, listen) for t, listen in (await ctx.db.execute(
        select(Track, Listen).outerjoin(Listen, (Listen.track_id == Track.id) & (Listen.user_id == ctx.user.id))
        .where(Track.id.in_(ids)))).all()}
    return [track_item(*rows[i]) for i in ids if i in rows]


async def _wave_state(ctx: Ctx) -> dict:
    """No wave yet: one is being made (a job queued now if none is), or why there's none."""
    if not await settings.get(ctx.db, "wave.enabled"):
        return {"message": _("«Моя волна» выключена администратором.")}
    last = (await ctx.db.execute(select(Job).where(Job.user_id == ctx.user.id, Job.kind == "wave")
                                 .order_by(Job.id.desc()))).scalars().first()
    if last is not None and last.status in ("queued", "running"):
        return {"pending": True}
    if last is not None and last.status == "done" and (last.result or {}).get("skipped"):
        return {"message": _("Волну пока не из чего собрать: {why}. Послушайте любой микс — и она появится.").format(
            why=last.result["skipped"])}
    await jobs.enqueue(ctx.db, "wave", user_id=ctx.user.id, priority=20, dedupe_key=f"wave:{ctx.user.id}")
    return {"pending": True}


async def _unheard_wave(ctx: Ctx, playlist: Playlist) -> list[str] | None:
    """The wave's tracks without what the user heard within a track's pause («Волна»), so coming back to it never
    starts with a track just played. Less than half left: a fresh version is made (it comes as a new version); nothing
    left: None (wait for it), unless a version made since found nothing new either (a small library): then it plays
    as it is."""
    mix = await mixing.load(ctx.db, "wave")
    heard = await wave.recently_heard(ctx.db, ctx.user, datetime.now(UTC), mix.track_rest_days)
    ids = [i for i in playlist.track_ids if i not in heard]
    if len(ids) >= mix.size // 2:
        return ids
    last = (await ctx.db.execute(select(Job).where(Job.user_id == ctx.user.id, Job.kind == "wave")
                                 .order_by(Job.id.desc()))).scalars().first()
    rebuilt_in_vain = (last is not None and last.status in ("done", "failed") and last.finished_at is not None
                       and last.finished_at > playlist.created_at)
    if rebuilt_in_vain:
        return ids or playlist.track_ids
    if last is None or last.status not in ("queued", "running"):
        await jobs.enqueue(ctx.db, "wave", user_id=ctx.user.id, priority=20, dedupe_key=f"wave:{ctx.user.id}")
    return ids or None


@router.get("/queue/{which:path}")
async def queue(which: str, ctx: Ctx = Depends(user_required)):
    """«wave» (the user's wave as it is now), «history» (what they played last), a playlist's id, or a set the user
    chose in the library search («artist:…», «album:…», «track:…», kind «set»: the player goes on into the wave after
    it): its tracks, and its version (the player compares it to learn that the wave was rebuilt)."""
    if which.partition(":")[0] in search.KINDS:
        name, ids = await search.tracks_of(ctx.db, which)
        if not ids:
            raise HTTPException(404)
        return {"id": which, "kind": "set", "name": name, "description": "", "version": "",
                "tracks": await _tracks(ctx, ids)}
    if which == "history":
        rows = await library.recent_listens(ctx.db, ctx.user, HISTORY)
        return {"id": "history", "kind": "history", "name": _("История"), "description": "", "version": "",
                "tracks": [track_item(t, listen) for t, listen in rows]}
    if which == "wave":
        playlist = (await ctx.db.execute(select(Playlist).where(Playlist.user_id == ctx.user.id,
                                                                Playlist.kind == "wave")
                                         .order_by(Playlist.id.desc()))).scalars().first()
        if playlist is None or not playlist.track_ids:
            return await _wave_state(ctx)
        ids = await _unheard_wave(ctx, playlist)
        if ids is None:
            return {"pending": True}
        # «Между треками одного исполнителя»: the player keeps it from what it just played to the new version
        return {"id": playlist.id, "kind": playlist.kind, "name": playlist.name,
                "description": playlist.description, "version": playlist.created_at.isoformat(),
                "artist_gap": (await mixing.load(ctx.db, "wave")).artist_gap, "tracks": await _tracks(ctx, ids)}
    else:
        playlist = await ctx.db.get(Playlist, int(which)) if which.isdigit() else None
        if playlist is None or playlist.user_id != ctx.user.id:
            raise HTTPException(404)
    return {"id": playlist.id, "kind": playlist.kind, "name": playlist.name, "description": playlist.description,
            "version": playlist.created_at.isoformat(), "tracks": await _tracks(ctx, playlist.track_ids)}


@router.get("/stream/{track_id}")
async def stream(track_id: str, request: Request, ctx: Ctx = Depends(user_required)):
    """The track's file as Navidrome gives it to the user, with the browser's Range passed on (seeking)."""
    nd = library.navidrome()
    url, params = nd.signed(_account(ctx), "stream", [("id", track_id)], client=WEB_PLAYER)
    return await upstream.relay("GET", url, MEDIA_HEADERS, "Navidrome", params=params,
                                headers=upstream.keep("range")(request.headers))


@router.get("/cover/{track_id}")
async def cover(track_id: str, ctx: Ctx = Depends(user_required)):
    url, params = library.navidrome().signed(_account(ctx), "getCoverArt",
                                             [("id", f"mf-{track_id}"), ("size", COVER_SIZE)], client=WEB_PLAYER)
    response = await upstream.relay("GET", url, MEDIA_HEADERS, "Navidrome", read_timeout=60, params=params)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@router.post("/event")
async def event(track_id: str = Body(), seconds: int = Body(), disliked: bool = Body(False),
                chosen: bool = Body(False), ctx: Ctx = Depends(user_required)):
    """A track gave way to the next one (or the player stopped) after `seconds` of playing: played on or skipped
    (a 👎 is a skip however long it played). A play counts in Navidrome and in «История» at once. A track the user
    chose themselves (`chosen`: the library search) skipped without a 👎 says nothing of their taste: not counted."""
    track = await ctx.db.get(Track, track_id)
    if track is None:
        raise HTTPException(404)
    now = datetime.now(UTC)
    seconds = max(0, seconds)
    skip_share = (await mixing.load(ctx.db, "wave")).skip_share
    skipped = disliked or seconds < skip_share * (track.duration or wave.DEFAULT_DURATION)
    if skipped and chosen and not disliked:
        return {"skipped": True, "rebuilding": False}
    if not skipped:
        await library.note_listen(ctx.db, ctx.user, track_id, played_at=now)
        try:
            await library.navidrome().scrobble(_account(ctx), track_id, int(now.timestamp() * 1000))
        except NavidromeError as e:  # the play still counts for the wave and the history
            log.warning("scrobble: %s", e)
    rebuilding = False
    if await settings.get(ctx.db, "wave.enabled"):
        rebuilding = bool(await wave.record(ctx.db, [(ctx.user.username, track_id, skipped, seconds)], now))
    return {"skipped": skipped, "rebuilding": rebuilding}


@router.post("/rate")
async def rate(track_id: str = Body(), value: str = Body(), ctx: Ctx = Depends(user_required)):
    """♥ («like»: a star), 👎 («dislike»: a rating of 1) or neither («none»), in Navidrome (so every music app sees it)
    and in Kurai at once; the nightly taste weighs them (a star like several plays, a low rating against)."""
    if value not in RATINGS or await ctx.db.get(Track, track_id) is None:
        raise HTTPException(404)
    before = (await ctx.db.execute(select(Listen.rating).where(Listen.user_id == ctx.user.id,
                                                               Listen.track_id == track_id))).scalar() or 0
    # 👎 is a rating of 1; ♥ or neither take only a 👎's low rating back (a 4★ set in an app stays)
    starred = value == "like"
    rating = 1 if value == "dislike" else 0 if before in (1, 2) else before
    listen = await library.note_listen(ctx.db, ctx.user, track_id, starred=starred, rating=rating)
    try:
        await library.navidrome().rate(_account(ctx), track_id, starred, rating)
    except NavidromeError as e:  # kept in Kurai; the next sync would bring Navidrome's back, so it's said
        log.warning("rate: %s", e)
        raise HTTPException(502, _("Navidrome не сохранил оценку: {e}").format(e=e)) from e
    return {"liked": listen.starred, "disliked": listen.rating in (1, 2)}


@router.post("/save")
async def save(name: str = Body(), track_ids: list[str] = Body(), ctx: Ctx = Depends(user_required)):
    """The player's list saved under the user's name for it: a playlist of their own (in «Плейлисты» and Navidrome,
    never removed by itself), with the tracks Kurai knows, in its order."""
    name = name.strip()[:NAME_MAX]
    ids = list(dict.fromkeys(track_ids))[:SAVED_MAX]
    known = set((await ctx.db.execute(select(Track.id).where(Track.id.in_(ids)))).scalars()) if ids else set()
    ids = [i for i in ids if i in known]
    if not name or not ids:
        raise HTTPException(422, _("Нужны название и хотя бы один трек"))
    playlist = Playlist(user_id=ctx.user.id, kind="saved", name=name, track_ids=ids)
    ctx.db.add(playlist)
    try:
        await playlists.write(ctx.db, ctx.user, playlist)
    except NavidromeError as e:  # kept in Kurai: it plays here; music apps don't see it
        log.warning("save playlist: %s", e)
    await ctx.db.commit()
    return {"id": playlist.id, "name": playlist.name, "tracks": len(ids)}
