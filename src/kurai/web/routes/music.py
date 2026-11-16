"""«Музыка»: the search in the library (what's already in storage), its first tab; the plugins add theirs after it
(Plugin.music_tabs)."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from kurai.db.models import Listen
from kurai.recommend import search
from kurai.web.deps import Ctx, user_required
from kurai.web.templating import render

router = APIRouter()


@router.get("/music")
async def music(request: Request, q: str = "", kind: list[str] = Query(default=[]),
                ctx: Ctx = Depends(user_required)):
    """The search: everything, or only the kinds ticked (artist, album, track); each result says what it is."""
    query = q.strip()[:200]
    kinds = {k for k in kind if k in search.KINDS}
    found = await search.find(ctx.db, query, kinds) if query else search.Found()
    ids = [t.id for t in found.tracks]
    listens = {row.track_id: row for row in (await ctx.db.execute(
        select(Listen).where(Listen.user_id == ctx.user.id, Listen.track_id.in_(ids)))).scalars()} if ids else {}
    return render(request, "music.html", ctx, q=query, kinds=kinds, found=found, listens=listens)
