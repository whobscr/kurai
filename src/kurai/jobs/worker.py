"""The worker process (`kurai worker`): schedules the recurring and nightly jobs, runs the queue in its two lanes, and
runs the Telegram bot when it's on."""

import asyncio
import logging
from datetime import datetime, timedelta

from kurai import accounts, plugins, settings
from kurai import db as database
from kurai.audio import analyze as audio_analyze
from kurai.db.models import Job, User
from kurai.i18n import _
from kurai.jobs import queue, window
from kurai.notify import notify, send_telegram
from kurai.recommend import artist_ids, library, playlists, popularity, related, retrieval, wave

log = logging.getLogger(__name__)

TICK = 20
# outside the night: how often the light jobs run (plugins add theirs)
EVERY = {"sync_library": timedelta(hours=1), "sync_listens": timedelta(minutes=30),
         "embed_new": timedelta(minutes=30), "popularity": timedelta(hours=3), "artist_ids": timedelta(hours=3)}
# a small number of new tracks is embedded during the day; a big backlog (the first indexing) waits for the night
DAYTIME_EMBED_LIMIT = 200
# the audio analysis (when it's on): new imports during the day, the backlog at night
AUDIO_EVERY = timedelta(minutes=30)
JOB_TIMEOUT_S = 3600  # one hour: a hanging job must not stop the queue
LONG_JOBS = {"nightly", "analyze_audio"}


class PartialFailure(Exception):
    """Some parts of a job failed, the others were done: the job is marked failed with what it did kept."""

    def __init__(self, failed: list[str], result: dict):
        super().__init__("; ".join(failed))
        self.result = result


async def _nightly(db, job: Job) -> dict:
    """The night's work, in order: the library, its vectors, each user's listens, taste and mixes, then the plugins'
    steps (e.g. finding new music). A part that fails is noted and the rest goes on; the job fails at the end, naming
    the failed parts. Each plugin step runs in its own session so a plugin's rollback can't discard core's pending
    work."""
    result: dict = {}
    failed: list[str] = []

    async def step(name: str, run) -> None:
        try:
            result[name] = await run()
        except Exception as e:
            log.exception("night step %s failed", name)
            await db.rollback()
            failed.append(f"{name}: {str(e) or e.__class__.__name__}")

    async def plugin_step(name: str, run) -> None:
        """Plugin step in its own session, committed here: its failures don't touch the nightly session."""
        try:
            async with database.session() as plugin_db:
                result[name] = await run(plugin_db)
                await plugin_db.commit()
        except Exception as e:
            log.exception("plugin step %s failed", name)
            failed.append(f"{name}: {str(e) or e.__class__.__name__}")

    await step("library", lambda: library.sync_library(db))
    await step("artist_ids", lambda: artist_ids.look_up(db))
    await step("embeddings", lambda: library.embed_tracks(db))
    # cached space means for retrieval.joint(): refreshed once, after the library and its vectors are current
    await step("space_means", lambda: retrieval.refresh_means(db))
    wave_on = await settings.get(db, "wave.enabled")
    for user_id, username in [(u.id, u.username) for u in await library.active_users(db)]:
        async def for_user(user_id: int = user_id) -> dict:
            user = await db.get(User, user_id)
            out = {"listens": await library.sync_listens(db, user),
                   "taste": await library.build_taste(db, user),
                   "related": await related.refresh(db, user),
                   "playlists": await playlists.nightly(db, user)}
            if user.favorites_nightly:
                out["favorites"] = await playlists.build_favorites(db, user, create=False)
            if wave_on:
                out["wave"] = await wave.build(db, user)
            return out

        await step(f"user {username}", for_user)
    for plugin in plugins.plugins():
        for name, fn in plugin.nightly:
            await plugin_step(name, fn)
    await step("old_playlists_removed", lambda: playlists.remove_old(db))
    await step("old_sessions_removed", lambda: accounts.prune_sessions(db))
    await step("old_jobs_removed", lambda: queue.prune(db))
    if failed:
        raise PartialFailure(failed, result)
    return result


async def _sync_listens(db, job: Job) -> dict:
    """Each user's plays on their own: one that fails (their Navidrome account) doesn't hold back the others; the job
    fails at the end, naming them."""
    result, failed = {}, []
    for user_id, username in [(u.id, u.username) for u in await library.active_users(db)]:
        try:
            result[username] = await library.sync_listens(db, await db.get(User, user_id))
        except Exception as e:
            await db.rollback()
            failed.append(f"{username}: {str(e) or e.__class__.__name__}")
    if failed:
        raise PartialFailure(failed, result)
    return result


async def _embed_new(db, job: Job) -> dict:
    if await library.pending_embeddings(db) > DAYTIME_EMBED_LIMIT:
        return {"skipped": _("много новых треков: проиндексируются ночью")}
    return await library.embed_tracks(db)


async def _ask(db, job: Job) -> dict:
    """A request from the web or from Telegram; one from Telegram (`reply_to`: its chat) is answered there."""
    user = await db.get(User, job.user_id)
    chat = job.payload.get("reply_to")
    result = await playlists.ask(db, user, job.payload["text"])
    if "playlist_id" in result:
        text = _("Плейлист «{name}» готов — он уже в вашем плеере").format(name=result["name"])
        if result.get("short"):
            text += _(". На этом языке нашлось только {n} треков").format(n=result["tracks"])
        await notify(db, user.id, text, "/", telegram=not chat)
        if chat:
            await send_telegram(db, chat, text, "/")
    elif chat and result.get("answer"):
        await send_telegram(db, chat, result["answer"])
    return result


async def _wave(db, job: Job) -> dict:
    """A new version of the wave; with «retaste» (the user just said what they like), from their taste made anew."""
    # jobs queued before 0.2.23 have the user in the payload only
    user = await db.get(User, job.user_id or job.payload["user_id"])
    if (job.payload or {}).get("retaste"):
        await library.build_taste(db, user)
    return await wave.build(db, user)


async def _explain(db, job: Job) -> dict:
    return {"text": await playlists.explain(db, await db.get(User, job.user_id), job.payload["track_id"])}


HANDLERS = {
    "nightly": _nightly,
    "sync_library": lambda db, job: library.sync_library(db),
    "embed_new": _embed_new,
    "sync_listens": _sync_listens,
    "ask": _ask,
    "explain": _explain,
    "wave": _wave,
    "popularity": lambda db, job: popularity.refresh(db),
    "artist_ids": lambda db, job: artist_ids.look_up(db),
    "analyze_audio": audio_analyze.run,  # taken only by the audio container's lane
}


def handlers() -> dict:
    return {**HANDLERS, **plugins.jobs()}


async def run_one(job_id: int) -> None:
    """Run a job with a per-job timeout (whole-library jobs excepted). A hanging job is cancelled and marked failed
    rather than stopping the queue; the failure is recorded in a fresh session so a broken one can't lose it."""
    async with database.session() as db:
        job = await db.get(Job, job_id)
        kind = job.kind
        handler = handlers().get(kind)
        timeout = None if kind in LONG_JOBS else JOB_TIMEOUT_S
        ask_user = job.user_id if kind == "ask" else None
        chat = (job.payload or {}).get("reply_to")
        error, partial = None, None
        try:
            if handler is None:
                raise ValueError(_("неизвестная задача {kind}").format(kind=kind))
            result = await asyncio.wait_for(handler(db, job), timeout=timeout)
            await queue.finish(db, job, result if isinstance(result, dict) else {"result": result})
            return
        except TimeoutError:
            log.error("job %s (%s) timed out after %s s", job_id, kind, timeout)
            error = f"timed out after {timeout} s"
        except Exception as e:
            log.exception("job %s (%s) failed", job_id, kind)
            error = (str(e) or e.__class__.__name__)[:2000]
            partial = e.result if isinstance(e, PartialFailure) else None
        try:
            await db.rollback()
        except Exception:
            log.warning("rollback after job %s failed; finishing in a fresh session", job_id)
    async with database.session() as db:
        job = await db.get(Job, job_id)
        await queue.finish(db, job, partial, error=error)
        if ask_user:
            text = _("Не получилось: {message}").format(message=error[:300])
            await notify(db, ask_user, text, "/ask", telegram=not chat)
            if chat:
                await send_telegram(db, chat, text)


async def schedule(now: datetime) -> bool:
    """Puts the due recurring jobs into the queue; returns whether it's night."""
    async with database.session() as db:
        values = await settings.get_all(db)
        night = window.current(now, values["night.start"], values["night.end"])
        if night:
            key = f"nightly:{night[0].date().isoformat()}"
            await queue.drop_stale(db, "nightly", key)
            await queue.enqueue(db, "nightly", night_only=True, priority=60, dedupe_key=key, once=True)
        for kind, every in {**EVERY, **plugins.every()}.items():
            last = await queue.last_finished(db, kind)
            if last is None or now - last >= every:
                await queue.enqueue(db, kind, priority=40, dedupe_key=kind)
        if values["audio.enabled"]:
            await _schedule_audio(db, now, night)
        return night is not None


async def _schedule_audio(db, now: datetime, night: tuple[datetime, datetime] | None) -> None:
    """At night: one run until the window ends; by day: a small run every half hour for the new imports."""
    if night:
        key = f"analyze_audio:{night[0].date().isoformat()}"
        await queue.drop_stale(db, "analyze_audio", key)
        await queue.enqueue(db, "analyze_audio", {"until": night[1].isoformat()}, night_only=True, priority=60,
                            dedupe_key=key, once=True)
        return
    last = await queue.last_finished(db, "analyze_audio")
    if last is None or now - last >= AUDIO_EVERY:
        await queue.enqueue(db, "analyze_audio", {"limit": audio_analyze.DAYTIME_LIMIT}, priority=40,
                            dedupe_key="analyze_audio")


async def lane(interactive: bool, state: dict) -> None:
    running: set[asyncio.Task] = set()
    while True:
        async with database.session() as db:
            limit = int(await settings.get(db, "llm.parallel")) if interactive else 1
            while len(running) < max(1, limit):
                job = await queue.claim(db, "interactive" if interactive else "general", state["night"],
                                        handlers().keys())
                if job is None:
                    break
                task = asyncio.create_task(run_one(job.id))
                running.add(task)
                task.add_done_callback(running.discard)
        await asyncio.sleep(2 if interactive else 5)


async def plugins_switched(db) -> bool:
    """Whether the plugins' switches changed since this process started (it then restarts to pick them up)."""
    saved = frozenset(await settings.get(db, plugins.DISABLED_KEY))
    off = plugins.disabled()
    if not plugins.disabled.last_read_ok:
        return False
    return saved != off


async def scheduler(state: dict) -> None:
    while True:
        async with database.session() as db:
            if await plugins_switched(db):
                log.info("the plugins were switched on or off: restarting")
                return
        try:
            state["night"] = await schedule(datetime.now().astimezone())
        except Exception:
            log.exception("scheduling failed")
        await asyncio.sleep(TICK)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from kurai.bootstrap import bootstrap

    await bootstrap()
    async with database.session() as db:
        await queue.requeue_interrupted(db, ("general", "interactive"))
        for plugin in plugins.plugins():
            for kind in plugin.on_start:
                await queue.enqueue(db, kind, priority=10, dedupe_key=kind)
    state = {"night": False}
    from kurai.telegram.bot import run_bot

    # the scheduler returns when the plugins' switches change: the worker then stops, and Docker starts it again
    # (restart: unless-stopped); a job cut short is queued again at start
    tasks = [asyncio.create_task(c) for c in (scheduler(state), lane(False, state), lane(True, state), run_bot(),
                                              wave.watch())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # a crash is raised as before


def run() -> None:
    asyncio.run(main())
