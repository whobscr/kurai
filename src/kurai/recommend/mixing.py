"""How chaotic or familiar the music is: the parameters of «Моя волна» and of the generated playlists (the nightly mixes
and «Спросить»), server-wide, set on the «Волна» and «Плейлисты» tabs. One schema here serves the settings' defaults,
both tabs' forms, their checks and the code that picks the tracks; /docs/admin/mixing explains each one at length.

A profile is one set of values: «wave» or «playlists». A parameter belongs to the profiles it has a default in."""

import hashlib
import random
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.db.models import Listen, Track, User, WaveEvent
from kurai.i18n import _
from kurai.recommend.vectors import Candidate, adjust, arrange, compose, spread

PROFILES = {"wave": _("«Моя волна»"), "playlists": _("Плейлисты")}
# a resting artist let back in (others ran short): this many spreads lower, so after everyone else
REST_LAST = 10


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
          _("Треки, которые человек ещё ни разу не слушал, но близкие к его вкусу. 0 — только знакомое (если "
            "знакомого, что подходит, хватает)."),
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
          _("Доля волны, которая идёт за тем, что играет последние часы; остальное — по вкусу за всё время. 0.6 — "
            "60 % треков."),
          float, {"wave": 0.6}),
    Param("taste_weight", _("Вкус в «Спросить»"),
          _("Насколько запрос тянется к вкусу человека. Большой вес подменяет просьбу привычным."),
          float, {"playlists": 0.15}, ask=True),
    Param("track_rest_days", _("Пауза для трека, дней"),
          _("Трек, сыгранный за эти дни, не попадает в подборку — чтобы успеть по нему соскучиться."),
          float, {"wave": 7, "playlists": 7}, high=365),
    Param("artist_rest_hours", _("Пауза для исполнителя, ч"),
          _("Самое большее столько часов исполнитель, только что звучавший, не попадает в подборку (пока хватает "
            "других; если не хватает — возвращается, последним). Когда именно он вернётся — решает «Форма паузы». "
            "0 — без паузы."),
          float, {"wave": 6, "playlists": 24}, high=24 * 30),
    # ---- the advanced ones ----
    Param("artist_rest_shape", _("Форма паузы исполнителя"),
          _("Когда в пределах паузы исполнитель возвращается: 1 — в любой момент с равной вероятностью; 2 — чем "
            "ближе к концу, тем вероятнее (к середине паузы вернулась четверть); 8 и больше — почти все в самом "
            "конце."),
          float, {"wave": 2, "playlists": 2}, low=1, high=20, advanced=True),
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
          _("Насколько опускаются треки, похожие на пропущенные: 0 — никак, 1 — сильно (волна уходит от их звучания)."),
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
          _("Через столько минут трек весит вдвое меньше: чем меньше, тем больше волны — за последним, что звучало; "
            "чем больше — тем ровнее за всем сеансом."),
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


def returns_after(hours: float, shape: float, key: str) -> float:
    """When, within its pause of `hours`, an artist heard comes back: a point drawn once for `key` (the user, the
    artist and when they were last heard — the same at every rebuild, a new draw after the next play), with the
    chance of being back by a part x of the pause x^shape («Форма паузы исполнителя»: 1 — even, 2 — quadratic)."""
    draw = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / 2 ** 64
    return hours * draw ** (1 / max(shape, 1e-6))


async def resting_artists(db: AsyncSession, user: User, now: datetime, hours: float, shape: float = 2) -> set[str]:
    """The artists the user heard (played or skipped, any app) whose return within the pause («Пауза для
    исполнителя») hasn't come yet, in lower case."""
    if hours <= 0:
        return set()
    since = now - timedelta(hours=hours)
    last: dict[str, datetime] = {}
    for stmt in (select(Track.artist, func.max(WaveEvent.at)).join(WaveEvent, WaveEvent.track_id == Track.id)
                 .where(WaveEvent.user_id == user.id, WaveEvent.at >= since).group_by(Track.artist),
                 select(Track.artist, func.max(Listen.last_played)).join(Listen, Listen.track_id == Track.id)
                 .where(Listen.user_id == user.id, Listen.last_played >= since).group_by(Track.artist)):
        for artist, at in (await db.execute(stmt)).all():
            if artist and at and (artist.lower() not in last or at > last[artist.lower()]):
                last[artist.lower()] = at
    return {artist for artist, at in last.items()
            if (now - at).total_seconds() / 3600 < returns_after(hours, shape, f"{user.id}|{artist}|{at.isoformat()}")}


def _capacity(candidates: list[Candidate], per_artist: int) -> int:
    """How many tracks these can give with at most `per_artist` of each artist."""
    counts: dict[str, int] = {}
    for c in candidates:
        counts[c.artist.lower()] = counts.get(c.artist.lower(), 0) + 1
    return sum(min(n, per_artist) for n in counts.values())


def choose(pool: list[Candidate], near: list[Candidate], known: set[str], mix: Mix, size: int,
           resting: set[str] | None = None, rng: random.Random | None = None,
           chosen: list[Candidate] | None = None, away: list | None = None) -> list[Candidate]:
    """`size` more tracks after `chosen` (another part of the same version), not in playing order: from the candidates
    near the query (`pool`) and those of the «слушают вместе» artists (`near`). The artists resting («Пауза для
    исполнителя») are left out while the others can fill it — else let back in, last; randomness is added; then the
    shares — related, new (never played: not in `known`), known — each by MMR. Known takes only the better half of the
    pool (familiar tracks far from the query don't fill its share; they fill only what's missing). The tracks like
    the skipped ones (`away`, their vectors) are lowered by «Отталкивание от пропущенного». Each track says which part
    it came for (`kind`)."""
    resting = resting or set()
    awake = [c for c in pool if c.artist.lower() not in resting]
    awake_near = [c for c in near if c.artist.lower() not in resting]
    if _capacity(awake + awake_near, mix.artist_max) >= size:
        pool, near = awake, awake_near
    # the better half of the pool, the resting artists aside (they come back only to fill what's missing): what a
    # familiar track has to reach to count for the known share
    base = [c for c in pool if c.artist.lower() not in resting] or pool
    cut = statistics.median(c.relevance for c in base) if base else 0.0
    fits = {c.id for c in base if c.relevance >= cut}
    scale = spread(pool + near)
    push = mix.skip_push if away else 0.0
    pool = adjust(pool, mix.randomness, resting, REST_LAST, rng, scale, away, push)
    near = adjust(near, mix.randomness, resting, REST_LAST, rng, scale, away, push)
    new = [replace(c, kind="new") for c in pool if c.id not in known]
    old = [replace(c, kind="known") for c in pool if c.id in fits and c.id in known]
    rest = [replace(c, kind="known") for c in pool if c.id not in fits and c.id in known]
    # the related artists' tracks the user hasn't heard come first: that's what they're mixed in for
    near = [replace(c, kind="related") for c in sorted(near, key=lambda c: (c.id in known, -c.relevance))]
    n_related = round(size * mix.related_share) if near else 0
    n_new = round(size * mix.new_share)
    parts = [(near, n_related), (new, n_new), (old, max(0, size - n_related - n_new)), (rest, 0)]
    return compose(parts, size, mix.variety, mix.artist_max, chosen)


def assemble(pool: list[Candidate], near: list[Candidate], known: set[str], mix: Mix, size: int,
             resting: set[str] | None = None, rng: random.Random | None = None) -> list[Candidate]:
    """A mix from one query's candidates (`choose`), in playing order."""
    return arrange(choose(pool, near, known, mix, size, resting, rng), mix.smooth, mix.artist_gap)
