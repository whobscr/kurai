"""How chaotic or familiar the music is: the parameters of «Моя волна» and of the generated playlists (the nightly mixes
and «Спросить»), server-wide, set on the «Волна» and «Плейлисты» tabs. One schema here serves the settings' defaults,
both tabs' forms, their checks and the code that picks the tracks; /docs/admin/mixing explains each one at length.

A profile is one set of values: «wave» or «playlists». A parameter belongs to the profiles it has a default in."""

import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.db.models import Listen, Track, User, WaveEvent
from kurai.i18n import _
from kurai.recommend.vectors import Candidate, adjust, arrange, compose, spread

PROFILES = {"wave": _("«Моя волна»"), "playlists": _("Плейлисты")}


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    help: str  # one line on the tab; the docs page says the rest
    kind: type  # float | int | bool
    defaults: dict[str, Any]  # profile -> default; a profile without one doesn't have the parameter
    low: float = 0
    high: float = 1
    advanced: bool = False
    ask: bool = False  # «Спросить» uses it too (from «playlists»)
    anchor: str = field(default="")  # its section on the docs page

    def setting(self, profile: str) -> str:
        return f"mix.{profile}.{self.key}"

    @property
    def switch(self) -> bool:
        return self.kind is bool

    @property
    def bounds(self) -> str:
        """Its range, for the tab: «0–1»."""
        return f"{_number(self.low)}–{_number(self.high)}"


PARAMS: tuple[Param, ...] = (
    # ---- the basic ones: what the music is made of ----
    Param("new_share", _("Доля нового"),
          _("Треки, которые человек ещё ни разу не слушал, но близкие к его вкусу. 0 — только знакомое."),
          float, {"wave": 0.4, "playlists": 0.5}),
    Param("related_share", _("Доля «слушают вместе»"),
          _("Исполнители, которых в мире слушают те же люди, что и любимых исполнителей человека (ListenBrainz), — "
            "даже если звучат иначе: так в подборку приходят соседние жанры."),
          float, {"wave": 0.15, "playlists": 0.15}),
    Param("variety", _("Разнообразие"),
          _("0 — самые похожие на вкус треки подряд; 1 — каждый следующий как можно непохожее на уже выбранные."),
          float, {"wave": 0.35, "playlists": 0.35}, ask=True),
    Param("randomness", _("Случайность"),
          _("Сколько случая в выборе: 0 — всегда одно и то же при тех же данных; 1 — разброс размером со всю "
            "разницу между кандидатами."),
          float, {"wave": 0.15, "playlists": 0.1}, ask=True),
    Param("now_weight", _("«Сейчас» против «всегда»"),
          _("Насколько волна идёт за тем, что играет последние часы, а не за вкусом за всё время."),
          float, {"wave": 0.6}),
    Param("taste_weight", _("Вкус в «Спросить»"),
          _("Насколько запрос тянется к вкусу человека. Большой вес подменяет просьбу привычным."),
          float, {"playlists": 0.15}, ask=True),
    Param("track_rest_days", _("Пауза для трека, дней"),
          _("Трек, сыгранный за эти дни, не попадает в подборку — чтобы успеть по нему соскучиться."),
          float, {"wave": 7, "playlists": 7}, high=365),
    Param("artist_rest_hours", _("Пауза для исполнителя, ч"),
          _("Исполнитель, звучавший за эти часы, опускается в выборе (а не исключается: если хорошего больше нет, "
            "он вернётся)."),
          float, {"wave": 6, "playlists": 24}, high=24 * 30),
    # ---- the advanced ones ----
    Param("artist_rest_penalty", _("Сила паузы исполнителя"),
          _("Насколько опускается недавно звучавший исполнитель: 0 — никак, 1 — на всю разницу между кандидатами."),
          float, {"wave": 0.5, "playlists": 0.5}, advanced=True),
    Param("artist_max", _("Треков одного исполнителя в подборке"),
          _("Не больше стольких треков одного исполнителя в одной версии волны или в одном плейлисте."),
          int, {"wave": 2, "playlists": 2}, low=1, high=20, advanced=True, ask=True),
    Param("artist_gap", _("Между треками одного исполнителя"),
          _("Сколько других треков стоит между двумя треками одного исполнителя (если хватает других)."),
          int, {"wave": 5, "playlists": 4}, high=30, advanced=True, ask=True),
    Param("smooth", _("Плавные переходы"),
          _("Соседние треки звучат похоже. Выключено — порядок по тому, насколько трек подходит."),
          bool, {"wave": True, "playlists": True}, advanced=True, ask=True),
    Param("size", _("Треков в подборке"),
          _("В одной версии волны; в одном ночном миксе."),
          int, {"wave": 40, "playlists": 30}, low=10, high=200, advanced=True),
    Param("related_depth", _("Глубина «слушают вместе»"),
          _("1 — соседи любимых исполнителей; 2 — ещё и соседи соседей: шире, дальше от привычного."),
          int, {"wave": 1, "playlists": 1}, low=1, high=2, advanced=True),
    Param("skip_push", _("Отталкивание от пропущенного"),
          _("Насколько волна уходит от звучания пропущенных треков."),
          float, {"wave": 0.3}, advanced=True),
    Param("skip_share", _("Что считать пропуском"),
          _("Трек, сыгранный меньше этой доли своей длины, считается пропущенным."),
          float, {"wave": 0.5}, low=0.05, high=0.95, advanced=True),
    Param("artist_skip_ban", _("Убрать исполнителя после пропусков"),
          _("Столько пропусков одного исполнителя за «сейчас» — и он уходит из волны до конца сеанса. 0 — никогда."),
          int, {"wave": 2}, high=10, advanced=True),
    Param("session_hours", _("Что считается «сейчас», ч"),
          _("За сколько последних часов прослушивания и пропуски направляют волну."),
          float, {"wave": 3}, low=0.5, high=24, advanced=True),
    Param("session_half_life", _("Затухание «сейчас», мин"),
          _("Через столько минут трек весит вдвое меньше: чем меньше, тем резче волна следует за последним."),
          float, {"wave": 45}, low=5, high=24 * 60, advanced=True),
    Param("rebuild_plays", _("Обновлять после стольких треков"),
          _("Новая версия волны — после стольких дослушанных треков (после пропуска — сразу)."),
          int, {"wave": 3}, low=1, high=50, advanced=True),
    Param("rebuild_minutes", _("Обновлять не чаще, мин"),
          _("Не чаще одной версии волны за столько минут."),
          float, {"wave": 4}, high=120, advanced=True),
    Param("popular_every", _("Новичкам — популярный трек каждые"),
          _("Пока человек ничего не слушал: каждый N-й трек — из популярного в библиотеке. 0 — без них."),
          int, {"wave": 3}, high=20, advanced=True),
)
BY_KEY = {p.key: p for p in PARAMS}


def params(profile: str) -> list[Param]:
    return [p for p in PARAMS if profile in p.defaults]


def defaults() -> dict[str, Any]:
    """Every profile's parameters as settings (settings.py merges them into its defaults)."""
    return {p.setting(profile): value for p in PARAMS for profile, value in p.defaults.items()}


def fields(profile: str) -> dict[str, type]:
    """The settings the profile's tab may write, and their types."""
    return {p.setting(profile): p.kind for p in params(profile)}


def validate(profile: str, values: dict[str, Any]) -> str | None:
    """An error message for the first value out of its range, or None."""
    for p in params(profile):
        value = values.get(p.setting(profile))
        if value is None or p.kind is bool:
            continue
        if not p.low <= value <= p.high:
            return _("«{label}» — от {low} до {high}").format(label=p.label, low=_number(p.low),
                                                                high=_number(p.high))
    shares = [values.get(f"mix.{profile}.{k}", 0) or 0 for k in ("new_share", "related_share")]
    if sum(shares) > 1:
        return _("«Доля нового» и «Доля «слушают вместе»» вместе — не больше 1")
    return None


def _number(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


@dataclass(frozen=True)
class Mix:
    """One profile's values, read when a wave or a playlist is made; a parameter the profile lacks reads as the
    wave's default (it isn't used there)."""

    profile: str
    values: dict[str, Any]

    def __getattr__(self, key: str) -> Any:
        if key not in BY_KEY:
            raise AttributeError(key)
        p = BY_KEY[key]
        default = p.defaults.get(self.profile, next(iter(p.defaults.values())))
        value = self.values.get(p.setting(self.profile), default)
        return p.kind(value)


async def load(db: AsyncSession, profile: str) -> Mix:
    return Mix(profile, await settings.get_all(db))


def standard(profile: str) -> Mix:
    """The defaults (tests, and code without a database at hand)."""
    return Mix(profile, {})


async def resting_artists(db: AsyncSession, user: User, now: datetime, hours: float) -> set[str]:
    """The artists the user heard within `hours` (played or skipped, any app), in lower case: they rest («Пауза для
    исполнителя»)."""
    since = now - timedelta(hours=hours)
    events = select(Track.artist).join(WaveEvent, WaveEvent.track_id == Track.id).where(
        WaveEvent.user_id == user.id, WaveEvent.at >= since)
    played = select(Track.artist).join(Listen, Listen.track_id == Track.id).where(
        Listen.user_id == user.id, Listen.last_played >= since)
    return {a.lower() for a in (await db.execute(events.union(played))).scalars() if a}


def assemble(pool: list[Candidate], near: list[Candidate], known: set[str], mix: Mix, size: int,
             resting: set[str] | None = None, rng: random.Random | None = None) -> list[Candidate]:
    """A wave's version or a mix from the candidates near the query (`pool`) and those of the «слушают вместе»
    artists (`near`): the relevance weighed (resting artists lowered, randomness added), then the shares — related,
    new (never played: not in `known`), known — each chosen by MMR, and the whole put in playing order. Each track
    says which part it came for (`kind`)."""
    scale = spread(pool + near)
    pool = adjust(pool, mix.randomness, resting, mix.artist_rest_penalty, rng, scale)
    near = adjust(near, mix.randomness, resting, mix.artist_rest_penalty, rng, scale)
    new = [replace(c, kind="new") for c in pool if c.id not in known]
    old = [replace(c, kind="known") for c in pool if c.id in known]
    # the related artists' tracks the user hasn't heard come first: that's what they're mixed in for
    near = [replace(c, kind="related") for c in sorted(near, key=lambda c: (c.id in known, -c.relevance))]
    n_related = round(size * mix.related_share) if near else 0
    n_new = round(size * mix.new_share)
    parts = [(near, n_related), (new, n_new), (old, max(0, size - n_related - n_new))]
    return arrange(compose(parts, size, mix.variety, mix.artist_max), mix.smooth, mix.artist_gap)
