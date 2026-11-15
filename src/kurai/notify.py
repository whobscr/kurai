"""Telling a user something: always in the app's notification list, plus Telegram and web push where the user turned
them on. Delivery problems are logged, never raised: a notification must not fail the job that sends it."""

import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import outbound, settings
from kurai.config import get_config
from kurai.db.models import Notification, PushSubscription, User

log = logging.getLogger(__name__)


async def notify(db: AsyncSession, user_id: int, text: str, link: str = "") -> None:
    db.add(Notification(user_id=user_id, text=text, link=link))
    await db.commit()
    user = await db.get(User, user_id)
    if user is None:
        return
    values = await settings.get_all(db)
    if user.notify_telegram and user.telegram_chat_id and values["telegram.enabled"] and values["telegram.token"]:
        await _telegram(values, user.telegram_chat_id, text, link)
    if user.notify_push and values["webpush.private_key"]:
        await _push(db, values, user_id, text, link)


async def notify_admins(db: AsyncSession, text: str, link: str = "") -> None:
    for admin in (await db.execute(select(User).where(User.role == "admin", User.active.is_(True)))).scalars():
        await notify(db, admin.id, text, link)


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
    """Web push (VAPID). pywebpush sends with requests; the gateway's proxy is given to its session."""
    import asyncio

    import requests
    from pywebpush import WebPushException, webpush

    route = outbound.route_from(values, "webpush")
    session = requests.Session()
    if route.proxy:
        session.proxies = {"https": route.proxy, "http": route.proxy}
    payload = json.dumps({"title": "Kurai", "body": text, "url": link or "/"})
    for sub in (await db.execute(select(PushSubscription).where(PushSubscription.user_id == user_id))).scalars().all():
        try:
            await asyncio.to_thread(
                webpush, {"endpoint": sub.endpoint, "keys": sub.keys}, payload,
                vapid_private_key=values["webpush.private_key"],
                vapid_claims={"sub": f"mailto:kurai@{get_config().base_url.split('://')[-1]}"},
                requests_session=session, timeout=20)
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):  # the browser dropped it
                await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == sub.endpoint))
                await db.commit()
            else:
                log.warning("web push: %s", e)
        except Exception as e:
            log.warning("web push: %s", e)
