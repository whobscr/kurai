"""The Telegram bot: the same features as the web app without installing anything.

- /start <code>: links the chat to a Kurai account (the code comes from the personal cabinet; no password in a chat)
- plugins' handlers (e.g. ordering albums), tried first
- any other text: «Спросить» (a playlist or an answer, sent back when ready)

It runs inside the worker, restarts itself when its token or the gateway's settings change, and goes through the
outbound gateway (Telegram is blocked in some countries)."""

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy import select

from kurai import db as database
from kurai import outbound, plugins, settings
from kurai.db.models import User
from kurai.i18n import _
from kurai.jobs import queue

log = logging.getLogger(__name__)

async def user_of(chat_id: int) -> User | None:
    """The active Kurai user linked to a chat (for plugins' handlers too)."""
    async with database.session() as db:
        return (await db.execute(select(User).where(User.telegram_chat_id == chat_id,
                                                    User.active.is_(True)))).scalar_one_or_none()


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        code = (command.args or "").strip()
        async with database.session() as db:
            user = (await db.execute(select(User).where(User.telegram_link_code == code))).scalar_one_or_none() \
                if code else None
            if user is None:
                await message.answer(_("Откройте «Личный кабинет → Telegram» в Kurai и нажмите «Подключить»."))
                return
            user.telegram_chat_id = message.chat.id
            user.telegram_link_code = None
            await db.commit()
        await message.answer(_("Готово, {name}! Пишите, что хотите послушать.").format(name=user.display_name))

    for plugin in plugins.plugins():
        if plugin.telegram:
            plugin.telegram(dp)

    @dp.message(F.text)
    async def text(message: Message) -> None:
        user = await user_of(message.chat.id)
        if user is None:
            await message.answer(_("Этот чат не подключён к Kurai: «Личный кабинет → Telegram»."))
            return
        async with database.session() as db:
            await queue.enqueue(db, "ask", {"text": message.text.strip()[:1000]}, user_id=user.id, priority=10)
        await message.answer(_("Работаю над этим — пришлю, когда будет готово."))

    return dp


_bot: Bot | None = None


def current_bot() -> Bot | None:
    """The running bot, for sending messages outside a chat's handler; None while it's off."""
    return _bot


async def run_bot() -> None:
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
        session = AiohttpSession(proxy=route.proxy) if route.proxy else AiohttpSession()
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
