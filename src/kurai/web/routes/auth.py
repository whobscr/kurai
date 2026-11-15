"""Logging in: password, then the TOTP code when the user turned it on (or a recovery code), then a forced password
change for the first admin. Every failure gets the same short sentence: nothing tells whether the name exists."""

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from kurai import accounts, security
from kurai.db.models import Session, User
from kurai.i18n import _
from kurai.web.deps import COOKIE, SESSION_DAYS, Ctx, check_csrf, context, user_required
from kurai.web.templating import login_scene, render

router = APIRouter()
FAILED = _("Неверное имя или пароль.")


def _set_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")


@router.get("/login")
async def login_page(request: Request, ctx: Ctx = Depends(context)):
    if ctx.user and ctx.session and not ctx.session.pending_totp:
        return RedirectResponse("/", 303)
    step = "totp" if ctx.session and ctx.session.pending_totp else "password"
    return render(request, "login.html", ctx, step=step, scene=login_scene(), error=None)


@router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), ctx: Ctx = Depends(context)):
    user = (await ctx.db.execute(select(User).where(User.username == username.strip().lower()))).scalar_one_or_none()
    if user is None or not user.active or not security.verify_password(user.password_hash, password):
        await asyncio.sleep(1)  # slows guessing down
        return render(request, "login.html", ctx, step="password", scene=login_scene(), error=FAILED, status_code=401)
    token = security.random_token()
    ctx.db.add(Session(id=security.token_id(token), user_id=user.id, pending_totp=user.totp_enabled,
                       csrf_token=security.random_token(24)))
    await ctx.db.commit()
    response = RedirectResponse("/login" if user.totp_enabled else "/", 303)
    _set_cookie(response, token, request)
    return response


@router.post("/login/totp")
async def login_totp(request: Request, code: str = Form(""), ctx: Ctx = Depends(context)):
    if ctx.session is None or not ctx.session.pending_totp:
        return RedirectResponse("/login", 303)
    user = ctx.user
    ok = security.verify_totp(security.decrypt(user.totp_secret), code)
    if not ok:
        remaining = security.use_recovery_code(user.recovery_codes or [], code)
        if remaining is not None:
            user.recovery_codes = remaining
            ok = True
    if not ok:
        await asyncio.sleep(1)
        return render(request, "login.html", ctx, step="totp", scene=login_scene(), error=_("Неверный код."),
                      status_code=401)
    ctx.session.pending_totp = False
    await ctx.db.commit()
    return RedirectResponse("/", 303)


@router.post("/logout")
async def logout(request: Request, ctx: Ctx = Depends(context)):
    if ctx.session:
        await check_csrf(request, ctx)
        await ctx.db.execute(delete(Session).where(Session.id == ctx.session.id))
        await ctx.db.commit()
    response = RedirectResponse("/login", 303)
    response.delete_cookie(COOKIE)
    return response


@router.get("/password")
async def password_page(request: Request, ctx: Ctx = Depends(user_required)):
    return render(request, "password.html", ctx, error=None)


@router.post("/password")
async def change_password(request: Request, current: str = Form(""), new: str = Form(""), repeat: str = Form(""),
                          ctx: Ctx = Depends(user_required)):
    error = None
    if not security.verify_password(ctx.user.password_hash, current):
        error = _("Текущий пароль неверен.")
    elif new != repeat:
        error = _("Новые пароли не совпадают.")
    elif new == current:
        error = _("Новый пароль должен отличаться от текущего.")
    else:
        try:
            await accounts.set_password(ctx.db, ctx.user, new)
        except accounts.AccountError as e:
            error = str(e)
        except Exception as e:  # Navidrome unreachable: nothing changed
            error = _("Не удалось сменить пароль в Navidrome: {e}").format(e=e)
    if error:
        return render(request, "password.html", ctx, error=error, status_code=400)
    # every other session of this user ends
    await ctx.db.execute(delete(Session).where(Session.user_id == ctx.user.id, Session.id != ctx.session.id))
    await ctx.db.commit()
    return RedirectResponse("/", 303)
