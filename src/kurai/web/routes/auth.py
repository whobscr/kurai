"""Logging in: password, then the TOTP code when the user turned it on (or a recovery code), then a forced password
change for the first admin. Every failure gets the same short sentence: nothing tells whether the name exists."""

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from kurai import accounts, security
from kurai.db.models import Session, User
from kurai.i18n import _
from kurai.web.deps import COOKIE, Ctx, check_csrf, context, set_session_cookie, user_required
from kurai.web.templating import login_scene, render

router = APIRouter()
FAILED = _("Неверное имя или пароль.")
_DUMMY_HASH = security.hash_password("kurai: not a password")
# guessing: a few tries per name from one address, a few more per name in all, a few codes per login
LOGIN_TRIES = security.Throttle(5, 15 * 60)
NAME_TRIES = security.Throttle(20, 15 * 60)
CODE_TRIES = 5
_code_failures = security.Throttle(CODE_TRIES, 15 * 60)


@router.get("/login")
async def login_page(request: Request, ctx: Ctx = Depends(context)):
    if ctx.user and ctx.session and not ctx.session.pending_totp:
        return RedirectResponse("/", 303)
    step = "totp" if ctx.session and ctx.session.pending_totp else "password"
    return render(request, "login.html", ctx, step=step, scene=login_scene(), error=None)


@router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), ctx: Ctx = Depends(context)):
    name = username.strip().lower()
    pair = f"{request.client.host if request.client else ''}|{name}"
    if LOGIN_TRIES.blocked(pair) or NAME_TRIES.blocked(name):
        await asyncio.sleep(1)
        return render(request, "login.html", ctx, step="password", scene=login_scene(), error=FAILED, status_code=401)
    user = (await ctx.db.execute(select(User).where(User.username == name))).scalar_one_or_none()
    # an unknown name costs the same hashing as a known one: the time doesn't tell which names exist
    known = user is not None and user.active
    if not security.verify_password(user.password_hash if known else _DUMMY_HASH, password) or not known:
        LOGIN_TRIES.fail(pair)
        NAME_TRIES.fail(name)
        await asyncio.sleep(1)  # slows guessing down
        return render(request, "login.html", ctx, step="password", scene=login_scene(), error=FAILED, status_code=401)
    LOGIN_TRIES.clear(pair)
    token = security.random_token()
    ctx.db.add(Session(id=security.token_id(token), user_id=user.id, pending_totp=user.totp_enabled,
                       csrf_token=security.random_token(24)))
    await ctx.db.commit()
    response = RedirectResponse("/login" if user.totp_enabled else "/", 303)
    set_session_cookie(response, token, request)
    return response


@router.post("/login/totp")
async def login_totp(request: Request, code: str = Form(""), ctx: Ctx = Depends(context)):
    if ctx.session is None or not ctx.session.pending_totp:
        return RedirectResponse("/login", 303)
    user = ctx.user
    step = security.totp_step(security.decrypt(user.totp_secret), code, user.totp_last_step)
    ok = step is not None
    if ok:
        user.totp_last_step = step
    elif security.looks_like_recovery_code(code):
        remaining = security.use_recovery_code(user.recovery_codes or [], code)
        if remaining is not None:
            user.recovery_codes = remaining
            ok = True
    if not ok:
        await asyncio.sleep(1)
        _code_failures.fail(ctx.session.id)
        if _code_failures.blocked(ctx.session.id):  # too many codes: the password again
            _code_failures.clear(ctx.session.id)
            await ctx.db.execute(delete(Session).where(Session.id == ctx.session.id))
            await ctx.db.commit()
            response = render(request, "login.html", None, step="password", scene=login_scene(), error=FAILED,
                              status_code=401)
            response.delete_cookie(COOKIE)
            return response
        return render(request, "login.html", ctx, step="totp", scene=login_scene(), error=_("Неверный код."),
                      status_code=401)
    _code_failures.clear(ctx.session.id)
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
    await accounts.end_sessions(ctx.db, ctx.user.id, except_id=ctx.session.id)  # every other session ends
    return RedirectResponse("/", 303)
