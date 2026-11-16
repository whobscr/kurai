"""The plugin's web part: its tab of «Музыка» (what's coming into the library), the data its admin tab shows, and the
approve / reject buttons on it."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from kurai.i18n import _
from kurai.jobs import queue
from kurai.web.deps import Ctx, admin_required, user_required
from kurai.web.routes.admin import back
from kurai.web.templating import render
from kurai_incoming.models import IncomingAlbum

router = APIRouter()


async def tab_data(db) -> dict:
    """Extra values for the tab's template (AdminTab.data)."""
    rows = (await db.execute(select(IncomingAlbum).order_by(IncomingAlbum.found_at.desc()).limit(100))).scalars().all()
    return {"albums": rows}


@router.get("/music/incoming")
async def music_tab(request: Request, ctx: Ctx = Depends(user_required)):
    """A tab of «Музыка» (Plugin.music_tabs): the albums on their way into the library, for everyone."""
    rows = (await ctx.db.execute(select(IncomingAlbum).where(IncomingAlbum.status.in_(("new", "approved")))
                                 .order_by(IncomingAlbum.found_at.desc()).limit(100))).scalars().all()
    return render(request, "incoming/music_incoming.html", ctx, albums=rows)


@router.post("/admin/incoming/{album_id}/{verdict}")
async def decide(album_id: int, verdict: str, ctx: Ctx = Depends(admin_required)):
    album = await ctx.db.get(IncomingAlbum, album_id)
    if album is None or album.status != "new" or verdict not in ("approve", "reject"):
        raise HTTPException(404)
    album.status = "approved" if verdict == "approve" else "rejected"
    await ctx.db.commit()
    if album.status == "approved":  # moved by the job, soon
        await queue.enqueue(ctx.db, "incoming_scan", priority=30, dedupe_key="incoming_scan")
    return back("incoming", _("Одобрено: альбом скоро появится в библиотеке") if album.status == "approved"
                else _("Отклонено"))
