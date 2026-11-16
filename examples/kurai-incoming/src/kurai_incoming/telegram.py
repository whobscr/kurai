"""In Kurai's Telegram bot: «входящие» tells an admin how many albums wait for approval. Its filter is narrow, so
every other message goes on to Kurai's «Спросить»."""

from aiogram import Dispatcher, F
from aiogram.types import Message
from sqlalchemy import func, select

from kurai import db as database
from kurai.config import get_config
from kurai.i18n import _
from kurai.telegram.bot import user_of
from kurai_incoming.models import IncomingAlbum

HELP = _("«входящие» — сколько альбомов ждут одобрения (для администраторов).")


async def waiting() -> int:
    async with database.session() as db:
        return (await db.execute(select(func.count()).select_from(IncomingAlbum)
                                 .where(IncomingAlbum.status == "new"))).scalar_one()


def register(dp: Dispatcher) -> None:
    @dp.message(F.text.func(lambda t: t.strip().lower() == "входящие"))
    async def incoming(message: Message) -> None:
        user = await user_of(message.chat.id)
        if user is None or not user.is_admin:
            await message.answer(_("Входящие видят только администраторы Kurai."))
            return
        await message.answer(_("Ждут одобрения: {n}. {url}/admin/incoming").format(
            n=await waiting(), url=get_config().base_url))
