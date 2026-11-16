"""The web app (`kurai web`): FastAPI with server-rendered pages (Jinja2, Beer CSS, HTMX)."""

import logging
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from kurai import db as database
from kurai import plugins, settings
from kurai.i18n import _
from kurai.web.deps import LoginRequired, PasswordChangeRequired, context, set_session_cookie
from kurai.web.routes import admin, apps, auth, main, me, misc, music, player
from kurai.web.templating import ROOT, art_hours, doodle, render

ERRORS = {
    403: _("Сюда нельзя"),
    404: _("Такой страницы нет"),
    500: _("Что-то сломалось"),
    502: _("Сервис не отвечает"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from kurai.bootstrap import bootstrap

    await bootstrap()
    async with database.session() as db:
        values = await settings.get_all(db)
    app.state.default_accent = values["ui.default_accent"]
    app.state.art_hours = art_hours(values)
    yield
    await database.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Kurai", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.default_accent = settings.defaults()["ui.default_accent"]
    app.state.art_hours = art_hours(settings.defaults())
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    for module in (auth, main, me, music, player, admin, apps, misc):
        app.include_router(module.router)
    for plugin in plugins.plugins():
        if plugin.router is not None:
            app.include_router(plugin.router)

    @app.middleware("http")
    async def renew_cookie(request: Request, call_next):
        """A session in use keeps its cookie as long as itself (deps.context marks when it was seen again)."""
        response = await call_next(request)
        token = getattr(request.state, "renew_cookie", None)
        if token and "set-cookie" not in response.headers:
            set_session_cookie(response, token, request)
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        if request.headers.get("HX-Request"):
            return RedirectResponse("/login", 303, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", 303)

    @app.exception_handler(PasswordChangeRequired)
    async def to_password(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/password", 303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        # FastAPI's HTTPException is a subclass: this catches both, unknown addresses (404) too
        if request.url.path.startswith(("/jobs/", "/admin/check/", "/me/push", "/player/")) or exc.status_code < 400:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        # Starlette's own details are just the status's phrase ("Not Found"): only Kurai's messages are shown
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail != HTTPStatus(exc.status_code).phrase else ""
        return await _error_page(request, exc.status_code, detail)

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception):
        logging.getLogger("kurai").exception("unhandled error on %s", request.url.path)
        return await _error_page(request, 500, "")

    return app


async def _error_page(request: Request, status: int, detail: str):
    ctx = None
    try:
        async with database.session() as db:
            ctx = await context(request, db)
            return render(request, "error.html", ctx, status_code=status, status=status,
                          title=ERRORS.get(status, _("Ошибка")), detail=detail, doodle=doodle())
    except Exception:
        return render(request, "error.html", None, status_code=status, status=status,
                      title=ERRORS.get(status, _("Ошибка")), detail="", doodle=doodle())


app = create_app()
