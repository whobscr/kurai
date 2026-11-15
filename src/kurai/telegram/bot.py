"""The Telegram bot: the same features as the web app without installing anything.

- /start <code>: links the chat to a Kurai account (the code comes from the personal cabinet; no password in a chat)
- plugins' handlers (e.g. ordering albums), tried first
- any other text: «Спросить» (a playlist or an answer, sent back when ready)

It runs inside the worker, restarts itself when its token or the gateway's settings change, and goes through the
outbound gateway (Telegram is blocked in some countries)."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy import select, update

from kurai import db as database
from kurai import outbound, plugins, settings
from kurai.db.models import User
from kurai.i18n import _
from kurai.jobs import queue

log = logging.getLogger(__name__)

LINK_VALID = timedelta(hours=1)
CODE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")


async def user_of(chat_id: int) -> User | None:
    """The active Kurai user linked to a chat (for plugins' handlers too)."""
    async with database.session() as db:
        return (await db.execute(select(User).where(User.telegram_chat_id == chat_id,
                                                    User.active.is_(True)))).scalar_one_or_none()


async def link(code: str, chat_id: int) -> User | None:
    """Links the chat to the account whose fresh code this is (from «Личный кабинет → Telegram»); None for an unknown
    or expired code. A chat linked to another account before moves to this one."""
    async with database.session() as db:
        user = (await db.execute(select(User).where(User.telegram_link_code == code,
                                                    User.active.is_(True)))).scalar_one_or_none()
        # codes made before 0.2.23 have no time: they're still taken
        if user is None or (user.telegram_link_at and datetime.now(UTC) - user.telegram_link_at > LINK_VALID):
            return None
        await db.execute(update(User).where(User.telegram_chat_id == chat_id, User.id != user.id)
                         .values(telegram_chat_id=None))
        user.telegram_chat_id, user.telegram_link_code, user.telegram_link_at = chat_id, None, None
        await db.commit()
        return user


NOT_LINKED = _("Этот чат не подключён к Kurai. В Kurai откройте «Личный кабинет → Telegram», нажмите «Подключить», "
               "затем откройте ссылку или пришлите сюда код со страницы.")
BAD_CODE = _("Код не подошёл или устарел (он действует час). Нажмите «Подключить» в «Личный кабинет → Telegram» ещё "
             "раз и пришлите новый код.")


def _linked(user: User) -> str:
    return _("Готово, {name}! Пишите, что хотите послушать.").format(name=user.display_name)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        code = (command.args or "").strip()
        if code:
            user = await link(code, message.chat.id)
            await message.answer(_linked(user) if user else BAD_CODE)
            return
        # a plain /start: some apps don't pass the link's code into a chat that is already open
        user = await user_of(message.chat.id)
        await message.answer(_("Этот чат уже подключён, {name}. Пишите, что хотите послушать.").format(
            name=user.display_name) if user else NOT_LINKED)

    for plugin in plugins.plugins():
        if plugin.telegram:
            plugin.telegram(dp)

    @dp.message(F.text)
    async def text(message: Message) -> None:
        body = message.text.strip()
        user = await user_of(message.chat.id)
        if user is None:
            linked = await link(body, message.chat.id) if CODE.match(body) else None
            await message.answer(_linked(linked) if linked else NOT_LINKED)
            return
        if body.startswith("/"):
            await message.answer(_("Таких команд нет: просто напишите, что хотите послушать или о чём спросить."))
            return
        async with database.session() as db:
            await queue.enqueue(db, "ask", {"text": body[:1000], "reply_to": message.chat.id}, user_id=user.id,
                                priority=10)
        await message.answer(_("Работаю над этим — пришлю, когда будет готово."))

    return dp


_bot: Bot | None = None


def current_bot() -> Bot | None:
    """The running bot, for sending messages outside a chat's handler; None while it's off."""
    return _bot


def session_for(route: outbound.Route) -> AiohttpSession:
    """aiogram's session through the gateway's route. aiogram doesn't know «socks5h://» (it refuses the URL), but
    always resolves names through a SOCKS proxy itself (rdns), so «socks5://» means the same to it. The gateway's DNS
    server is given to its connector (aiogram has no option for it)."""
    session = AiohttpSession(proxy=route.proxy.replace("socks5h://", "socks5://", 1)) if route.proxy \
        else AiohttpSession()
    if route.dns:
        session._connector_init["resolver"] = outbound.resolver(route.dns)
    return session


async def run_bot() -> None:
    """The bot, kept going: whatever goes wrong with it (Telegram, the proxy, a bad setting) is logged and tried again
    in a minute, and never stops the worker it runs in (the jobs go on)."""
    while True:
        try:
            await _run_bot()
        except Exception:
            log.exception("telegram bot failed; trying again in a minute")
            await asyncio.sleep(60)


async def _run_bot() -> None:
    """Polls Telegram while the bot is on; restarts on a change of its token or the gateway's settings."""
    global _bot
    while True:
        async with database.session() as db:
            values = await settings.get_all(db)
        if not (values["telegram.enabled"] and values["telegram.token"]):
            await asyncio.sleep(60)
            continue
        route = outbound.route_from(values, "telegram")
        signature = (values["telegram.token"], route)
        session = session_for(route)
        _bot = Bot(values["telegram.token"], session=session)
        dp = build_dispatcher()
        polling = asyncio.create_task(dp.start_polling(_bot, handle_signals=False))
        try:
            while not polling.done():
                await asyncio.sleep(60)
                async with database.session() as db:
                    values = await settings.get_all(db)
                now = (values["telegram.token"], outbound.route_from(values, "telegram"))
                if now != signature or not values["telegram.enabled"]:
                    await dp.stop_polling()
                    break
        except Exception:
            log.exception("telegram bot")
        finally:
            await _bot.session.close()
            _bot = None
        if polling.done() and polling.exception():
            log.warning("telegram polling stopped: %s", polling.exception())
            await asyncio.sleep(60)
