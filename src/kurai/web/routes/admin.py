"""«Системные настройки» (admins only): one tab per area (plugins add theirs), users, the apps, jobs, and the
«Проверить» buttons. Saving a tab only writes the settings; jobs pick them up (the worker re-reads them)."""

import asyncio
import os
import signal
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from starlette.background import BackgroundTask

from kurai import accounts, llm, outbound, plugins, settings
from kurai.audio import analyze as audio_analyze
from kurai.audio import client as audio_client
from kurai.db.models import Job, LlmCall, User
from kurai.i18n import _
from kurai.jobs import queue, window
from kurai.web.deps import Ctx, admin_required
from kurai.web.templating import render

router = APIRouter(prefix="/admin")

CORE_TABS = {
    "general": _("Общие"),
    "models": _("Модели"),
    "audio": _("Звук"),
    "gateway": _("Шлюз"),
    "users": _("Пользователи"),
    "apps": _("Приложения"),
    "plugins": _("Плагины"),
    "jobs": _("Задачи"),
}
# each tab's help page, linked at its top (a plugin's tab says its own: AdminTab.docs)
CORE_DOCS = {"general": "/docs/admin/first-run", "models": "/docs/admin/models", "audio": "/docs/admin/audio",
             "gateway": "/docs/admin/gateway", "users": "/docs/admin/users", "apps": "/docs/admin/apps#apps",
             "plugins": "/docs/admin/apps#plugins", "jobs": "/docs/admin/jobs"}
# «Запустить» on «Задачи»: Kurai's own; plugins add theirs
CORE_MANUAL_JOBS = {"nightly": _("Ночная работа сейчас"), "sync_library": _("Синхронизировать библиотеку"),
                    "sync_listens": _("Прослушивания")}


def plugin_tabs() -> dict[str, plugins.AdminTab]:
    return plugins.merged("admin_tabs")


def tabs() -> dict[str, str]:
    """Kurai's tabs with the plugins' ones after «Шлюз»."""
    keys = list(CORE_TABS)
    at = keys.index("gateway") + 1
    extra = {k: t.label for k, t in plugin_tabs().items()}
    return {**dict((k, CORE_TABS[k]) for k in keys[:at]), **extra, **dict((k, CORE_TABS[k]) for k in keys[at:])}


def tab_docs(tab: str) -> str:
    return plugin_tabs()[tab].docs if tab in plugin_tabs() else CORE_DOCS.get(tab, "")


def manual_jobs() -> dict[str, str]:
    return {**CORE_MANUAL_JOBS, **plugins.merged("manual_jobs")}

# which settings each tab's form may write, and their types
FIELDS: dict[str, dict[str, type]] = {
    "general": {"ui.default_accent": str, "night.start": str, "night.end": str, "playlists.per_night": int,
                "wave.enabled": bool, "telegram.enabled": bool, "telegram.token": str},
    "models": {"llm.enabled": bool, "llm.provider": str, "llm.server_type": str, "llm.base_url": str,
               "llm.api_key": str, "llm.model": str, "llm.timeout": int, "llm.parallel": int,
               "embed.provider": str, "embed.base_url": str, "embed.api_key": str, "embed.model": str,
               **{f"think.{t}": bool for t in settings.LLM_TASKS}},
    "audio": {"audio.enabled": bool, "audio.url": str, "audio.clap": bool, "audio.mix_weight": float,
              "audio.ask_weight": float},
    "gateway": {"outbound.proxy": str, "outbound.user": str, "outbound.password": str, "outbound.dns": str},
}
MANUAL_AUDIO_TRACKS = 100
JOBS_PAGE = 30
# «Задачи»'s filter: label, and which statuses it shows (None: all)
JOB_FILTERS = {"": (_("Все"), None), "failed": (_("Ошибки"), ["failed"]),
               "active": (_("Идут и в очереди"), ["running", "queued"])}


def fields(tab: str) -> dict[str, type] | None:
    if tab == "gateway":  # a checkbox for every subsystem, the plugins' too
        return {**FIELDS["gateway"], **{f"outbound.via.{n}": bool for n in settings.outbound_subsystems()}}
    if tab in plugin_tabs():
        return plugin_tabs()[tab].fields
    return FIELDS.get(tab)


def back(tab: str, message: str = "", **extra: Any) -> RedirectResponse:
    """Back to a tab with a message (URL-encoded: the messages are Russian)."""
    query = urlencode({"message": message, **extra}) if message else ""
    return RedirectResponse(f"/admin/{tab}" + (f"?{query}" if query else ""), 303)


def _coerce(kind: type, raw: str | None) -> Any:
    if kind is bool:
        return raw == "on"
    if raw is None:
        return None
    return kind(raw.strip()) if kind is not str else raw.strip()


def _validate(values: dict[str, Any]) -> str | None:
    for key in ("night.start", "night.end"):
        if key in values:
            try:
                window.parse(values[key])
            except ValueError:
                return _("Время — в виде ЧЧ:ММ")
    if values.get("outbound.dns"):
        try:
            outbound.parse_dns(values["outbound.dns"])
        except ValueError:
            return _("DNS-сервер — IP-адрес, например 10.8.0.1 или 10.8.0.1:53, а не имя")
    if "llm.parallel" in values and not 1 <= values["llm.parallel"] <= 16:
        return _("Параллельных запросов — от 1 до 16")
    if any(not 0 <= values[k] <= 1 for k in ("audio.mix_weight", "audio.ask_weight") if k in values):
        return _("Доля звука — от 0 до 1")
    return None


async def _tab_data(ctx: Ctx, tab: str, page: int = 1, status: str = "") -> dict[str, Any]:
    db = ctx.db
    data: dict[str, Any] = {}
    if tab == "general":
        values = await settings.get_all(db)
        data["night_hours"] = window.hours(values["night.start"], values["night.end"])
    if tab == "models":
        rows = (await db.execute(select(LlmCall.task, LlmCall.thinking, func.avg(LlmCall.seconds), func.count())
                                 .where(LlmCall.ok.is_(True)).group_by(LlmCall.task, LlmCall.thinking))).all()
        data["timings"] = {(t, th): (round(avg), n) for t, th, avg, n in rows}
    if tab == "audio":
        data["audio"] = await audio_analyze.progress(db)
        values = await settings.get_all(db)
        data["night_seconds"] = window.hours(values["night.start"], values["night.end"]) * 3600
    if tab == "users":
        data["users"] = (await db.execute(select(User).order_by(User.id))).scalars().all()
    if tab == "apps":
        data["apps"] = [("navidrome", "Navidrome", True)] + [(k, a.label, a.frame)
                                                             for k, a in plugins.merged("apps").items()]
    if tab == "plugins":
        off = set(await settings.get(db, plugins.DISABLED_KEY))
        data["plugin_list"] = [(p, p.name not in off) for p in plugins.installed()]
        data["restarting"] = off != plugins.disabled()
    if tab in plugin_tabs() and plugin_tabs()[tab].data:
        data.update(await plugin_tabs()[tab].data(db))
    if tab == "jobs":
        statuses = JOB_FILTERS.get(status, JOB_FILTERS[""])[1]
        query = select(Job).where(Job.status.in_(statuses)) if statuses else select(Job)
        page = max(1, page)
        # one more than a page: whether there's an older one
        rows = (await db.execute(query.order_by(Job.id.desc()).offset((page - 1) * JOBS_PAGE)
                                 .limit(JOBS_PAGE + 1))).scalars().all()
        data.update(jobs=rows[:JOBS_PAGE], jobs_page=page, jobs_older=len(rows) > JOBS_PAGE,
                    jobs_status=status if status in JOB_FILTERS else "", job_filters=JOB_FILTERS)
    return data


@router.get("")
async def index():
    return RedirectResponse("/admin/general", 303)


@router.get("/{tab}")
async def tab_page(request: Request, tab: str, message: str = "", confirm: int | None = None, page: int = 1,
                   status: str = "", ctx: Ctx = Depends(admin_required)):
    if tab not in tabs():
        raise HTTPException(404)
    values = await settings.get_all(ctx.db)
    return render(request, "admin.html", ctx, tab=tab, tabs=tabs(), values=values, llm_tasks=settings.LLM_TASKS,
                  subsystems=settings.outbound_subsystems(), plugin_tab=plugin_tabs().get(tab),
                  manual_jobs=manual_jobs(), message=message, confirm=confirm, tab_docs=tab_docs(tab),
                  **await _tab_data(ctx, tab, page, status))


async def _restart_web() -> None:
    """The web process stops shortly after the answer is sent, and Docker starts it again (restart: unless-stopped)
    with the plugins switched as saved. The worker notices the change itself."""
    asyncio.get_running_loop().call_later(1, os.kill, os.getpid(), signal.SIGTERM)


async def save_plugins(request: Request, ctx: Ctx) -> RedirectResponse:
    form = await request.form()
    off = sorted(p.name for p in plugins.installed() if form.get(f"plugin.{p.name}") != "on")
    await settings.put(ctx.db, {plugins.DISABLED_KEY: off})
    await ctx.db.commit()
    if frozenset(off) == plugins.disabled():
        return back("plugins", _("Сохранено"))
    response = back("plugins", _("Сохранено. Kurai перезапускается — обновите страницу через полминуты."))
    response.background = BackgroundTask(_restart_web)
    return response


@router.post("/{tab}")
async def save(request: Request, tab: str, ctx: Ctx = Depends(admin_required)):
    if tab == "plugins":
        return await save_plugins(request, ctx)
    tab_fields = fields(tab)
    if not tab_fields:
        raise HTTPException(404)
    form = await request.form()
    values: dict[str, Any] = {}
    try:
        for key, kind in tab_fields.items():
            raw = form.get(key)
            # secrets are shown as empty fields: an empty field keeps the stored value
            if key in settings.secret_keys() and not raw:
                continue  # an empty secret field keeps the stored value
            values[key] = _coerce(kind, str(raw) if raw is not None else None)
    except ValueError:
        return back(tab, _("Неверное значение"))
    plugin_tab = plugin_tabs().get(tab)
    if error := _validate(values) or (plugin_tab and plugin_tab.validate and plugin_tab.validate(values)):
        return back(tab, error)
    before = await settings.get_all(ctx.db)
    await settings.put(ctx.db, values)
    await ctx.db.commit()
    if tab == "general" and values.get("ui.default_accent"):
        request.app.state.default_accent = values["ui.default_accent"]
    # what depends on the saved values is re-applied in the background: the plugins' setup after the gateway
    if tab == "gateway":
        for plugin in plugins.plugins():
            for kind in plugin.on_start:
                await queue.enqueue(ctx.db, kind, priority=10, dedupe_key=kind)
    if plugin_tab and plugin_tab.after_save:
        await plugin_tab.after_save(ctx.db, values, before)
    if tab == "models" and values.get("embed.model") != before["embed.model"]:
        await queue.enqueue(ctx.db, "embed_new", priority=60, night_only=True, dedupe_key="reembed")
    return back(tab, _("Сохранено"))


# ---------- the «Проверить» buttons ----------

# a gateway subsystem's check: one request through its route
CHECK_URLS = {"telegram": "https://api.telegram.org", "llm": "https://api.anthropic.com",
              "webpush": "https://fcm.googleapis.com", "models": "https://huggingface.co"}


@router.post("/check/{what}")
async def check(what: str, ctx: Ctx = Depends(admin_required)):
    """One small request through the real path; the answer's time or the error's text."""
    started = time.monotonic()
    try:
        if what in plugins.merged("checks"):
            detail = await plugins.merged("checks")[what](ctx.db)
        elif what == "llm":
            answer = await llm.complete(ctx.db, "explain", "Answer with one word.", "Say OK", max_tokens=50)
            detail = answer[:80]
        elif what == "embed":
            vectors = await (await llm.embed_model(ctx.db)).embed(["проверка"])
            detail = f"{len(vectors[0])} измерений"
        elif what == "audio":
            health = await audio_client.health(ctx.db)
            detail = _("сервис отвечает, потоков: {n}").format(n=health["threads"])
            if await settings.get(ctx.db, "audio.clap"):
                vectors = await audio_client.clap_vectors(ctx.db, ["calm piano at night"])
                detail += _("; CLAP: {d} измерений").format(d=len(vectors[0]))
        elif what in CHECK_URLS:
            url = CHECK_URLS[what]
            async with await outbound.session_for(ctx.db, what, 20) as s, s.get(url) as resp:
                detail = f"HTTP {resp.status}"
        else:
            raise HTTPException(404)
        return {"ok": True, "seconds": round(time.monotonic() - started, 1), "detail": detail}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "seconds": round(time.monotonic() - started, 1), "detail": str(e) or e.__class__.__name__}


# ---------- actions ----------


@router.post("/audio/run")
async def audio_run(ctx: Ctx = Depends(admin_required)):
    """«Разобрать сейчас»: a run outside the night (also with a big backlog), up to MANUAL_AUDIO_TRACKS tracks."""
    await queue.enqueue(ctx.db, "analyze_audio", {"limit": MANUAL_AUDIO_TRACKS, "manual": True}, priority=30,
                        dedupe_key="manual:analyze_audio")
    return back("audio", _("Анализ запущен: до {n} треков").format(n=MANUAL_AUDIO_TRACKS))


@router.post("/audio/retry")
async def audio_retry(ctx: Ctx = Depends(admin_required)):
    count = await audio_analyze.retry_failed(ctx.db)
    return back("audio", _("Неудачные треки попробуем ещё раз: {n}").format(n=count))


@router.post("/users/create")
async def create_user(username: str = Form(...), display_name: str = Form(""), password: str = Form(...),
                      role: str = Form("user"), ctx: Ctx = Depends(admin_required)):
    try:
        await accounts.create_user(ctx.db, username, display_name, password, role)
        message = _("Пользователь добавлен")
    except accounts.AccountError as e:
        message = str(e)
    except Exception as e:  # Navidrome unreachable
        message = _("Navidrome: {e}").format(e=e)
    return back("users", message)


@router.post("/users/{user_id}/{action}")
async def user_action(user_id: int, action: str, password: str = Form(""), ctx: Ctx = Depends(admin_required)):
    user = await ctx.db.get(User, user_id)
    if user is None:
        raise HTTPException(404)
    try:
        if action == "role":
            await accounts.set_role(ctx.db, user, "user" if user.is_admin else "admin")
        elif action == "active":
            await accounts.set_active(ctx.db, user, not user.active)
        elif action == "password":
            await accounts.set_password(ctx.db, user, password)
            user.must_change_password = True
            await ctx.db.commit()
        elif action == "reset-2fa":
            user.totp_enabled, user.totp_secret, user.recovery_codes = False, None, []
            await ctx.db.commit()
        else:
            raise HTTPException(404)
        message = _("Сделано")
    except accounts.AccountError as e:
        message = str(e)
    return back("users", message)


@router.post("/jobs/run/{kind}")
async def run_job(kind: str, ctx: Ctx = Depends(admin_required)):
    if kind not in manual_jobs():
        raise HTTPException(404)
    # «Запустить сейчас» runs the night's work at once, outside the window too
    await queue.enqueue(ctx.db, kind, priority=30, dedupe_key=f"manual:{kind}")
    return RedirectResponse("/admin/jobs", 303)
