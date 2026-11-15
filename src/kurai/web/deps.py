"""Who is asking: the session cookie, the logged-in user, admin-only pages, and CSRF protection of every form and
HTMX request (a token per session, in a hidden field or the X-CSRF-Token header)."""

import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import db as database
from kurai import security
from kurai.accounts import SESSION_DAYS
from kurai.db.models import Session, User

COOKIE = "kurai_session"


class LoginRequired(Exception):
    """Raised by `user_required`; the app turns it into a redirect to /login."""


class PasswordChangeRequired(Exception):
    """The first admin (admin / admin) and users whose password an admin reset must change it first."""


async def get_db() -> AsyncIterator[AsyncSession]:
    async with database.session() as db:
        yield db


@dataclass
class Ctx:
    db: AsyncSession
    session: Session | None
    user: User | None

    @property
    def csrf(self) -> str:
        return self.session.csrf_token if self.session else ""


async def context(request: Request, db: AsyncSession = Depends(get_db)) -> Ctx:
    token = request.cookies.get(COOKIE)
    if not token:
        return Ctx(db, None, None)
    session = await db.get(Session, security.token_id(token))
    if session is None or datetime.now(UTC) - session.last_seen > timedelta(days=SESSION_DAYS):
        return Ctx(db, None, None)
    user = await db.get(User, session.user_id)
    if user is None or not user.active:
        return Ctx(db, None, None)
    if datetime.now(UTC) - session.last_seen > timedelta(minutes=5):
        session.last_seen = datetime.now(UTC)
        await db.commit()
        request.state.renew_cookie = token  # the cookie lives as long as the session (app.py sets it again)
    return Ctx(db, session, user)


def set_session_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")


async def check_csrf(request: Request, ctx: Ctx) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    sent = request.headers.get("X-CSRF-Token", "")
    if not sent and request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")):
        sent = str((await request.form()).get("csrf", ""))
    if not ctx.session or not hmac.compare_digest(sent, ctx.session.csrf_token):
        raise HTTPException(403, "CSRF")


async def user_required(request: Request, ctx: Ctx = Depends(context)) -> Ctx:
    if ctx.user is None or ctx.session is None or ctx.session.pending_totp:
        raise LoginRequired()
    if ctx.user.must_change_password and request.url.path not in {"/password", "/logout"}:
        raise PasswordChangeRequired()
    await check_csrf(request, ctx)
    return ctx


async def admin_required(ctx: Ctx = Depends(user_required)) -> Ctx:
    if not ctx.user.is_admin:
        raise HTTPException(403)
    return ctx
