"""«Личный кабинет»: the user's look (accent colour, light / dark), what they like to listen to, the optional TOTP with
recovery codes, Telegram, notifications and web push, «Любимое», and forgetting what they listened to."""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from kurai import accounts, notify, outbound, security, settings
from kurai.db.models import Playlist, PushSubscription
from kurai.i18n import _
from kurai.integrations.navidrome import NavidromeError
from kurai.jobs import queue
from kurai.recommend import playlists, start
from kurai.web.deps import Ctx, user_required
from kurai.web.templating import render

router = APIRouter(prefix="/me")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
STYLES_KEPT = 12


async def _page(request: Request, ctx: Ctx, **values):
    values.setdefault("message", None)
    values.setdefault("new_codes", None)
    values.setdefault("totp_setup", None)
    bot = None
    if ctx.user.telegram_link_code:
        bot = await _bot_username(ctx)
    favorites = (await ctx.db.execute(select(Playlist).where(Playlist.user_id == ctx.user.id,
                                                             Playlist.kind == playlists.FAVORITES))).scalars().first()
    return render(request, "me.html", ctx, bot=bot, push_key=await settings.get(ctx.db, "webpush.public_key"),
                  styles=await start.library_styles(ctx.db), favorites=favorites,
                  favorites_sizes=playlists.FAVORITES_SIZES, **values)


async def _bot_username(ctx: Ctx) -> str | None:
    """The bot's @name for the t.me link (asked from Telegram through the gateway)."""
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
    step = security.totp_step(secret, code)
    if step is None:
        return await _page(request, ctx, message=_("Код не подошёл — попробуйте ещё раз."),
                           totp_setup={"secret": secret, "uri": security.totp_uri(secret, ctx.user.username)})
    codes, hashes = security.new_recovery_codes()
    ctx.user.totp_enabled = True
    ctx.user.totp_last_step = step
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
    ctx.user.telegram_link_at = datetime.now(UTC)
    await ctx.db.commit()
    return await _page(request, ctx)


@router.post("/telegram/unlink")
async def telegram_unlink(ctx: Ctx = Depends(user_required)):
    ctx.user.telegram_chat_id = ctx.user.telegram_link_code = ctx.user.telegram_link_at = None
    await ctx.db.commit()
    return RedirectResponse("/me", 303)


@router.post("/start")
async def start_styles(request: Request, ctx: Ctx = Depends(user_required)):
    """What the user likes, before (or instead of) listening: styles of the library and their own words; «Пропустить»
    saves only that they were asked. The wave is made anew from the answers."""
    form = await request.form()
    back = RedirectResponse("/me#start" if form.get("back") == "me" else "/", 303)
    if not form.get("skip"):
        known = set(await start.library_styles(ctx.db, limit=200))
        ctx.user.start_styles = [s for s in form.getlist("style") if s in known][:STYLES_KEPT]
        ctx.user.start_text = str(form.get("text", "")).strip()[:500]
        # stored once, here — not re-drawn at random nightly; re-answers recompute it
        taste = await start.first_taste(ctx.db, ctx.user, recompute=True)
        if taste is not None:
            await start.store(ctx.db, ctx.user, taste)
        else:
            await start.clear(ctx.db, ctx.user)
        await queue.enqueue(ctx.db, "wave", {"retaste": True}, user_id=ctx.user.id, priority=20,
                            dedupe_key=f"wave-start:{ctx.user.id}")
    ctx.user.start_asked_at = datetime.now(UTC)
    await ctx.db.commit()
    return back


@router.post("/favorites")
async def favorites(request: Request, size: int = Form(50), nightly: str = Form(""), build: str = Form(""),
                    ctx: Ctx = Depends(user_required)):
    """«Любимое»: how many tracks and whether the night makes it anew; «Собрать» also makes it now."""
    low, high = playlists.FAVORITES_SIZES
    ctx.user.favorites_size = min(max(size, low), high)
    ctx.user.favorites_nightly = nightly == "on"
    await ctx.db.commit()
    if not build:
        return RedirectResponse("/me#favorites", 303)
    try:
        count = await playlists.build_favorites(ctx.db, ctx.user)
    except NavidromeError as e:
        await ctx.db.rollback()
        return await _page(request, ctx, message=_("Не получилось: {e}. Попробуйте ещё раз.").format(e=e))
    if not count:
        return await _page(request, ctx, message=_("Пока не из чего собрать: поставьте ♥ или послушайте что-нибудь."))
    return await _page(request, ctx, message=_("«Любимое» собрано: {n} треков — на странице «Для вас» и в "
                                               "музыкальных приложениях.").format(n=count))


@router.post("/forget")
async def forget(request: Request, sure: str = Form(""), ctx: Ctx = Depends(user_required)):
    """«Забыть прослушивания» (accounts.forget_listens), once the user ticked that they understand what goes."""
    if sure != "on":
        return await _page(request, ctx, message=_("Поставьте галочку, что понимаете, что удалится."))
    try:
        await accounts.forget_listens(ctx.db, ctx.user)
    except NavidromeError as e:
        await ctx.db.rollback()
        return await _page(request, ctx, message=_("Не получилось: {e}. Попробуйте ещё раз.").format(e=e))
    return await _page(request, ctx, message=_("Прослушивания забыты. «Моя волна» собирается заново — из того, что "
                                               "вы указали в «Что вы любите слушать»."))


@router.post("/notifications")
async def notifications(push: str = Form(""), telegram: str = Form(""), ctx: Ctx = Depends(user_required)):
    ctx.user.notify_push = push == "on"
    ctx.user.notify_telegram = telegram == "on"
    await ctx.db.commit()
    return RedirectResponse("/me", 303)


def _subscription(sub: Any) -> tuple[str, dict] | None:
    """A browser's push subscription: an https address of a push service on the internet and its two keys (anything
    else would make Kurai post to an address of the user's choice, local services too)."""
    if not isinstance(sub, dict) or not isinstance(sub.get("keys"), dict):
        return None
    endpoint, keys = str(sub.get("endpoint", "")), sub["keys"]
    if urlsplit(endpoint).scheme != "https" or outbound.is_local_url(endpoint):
        return None
    if not all(isinstance(keys.get(k), str) and keys[k] for k in ("p256dh", "auth")):
        return None
    return endpoint, {"p256dh": keys["p256dh"], "auth": keys["auth"]}


@router.post("/push")
async def push_subscribe(request: Request, ctx: Ctx = Depends(user_required)):
    """The browser's push subscription (sent by kurai.js): kept, push switched on for the user, and a test
    notification sent to this device at once, so the page can say whether it really works."""
    try:
        parsed = _subscription(await request.json())
    except ValueError:
        parsed = None
    if parsed is None:
        raise HTTPException(400, _("Не подписка на push"))
    endpoint, keys = parsed
    stmt = insert(PushSubscription).values(endpoint=endpoint, user_id=ctx.user.id, keys=keys)
    await ctx.db.execute(stmt.on_conflict_do_update(index_elements=[PushSubscription.endpoint],
                                                    set_={"user_id": ctx.user.id, "keys": keys}))
    ctx.user.notify_push = True
    await ctx.db.commit()
    error = await notify.push_test(ctx.db, endpoint, keys)
    return {"ok": error is None, "detail": error or ""}
