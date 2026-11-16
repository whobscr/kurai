"""The system settings: one table of keys, each with a default here or in a plugin. The system settings page writes
them; everything else reads them through `get` / `get_all`. Secret values (API keys, tokens) are stored encrypted."""

from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import plugins, security
from kurai.config import get_config
from kurai.db.models import Setting
from kurai.i18n import _
from kurai.recommend import mixing

# every task that may ask a model; «Модель думает» is on by default for the night ones (time is plentiful, answers are
# better) and off where a person waits
LLM_TASKS = {
    "name_playlist": {"label": _("Названия и описания плейлистов"), "night": True},
    "starter_profiles": {"label": _("Описания стартовых профилей вкуса"), "night": True},
    "ask": {"label": _("Запросы «Спросить»"), "night": False},
    "explain": {"label": _("«Почему этот трек»"), "night": False},
}

# the subsystems whose outgoing connections can go through the outbound gateway (outbound.py); plugins add theirs
CORE_SUBSYSTEMS = {
    "telegram": _("Telegram-бот"),
    "llm": _("Внешние модели (облачные API)"),
    "webpush": _("Push-уведомления"),
    "models": _("Загрузка моделей анализа звука"),
    "public_data": _("ListenBrainz (популярность и похожие исполнители)"),
    "musicbrainz": _("MusicBrainz (теги исполнителей)"),
}

CORE_SECRET_KEYS = {"llm.api_key", "embed.api_key", "telegram.token", "outbound.password", "navidrome.admin_password",
                    "webpush.private_key", "listenbrainz.token"}


@lru_cache
def outbound_subsystems() -> dict[str, str]:
    return {**CORE_SUBSYSTEMS, **plugins.merged("outbound")}


@lru_cache
def secret_keys() -> frozenset[str]:
    return frozenset(CORE_SECRET_KEYS).union(*(p.secret_keys for p in plugins.installed()))


def _defaults() -> dict[str, Any]:
    c = get_config()
    return {
        # the look
        "ui.default_accent": "#6750a4",
        # the night window for model and analysis work (local time, HH:MM); may cross midnight
        "night.start": "06:00",
        "night.end": "10:00",
        # models: the text provider and the embedding provider are separate (Claude has no embeddings)
        "llm.provider": "openai_compatible",  # openai_compatible | ollama | anthropic
        "llm.server_type": "llama_cpp",  # llama_cpp | vllm | openai | openrouter | other (openai_compatible only)
        "llm.base_url": c.llm_base_url,
        "llm.api_key": c.llm_api_key,
        "llm.model": c.llm_model,
        "llm.timeout": 900,
        "llm.parallel": 2,  # «Параллельные запросы к модели»: how many Kurai sends at once
        "llm.enabled": True,
        "embed.provider": "openai_compatible",  # openai_compatible | ollama
        "embed.base_url": c.embed_base_url,
        "embed.api_key": c.embed_api_key,
        "embed.model": c.embed_model,
        **{f"think.{task}": info["night"] for task, info in LLM_TASKS.items()},
        # the outbound gateway: one proxy, a checkbox per subsystem, an optional DNS server, never a direct fallback
        "outbound.proxy": c.outbound_proxy,
        "outbound.user": "",
        "outbound.password": "",
        "outbound.dns": "",
        **{f"outbound.via.{name}": bool(c.outbound_proxy) for name in outbound_subsystems()},
        # the audio analysis (the `audio` container): off until it runs; how much the sound counts
        "audio.enabled": False,
        "audio.url": c.audio_url,
        "audio.clap": True,  # CLAP too: search by a description of the sound; doubles the time per track
        "audio.mix_weight": 0.5,  # mixes: the sound's share against the tags
        "audio.ask_weight": 0.5,  # «Спросить»: the sound's share (CLAP and the sound) against the tags
        # the nightly mixes per user, and how long the generated playlists stay (days; 0: for good); a liked one stays
        "playlists.per_night": 3,
        "playlists.keep_days": 14,
        "ask.keep_days": 0,
        # Telegram (its own bot, the token is entered here)
        "telegram.enabled": bool(c.telegram_bot_token),
        "telegram.token": c.telegram_bot_token,
        # Navidrome: the admin account Kurai creates for itself on first start
        "navidrome.admin_user": "kurai",
        "navidrome.admin_password": "",
        # web push (VAPID keys made on first start)
        "webpush.public_key": "",
        "webpush.private_key": "",
        # public listening data (integrations/public_data.py): ListenBrainz's popularity needs a user's token;
        # MusicBrainz asks for a contact in every request
        "listenbrainz.token": "",
        "musicbrainz.contact": "",
        # «Моя волна»: a playlist that follows what each user is listening to (Navidrome's «now playing»)
        "wave.enabled": True,
        # how the wave and the playlists mix the music (recommend/mixing.py)
        **mixing.defaults(),
        # the plugins switched off on «Плагины» (their names)
        plugins.DISABLED_KEY: [],
    }


@lru_cache
def defaults() -> dict[str, Any]:
    values = _defaults()
    # a switched-off plugin's settings too: kept as they are, ready when it's on again
    for p in plugins.installed():
        values.update(p.settings())
    return values


async def get_all(db: AsyncSession) -> dict[str, Any]:
    values = dict(defaults())
    for row in (await db.execute(select(Setting))).scalars():
        values[row.key] = security.decrypt(row.value) if row.key in secret_keys() and row.value else row.value
    return values


async def get(db: AsyncSession, key: str) -> Any:
    if key not in defaults():
        raise KeyError(key)
    row = await db.get(Setting, key)
    if row is None:
        return defaults()[key]
    return security.decrypt(row.value) if key in secret_keys() and row.value else row.value


async def put(db: AsyncSession, values: dict[str, Any]) -> None:
    """Saves the given keys (unknown keys are refused). Commit is the caller's."""
    for key, value in values.items():
        if key not in defaults():
            raise KeyError(key)
        stored = security.encrypt(value) if key in secret_keys() and value else value
        await db.execute(insert(Setting).values(key=key, value=stored)
                         .on_conflict_do_update(index_elements=[Setting.key], set_={"value": stored}))


async def unreadable(db: AsyncSession) -> list[str]:
    """The secrets stored but not readable: encrypted with another KURAI_SECRET_KEY."""
    rows = (await db.execute(select(Setting).where(Setting.key.in_(secret_keys())))).scalars()
    return [row.key for row in rows if row.value and not security.decrypt(row.value)]
