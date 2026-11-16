"""The pages every user has: «Для вас» (the wave's big button, the playlists, notifications), «Спросить», and the PWA's
share target."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from kurai import plugins, settings
from kurai.db.models import Job, Listen, LlmCall, Notification, Playlist
from kurai.jobs import queue
from kurai.recommend import start
from kurai.web.deps import Ctx, user_required
from kurai.web.templating import render

router = APIRouter()


async def typical_seconds(ctx: Ctx, task: str) -> int | None:
    """The median duration of the task's recent model requests, for «обычно занимает ~N с»."""
    rows = (await ctx.db.execute(select(LlmCall.seconds).where(LlmCall.task == task, LlmCall.ok.is_(True))
                                 .order_by(LlmCall.id.desc()).limit(20))).scalars().all()
    return int(sorted(rows)[len(rows) // 2]) if rows else None


@router.get("/")
async def home(request: Request, ctx: Ctx = Depends(user_required)):
    playlists = (await ctx.db.execute(select(Playlist).where(Playlist.user_id == ctx.user.id)
                                      .order_by(Playlist.created_at.desc()).limit(24))).scalars().all()
    notes = (await ctx.db.execute(select(Notification).where(Notification.user_id == ctx.user.id)
                                  .order_by(Notification.created_at.desc()).limit(10))).scalars().all()
    await ctx.db.execute(update(Notification).where(Notification.user_id == ctx.user.id).values(read=True))
    await ctx.db.commit()
    wave = next((p for p in playlists if p.kind == "wave"), None)
    # a listener who hasn't played anything and wasn't asked yet: what do they like (the wave starts from it)
    ask_start = ctx.user.start_asked_at is None and not (await ctx.db.execute(
        select(Listen.track_id).where(Listen.user_id == ctx.user.id).limit(1))).first()
    return render(request, "home.html", ctx, wave=wave, mixes=[p for p in playlists if p.kind != "wave"], notes=notes,
                  wave_on=await settings.get(ctx.db, "wave.enabled"),
                  styles=await start.library_styles(ctx.db) if ask_start else [], ask_start=ask_start)


@router.post("/playlists/{playlist_id}/feedback")
async def feedback(playlist_id: int, value: int = Form(...), ctx: Ctx = Depends(user_required)):
    playlist = await ctx.db.get(Playlist, playlist_id)
    if playlist is None or playlist.user_id != ctx.user.id:
        raise HTTPException(404)
    playlist.feedback = max(-1, min(1, value))
    await ctx.db.commit()
    return RedirectResponse("/", 303)


@router.get("/ask")
async def ask_page(request: Request, text: str = "", ctx: Ctx = Depends(user_required)):
    jobs = (await ctx.db.execute(select(Job).where(Job.user_id == ctx.user.id, Job.kind == "ask")
                                 .order_by(Job.id.desc()).limit(10))).scalars().all()
    return render(request, "ask.html", ctx, jobs=jobs, typical=await typical_seconds(ctx, "ask"), text=text[:1000])


@router.post("/ask")
async def ask(text: str = Form(...), ctx: Ctx = Depends(user_required)):
    text = text.strip()[:1000]
    if text:
        await queue.enqueue(ctx.db, "ask", {"text": text}, user_id=ctx.user.id, priority=10)
    return RedirectResponse("/ask", 303)


@router.get("/ask/jobs")
async def ask_jobs(request: Request, ctx: Ctx = Depends(user_required)):
    """The list's partial, polled by HTMX while something is still running."""
    jobs = (await ctx.db.execute(select(Job).where(Job.user_id == ctx.user.id, Job.kind == "ask")
                                 .order_by(Job.id.desc()).limit(10))).scalars().all()
    return render(request, "partials/ask_jobs.html", ctx, jobs=jobs, typical=await typical_seconds(ctx, "ask"))


@router.post("/explain/{track_id}")
async def explain(track_id: str, ctx: Ctx = Depends(user_required)):
    job = await queue.enqueue(ctx.db, "explain", {"track_id": track_id}, user_id=ctx.user.id, priority=10)
    return {"job": job.id}


@router.get("/jobs/{job_id}")
async def job_state(job_id: int, ctx: Ctx = Depends(user_required)):
    job = await ctx.db.get(Job, job_id)
    if job is None or (job.user_id != ctx.user.id and not ctx.user.is_admin):
        raise HTTPException(404)
    return {"status": job.status, "result": job.result, "error": job.error}


@router.get("/share")
async def share(request: Request, ctx: Ctx = Depends(user_required)):
    """The PWA's share target (GET, so no form token is needed): a link or text shared from another app opens a
    plugin's page for it (e.g. an album search), or «Спросить» with the text filled in."""
    term = " ".join(v for k in ("title", "text", "url") if (v := request.query_params.get(k, "").strip()))
    path = next((p.share_path for p in plugins.plugins() if p.share_path), "/ask?text={q}")
    return RedirectResponse(path.replace("{q}", quote(term[:300])), 303)
