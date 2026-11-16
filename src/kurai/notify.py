"""Telling a user something: always in the app's notification list, plus Telegram and web push where the user turned
them on. Delivery problems are logged, never raised: a notification must not fail the job that sends it."""

import json
import logging
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import outbound, settings
from kurai.config import get_config
from kurai.db.models import Notification, PushSubscription, User
from kurai.i18n import _

log = logging.getLogger(__name__)


async def notify(db: AsyncSession, user_id: int, text: str, link: str = "", telegram: bool = True) -> None:
    """`telegram=False`: not to Telegram (e.g. the answer goes to the chat that asked anyway)."""
    db.add(Notification(user_id=user_id, text=text, link=link))
    await db.commit()
    user = await db.get(User, user_id)
    if user is None:
        return
    values = await settings.get_all(db)
    if telegram and user.notify_telegram and user.telegram_chat_id and _telegram_on(values):
        await _telegram(values, user.telegram_chat_id, text, link)
    if user.notify_push and values["webpush.private_key"]:
        await _push(db, values, user_id, text, link)


async def notify_admins(db: AsyncSession, text: str, link: str = "") -> None:
    for admin in (await db.execute(select(User).where(User.role == "admin", User.active.is_(True)))).scalars():
        await notify(db, admin.id, text, link)


async def send_telegram(db: AsyncSession, chat_id: int, text: str, link: str = "") -> None:
    """A message to one Telegram chat (e.g. the answer to what was asked in it), whatever the user's switches."""
    values = await settings.get_all(db)
    if _telegram_on(values):
        await _telegram(values, chat_id, text, link)


def _telegram_on(values: dict) -> bool:
    return bool(values["telegram.enabled"] and values["telegram.token"])


async def _telegram(values: dict, chat_id: int, text: str, link: str) -> None:
    body = {"chat_id": chat_id, "text": text + (f"\n{get_config().base_url}{link}" if link else "")}
    try:
        async with outbound.session_for_route(outbound.route_from(values, "telegram"), 30) as s, \
                s.post(f"https://api.telegram.org/bot{values['telegram.token']}/sendMessage", json=body) as resp:
            if resp.status >= 400:
                log.warning("telegram: HTTP %s", resp.status)
    except Exception as e:
        log.warning("telegram: %s", e)


async def _push(db: AsyncSession, values: dict, user_id: int, text: str, link: str) -> None:
    subs = (await db.execute(select(PushSubscription).where(PushSubscription.user_id == user_id))).scalars().all()
    for error in await _send_push(db, values, [(s.endpoint, s.keys) for s in subs], text, link):
        log.warning("web push: %s", error)


async def push_test(db: AsyncSession, endpoint: str, keys: dict) -> str | None:
    """A test notification to one device (its subscription just made): the error to show, or None when the push
    service took it."""
    values = await settings.get_all(db)
    if not values["webpush.private_key"]:
        return "no VAPID key"
    errors = await _send_push(db, values, [(endpoint, keys)], _("Push-уведомления работают на этом устройстве."), "/me")
    return errors[0] if errors else None


async def _send_push(db: AsyncSession, values: dict, subs: list[tuple[str, dict]], text: str, link: str) -> list[str]:
    """Web push (VAPID) to these subscriptions; the errors. pywebpush sends with requests; the gateway's proxy is
    given to its session. A subscription the browser dropped is removed."""
    import asyncio

    import requests
    from pywebpush import WebPushException, webpush

    route = outbound.route_from(values, "webpush")
    payload = json.dumps({"title": "Kurai", "body": text, "url": link or "/"})
    subject = f"mailto:kurai@{urlsplit(get_config().base_url).hostname or 'localhost'}"
    errors = []
    with requests.Session() as session:
        if route.proxy:
            session.proxies = {"https": route.proxy, "http": route.proxy}
        for endpoint, keys in subs:
            try:
                await asyncio.to_thread(
                    webpush, {"endpoint": endpoint, "keys": keys}, payload,
                    vapid_private_key=values["webpush.private_key"], vapid_claims={"sub": subject},
                    requests_session=session, timeout=20)
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):  # the browser dropped it
                    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))
                    await db.commit()
                errors.append(str(e))
            except Exception as e:
                errors.append(str(e))
    return errors
