"""«Личный кабинет»: the user's look (accent colour, light / dark), the optional TOTP with recovery codes, Telegram,
notifications and web push."""

import re

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from kurai import security, settings
from kurai.db.models import PushSubscription
from kurai.i18n import _
from kurai.web.deps import Ctx, user_required
from kurai.web.templating import render

router = APIRouter(prefix="/me")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


async def _page(request: Request, ctx: Ctx, **values):
    values.setdefault("message", None)
    values.setdefault("new_codes", None)
    values.setdefault("totp_setup", None)
    bot = None
    if ctx.user.telegram_link_code:
        bot = await _bot_username(ctx)
    return render(request, "me.html", ctx, bot=bot, push_key=await settings.get(ctx.db, "webpush.public_key"),
                  **values)


async def _bot_username(ctx: Ctx) -> str | None:
    """The bot's @name for the t.me link (asked from Telegram through the gateway)."""
    from kurai import outbound

    values = await settings.get_all(ctx.db)
    if not values["telegram.token"]:
        return None
    try:
        async with outbound.session_for_route(outbound.route_from(values, "telegram"), 15) as s, \
                s.get(f"https://api.telegram.org/bot{values['telegram.token']}/getMe") as resp:
            return (await resp.json()).get("result", {}).get("username")
    except Exception:
        return None


@router.get("")
async def page(request: Request, ctx: Ctx = Depends(user_required)):
    return await _page(request, ctx)


@router.post("/look")
async def look(accent: str = Form(""), mode: str = Form("auto"), ctx: Ctx = Depends(user_required)):
    ctx.user.accent_color = accent if HEX.match(accent) else None
    ctx.user.theme_mode = mode if mode in {"light", "dark", "auto"} else "auto"
    await ctx.db.commit()
    return RedirectResponse("/me", 303)


@router.post("/mode")
async def mode(mode: str = Body(embed=True), ctx: Ctx = Depends(user_required)):
    """The light / dark switch in every page's band (the page already changed; this remembers it)."""
    ctx.user.theme_mode = mode if mode in {"light", "dark", "auto"} else "auto"
    await ctx.db.commit()
    return Response(status_code=204)


@router.post("/totp/start")
async def totp_start(request: Request, ctx: Ctx = Depends(user_required)):
    secret = security.new_totp_secret()
    ctx.user.totp_secret = security.encrypt(secret)
    await ctx.db.commit()
    return await _page(request, ctx, totp_setup={"secret": secret,
                                                 "uri": security.totp_uri(secret, ctx.user.username)})


@router.post("/totp/confirm")
async def totp_confirm(request: Request, code: str = Form(""), ctx: Ctx = Depends(user_required)):
    secret = security.decrypt(ctx.user.totp_secret)
    if not security.verify_totp(secret, code):
        return await _page(request, ctx, message=_("Код не подошёл — попробуйте ещё раз."),
                           totp_setup={"secret": secret, "uri": security.totp_uri(secret, ctx.user.username)})
    codes, hashes = security.new_recovery_codes()
    ctx.user.totp_enabled = True
    ctx.user.recovery_codes = hashes
    await ctx.db.commit()
    return await _page(request, ctx, new_codes=codes, message=_("Двухфакторный вход включён."))


@router.post("/totp/disable")
async def totp_disable(request: Request, password: str = Form(""), ctx: Ctx = Depends(user_required)):
    if not security.verify_password(ctx.user.password_hash, password):
        return await _page(request, ctx, message=_("Неверный пароль."))
    ctx.user.totp_enabled = False
    ctx.user.totp_secret = None
    ctx.user.recovery_codes = []
    await ctx.db.commit()
    return await _page(request, ctx, message=_("Двухфакторный вход выключен."))


@router.post("/telegram/link")
async def telegram_link(request: Request, ctx: Ctx = Depends(user_required)):
    ctx.user.telegram_link_code = security.random_token(12)[:16]
    await ctx.db.commit()
    return await _page(request, ctx)


@router.post("/telegram/unlink")
async def telegram_unlink(ctx: Ctx = Depends(user_required)):
    ctx.user.telegram_chat_id = None
    ctx.user.telegram_link_code = None
    await ctx.db.commit()
    return RedirectResponse("/me", 303)


@router.post("/notifications")
async def notifications(push: str = Form(""), telegram: str = Form(""), ctx: Ctx = Depends(user_required)):
    ctx.user.notify_push = push == "on"
    ctx.user.notify_telegram = telegram == "on"
    await ctx.db.commit()
    return RedirectResponse("/me", 303)


@router.post("/push")
async def push_subscribe(request: Request, ctx: Ctx = Depends(user_required)):
    """The browser's push subscription (sent by sw-register.js)."""
    sub = await request.json()
    stmt = insert(PushSubscription).values(endpoint=sub["endpoint"], user_id=ctx.user.id, keys=sub["keys"])
    await ctx.db.execute(stmt.on_conflict_do_update(index_elements=[PushSubscription.endpoint],
                                                    set_={"user_id": ctx.user.id, "keys": sub["keys"]}))
    await ctx.db.commit()
    return {"ok": True}


@router.post("/push/delete")
async def push_unsubscribe(request: Request, ctx: Ctx = Depends(user_required)):
    sub = await request.json()
    await ctx.db.execute(delete(PushSubscription).where(PushSubscription.endpoint == sub.get("endpoint", ""),
                                                        PushSubscription.user_id == ctx.user.id))
    await ctx.db.commit()
    return {"ok": True}
