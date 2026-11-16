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
from kurai.db.models import Job, Playlist, Track
from kurai.i18n import _
from kurai.integrations.navidrome import WEB_PLAYER, Account, NavidromeError
from kurai.jobs import queue as jobs
from kurai.recommend import library, wave
from kurai.web import upstream
from kurai.web.deps import Ctx, user_required
from kurai.web.routes.misc import MEDIA_HEADERS

router = APIRouter(prefix="/player")
log = logging.getLogger(__name__)
COVER_SIZE = "600"


def _account(ctx: Ctx) -> Account:
    account = library.user_account(ctx.user)
    if account is None:
        raise HTTPException(409, _("У вашей учётной записи ещё нет доступа к музыке (Navidrome) — подождите минуту."))
    return account


async def _tracks(ctx: Ctx, ids: list[str]) -> list[dict]:
    """The playlist's tracks Kurai knows, in its order."""
    rows = {t.id: t for t in (await ctx.db.execute(select(Track).where(Track.id.in_(ids)))).scalars()}
    return [{"id": t.id, "title": t.title, "artist": t.artist, "album": t.album, "duration": t.duration}
            for t in (rows.get(i) for i in ids) if t is not None]


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


@router.get("/queue/{which}")
async def queue(which: str, ctx: Ctx = Depends(user_required)):
    """«wave» (the user's wave as it is now) or a playlist's id: its tracks, and its version (the player compares it to
    learn that the wave was rebuilt)."""
    if which == "wave":
        playlist = (await ctx.db.execute(select(Playlist).where(Playlist.user_id == ctx.user.id,
                                                                Playlist.kind == "wave")
                                         .order_by(Playlist.id.desc()))).scalars().first()
        if playlist is None or not playlist.track_ids:
            return await _wave_state(ctx)
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
async def event(track_id: str = Body(), seconds: int = Body(), ctx: Ctx = Depends(user_required)):
    """A track gave way to the next one (or the player stopped) after `seconds` of playing: played on or skipped."""
    track = await ctx.db.get(Track, track_id)
    if track is None:
        raise HTTPException(404)
    now = datetime.now(UTC)
    seconds = max(0, seconds)
    skipped = seconds < wave.SKIP_SHARE * (track.duration or wave.DEFAULT_DURATION)
    if not skipped:
        try:
            await library.navidrome().scrobble(_account(ctx), track_id, int(now.timestamp() * 1000))
        except NavidromeError as e:  # the play still counts for the wave
            log.warning("scrobble: %s", e)
    rebuilding = False
    if await settings.get(ctx.db, "wave.enabled"):
        rebuilding = bool(await wave.record(ctx.db, [(ctx.user.username, track_id, skipped, seconds)], now))
    return {"skipped": skipped, "rebuilding": rebuilding}

