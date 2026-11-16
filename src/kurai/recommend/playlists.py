"""Playlists: the nightly mixes, «Спросить» (the model only plans the query; code picks the tracks) and «Почему этот
трек». Every playlist is written into Navidrome as the user's private playlist, so it appears in any music app."""

import logging
import random
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import llm, settings
from kurai.audio import client as audio_client
from kurai.audio.tags import describe, describe_set, fits
from kurai.db.models import Listen, Playlist, Taste, Track, TrackAudio, User
from kurai.i18n import _
from kurai.recommend import mixing, related, retrieval
from kurai.recommend.library import navidrome, user_account
from kurai.recommend.vectors import Candidate, adjust, arrange, blend, mmr_select, normalize

log = logging.getLogger(__name__)

# the generated playlists removed when older than their setting's days (0: kept), but the liked ones
KEEP_DAYS = {"nightly": "playlists.keep_days", "ask": "ask.keep_days"}
ASK_MAX_TRACKS = 60
NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
    "required": ["name", "description"],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["playlist", "answer"]},
        "answer": {"type": "string"},
        "mood_text": {"type": "string"},
        "sound_en": {"type": "string"},
        "seed_artists": {"type": "array", "items": {"type": "string"}},
        "exclude_artists": {"type": "array", "items": {"type": "string"}},
        "minutes": {"type": "integer"},
        "name": {"type": "string"},
        "bpm_min": {"type": "integer"},
        "bpm_max": {"type": "integer"},
        "energy": {"type": "string", "enum": ["any", "calm", "medium", "energetic"]},
        "vocals": {"type": "string", "enum": ["any", "instrumental", "vocal"]},
    },
    "required": ["intent", "answer", "mood_text", "sound_en", "seed_artists", "exclude_artists", "minutes", "name",
                 "bpm_min", "bpm_max", "energy", "vocals"],
    "additionalProperties": False,
}


async def taste_of(db: AsyncSession, user: User) -> dict[str, np.ndarray]:
    """The user's taste in each space it's known in (not centred)."""
    taste = await db.get(Taste, user.id)
    if taste is None:
        return {}
    # a taste of another embedding model (it changed since the night) can't be compared with the tracks
    same_model = taste.embedding_model == await settings.get(db, "embed.model")
    parts = {"text": taste.vector if same_model else None, "sound": taste.audio_vector}
    return {s: np.asarray(v, dtype=np.float32) for s, v in parts.items() if v is not None}


async def _sound_of(db: AsyncSession, ids: list[str]) -> list[TrackAudio]:
    return list((await db.execute(select(TrackAudio).where(TrackAudio.track_id.in_(ids),
                                                           TrackAudio.error.is_(None)))).scalars())


def _fit_minutes(chosen: list[Candidate], durations: dict[str, int], minutes: int) -> list[Candidate]:
    out, total = [], 0
    for c in chosen:
        if total >= minutes * 60:
            break
        out.append(c)
        total += durations.get(c.id, 240)
    return out


async def _durations(db: AsyncSession, ids: list[str]) -> dict[str, int]:
    return dict((await db.execute(select(Track.id, Track.duration).where(Track.id.in_(ids)))).all())


async def write(db: AsyncSession, user: User, playlist: Playlist) -> None:
    """Into Navidrome as the user's private playlist (the same one again when it has its id)."""
    account = user_account(user)
    if account is None:
        return
    playlist.navidrome_id = await navidrome().save_playlist(account, playlist.name, playlist.track_ids,
                                                            playlist.navidrome_id, playlist.description)


async def _name(db: AsyncSession, artists: list[str], hint: str) -> tuple[str, str]:
    """A short Russian name and description from the playlist's artists; a plain fallback without a model."""
    try:
        data = await llm.complete_json(
            db, "name_playlist",
            "Ты придумываешь короткие названия и описания музыкальных плейлистов. Отвечай по-русски.",
            f"{hint}\nИсполнители: {', '.join(artists[:15])}\n"
            "Название — до 4 слов, без кавычек. Описание — одно предложение о настроении и звучании.",
            NAME_SCHEMA, max_tokens=300)
        return data["name"].strip()[:80] or _("Микс"), data["description"].strip()[:300]
    except llm.ModelError as e:
        log.info("no playlist name from the model: %s", e)
        return _("Микс: ") + ", ".join(artists[:3]), ""


async def plan_mixes(db: AsyncSession, user: User, count: int, mix: mixing.Mix | None = None,
                     rng: random.Random | None = None) -> list[list[Candidate]] | dict:
    """`count` mixes for one user, nothing saved: different corners of their taste, each with the «Плейлисты» tab's
    shares of new, known and «слушают вместе» tracks, varied and in playing order. A dict saying why when there's
    nothing to make them of."""
    mix = mix or await mixing.load(db, "playlists")
    j = await retrieval.joint(db, await retrieval.space_weights(db, "mix"))
    taste = {s: j.center(s, v) for s, v in (await taste_of(db, user)).items() if s in j.weights}
    if not taste:
        return {"skipped": _("вкус ещё не известен (мало прослушиваний)")}
    pool = await retrieval.candidates(db, j, taste, user, rest_days=mix.track_rest_days)
    neighbours = related.artists_for(await db.get(Taste, user.id), mix.related_depth) if mix.related_share else {}
    near = await retrieval.candidates(db, j, taste, user, rest_days=mix.track_rest_days,
                                      only_artists=list(neighbours)) if neighbours else []
    known = await retrieval.played(db, user, {c.id for c in pool + near})
    resting = await mixing.resting_artists(db, user, datetime.now(UTC), mix.artist_rest_hours)
    taste_joint = j.vector(taste)
    mixes = []
    for i in range(count):
        if len(pool) < mix.size // 2:
            break
        # each next mix starts from a different seed: the first from the taste itself, then from tracks not yet used
        seed = normalize(taste_joint) if i == 0 else blend([(taste_joint, 0.5),
                                                            (pool[min(len(pool) - 1, i * 7)].vector, 0.5)])
        assert seed is not None
        chosen = mixing.assemble(_towards(pool, seed), _towards(near, seed), known, mix, mix.size, resting, rng)
        used = {c.id for c in chosen}
        pool = [c for c in pool if c.id not in used]
        near = [c for c in near if c.id not in used]
        mixes.append(chosen)
    return mixes


def _towards(candidates: list[Candidate], seed: np.ndarray) -> list[Candidate]:
    """The candidates scored against another query, best first."""
    ranked = [Candidate(c.id, c.artist, c.vector, float(np.dot(normalize(c.vector), seed))) for c in candidates]
    return sorted(ranked, key=lambda c: -c.relevance)


async def nightly(db: AsyncSession, user: User, count: int | None = None) -> dict:
    """The night's mixes for one user, each named by the model and written into Navidrome."""
    count = count or int(await settings.get(db, "playlists.per_night"))
    mixes = await plan_mixes(db, user, count)
    if isinstance(mixes, dict):
        return mixes
    made = []
    for chosen in mixes:
        sound = describe_set(await _sound_of(db, [c.id for c in chosen]))
        name, description = await _name(db, [c.artist for c in chosen], "Ночной микс по вкусу слушателя."
                                        + (f"\nЗвучание: {sound}." if sound else ""))
        playlist = Playlist(user_id=user.id, kind="nightly", name=name, description=description,
                            track_ids=[c.id for c in chosen])
        db.add(playlist)
        await write(db, user, playlist)
        await db.commit()  # each mix on its own: one written into Navidrome is kept even if the next one fails
        made.append(name)
    return {"playlists": made}


async def remove_old(db: AsyncSession) -> dict:
    """The generated playlists older than their kind's keeping days («Плейлисты» tab; 0 keeps them), from Kurai and
    the users' Navidrome; a liked one (👍) stays."""
    values = await settings.get_all(db)
    removed = {}
    for kind, key in KEEP_DAYS.items():
        days = float(values[key] or 0)
        if days <= 0:
            continue
        before = datetime.now(UTC) - timedelta(days=days)
        old = (await db.execute(select(Playlist, User).join(User, User.id == Playlist.user_id).where(
            Playlist.kind == kind, Playlist.created_at < before, Playlist.feedback <= 0))).all()
        for playlist, user in old:
            account = user_account(user)
            if playlist.navidrome_id and account is not None:
                await navidrome().delete_playlist(account, playlist.navidrome_id)
            await db.delete(playlist)
            await db.commit()
        removed[kind] = len(old)
    return removed


async def _library_summary(db: AsyncSession) -> str:
    tracks = (await db.execute(select(func.count()).select_from(Track))).scalar_one()
    artists = (await db.execute(select(func.count(func.distinct(Track.artist))))).scalar_one()
    top = (await db.execute(select(Track.artist, func.count()).group_by(Track.artist)
                            .order_by(func.count().desc()).limit(30))).all()
    return (f"В библиотеке {tracks} треков, {artists} исполнителей. Больше всего треков у: "
            + ", ".join(f"{a} ({n})" for a, n in top))


def mix_query(j, space: str, parts: list[tuple[np.ndarray | None, float]]) -> np.ndarray | None:
    """A query in one space from several directions, each centred first."""
    return blend([(j.center(space, v) if v is not None else None, w) for v, w in parts])


ASK_SYSTEM = ("Ты — помощник музыкального сервера. Превращаешь просьбу слушателя в план плейлиста или отвечаешь "
              "на вопрос о его музыке. Отвечай только JSON по схеме, тексты — по-русски.")
ASK_RULES = (
    "Если это просьба о музыке: intent=playlist, mood_text — описание звучания для поиска (жанры, настроение, эпоха, "
    "темп), sound_en — то же звучание по-английски, 5–12 слов для поиска по звуку (инструменты, темп, настроение, "
    "жанр, например «slow melancholic piano with soft strings» или «fast energetic electronic workout beat»), "
    "seed_artists — исполнители из просьбы, exclude_artists — кого исключить, minutes — длительность (по умолчанию "
    "60), name — название до 4 слов, answer — пустая строка. "
    "Темп и энергия — отдельно, они проверяются по анализу звука: bpm_min и bpm_max — границы темпа в ударах в "
    "минуту, 0 — без границы (быстрое, для бега и тренировки — bpm_min 120; медленное — bpm_max 95; если темп не "
    "важен — оба 0); energy — calm (спокойное, для сна, фон), medium, energetic (бодрое, тренировка, вечеринка) или "
    "any; vocals — instrumental (без слов, без вокала), vocal или any. "
    "Если это вопрос: intent=answer, answer — ответ, остальные строки пустые, minutes=0, bpm 0, energy и vocals any.")


async def _analysis(db: AsyncSession, ids: list[str]) -> dict[str, TrackAudio]:
    return {a.track_id: a for a in await _sound_of(db, ids)}


def _by_fit(candidates: list[Candidate], verdicts: dict[str, bool | None]) -> list[list[Candidate]]:
    """The candidates that meet the request's tempo, energy and vocals, those nothing is known of, and the rest."""
    groups: dict[bool | None, list[Candidate]] = {True: [], None: [], False: []}
    for c in candidates:
        groups[verdicts.get(c.id)].append(c)
    return [groups[True], groups[None], groups[False]]


async def ask(db: AsyncSession, user: User, text: str) -> dict:
    """A request in words: the model turns it into a plan (or answers a question); retrieval picks the tracks near
    its description and checks its tempo, energy and vocals against the audio analysis; the «Плейлисты» tab's
    variety, randomness, artists' limits, order and the weight of the user's taste apply."""
    taste_row = await db.get(Taste, user.id)
    taste_artists = ", ".join(a["name"] for a in (taste_row.top_artists if taste_row else [])[:10]) or "неизвестно"
    plan = await llm.complete_json(
        db, "ask", ASK_SYSTEM,
        f"Просьба: {text}\nЛюбимые исполнители слушателя: {taste_artists}\n{await _library_summary(db)}\n{ASK_RULES}",
        PLAN_SCHEMA, max_tokens=600)
    if plan["intent"] == "answer":
        return {"answer": plan["answer"]}
    mix = await mixing.load(db, "playlists")
    j = await retrieval.joint(db, await retrieval.space_weights(db, "ask"))
    taste = await taste_of(db, user) if mix.taste_weight > 0 else {}
    seeds = await retrieval.seeds(db, j, plan["seed_artists"])
    queries: dict[str, np.ndarray | None] = {}
    if "text" in j.weights:
        embedder = await llm.embed_model(db)
        mood = np.asarray((await embedder.embed([plan["mood_text"] or text]))[0], dtype=np.float32)
        queries["text"] = mix_query(j, "text", [(normalize(mood), 0.6), (seeds.get("text"), 0.25),
                                                (taste.get("text"), mix.taste_weight)])
    if "clap" in j.weights:
        described = await audio_client.clap_text(db, plan.get("sound_en") or plan["mood_text"] or text)
        queries["clap"] = mix_query(j, "clap", [(described, 0.75), (seeds.get("clap"), 0.25)])
    if "sound" in j.weights:  # the sound of the named artists; the taste's only as much as it's set to count
        queries["sound"] = mix_query(j, "sound", [(seeds.get("sound"), 0.6), (taste.get("sound"), mix.taste_weight)])
    candidates = await retrieval.candidates(db, j, queries, user, exclude_artists=set(plan["exclude_artists"]))
    if not candidates:
        return {"answer": _("В библиотеке пока нет подходящих треков с векторами (идёт индексация?).")}
    # a model may leave out what the schema asks (not every server enforces it): then nothing is asked of the sound
    asked = {"bpm_min": max(0, int(plan.get("bpm_min") or 0)), "bpm_max": max(0, int(plan.get("bpm_max") or 0)),
             "wanted_energy": plan.get("energy") or "any", "vocals": plan.get("vocals") or "any"}
    sound = await _analysis(db, [c.id for c in candidates])
    verdicts = {c.id: fits(sound.get(c.id), **asked) for c in candidates}
    minutes = max(10, min(240, plan["minutes"] or 60))
    durations = await _durations(db, [c.id for c in candidates])
    typical = float(np.median([d for d in durations.values() if d] or [240]))
    need = min(ASK_MAX_TRACKS, int(minutes * 60 / typical) + 2)
    # the fitting tracks first; the unknown ones only when they're too few, the rest last (a small library still gets
    # its playlist)
    chosen: list[Candidate] = []
    for group in _by_fit(adjust(candidates, mix.randomness), verdicts):
        if len(chosen) < need:
            chosen = mmr_select(group, need - len(chosen), mix.variety, mix.artist_max, chosen)
    chosen = arrange(_fit_minutes(chosen, durations, minutes), mix.smooth, mix.artist_gap)
    playlist = Playlist(user_id=user.id, kind="ask", name=plan["name"][:80] or _("По запросу"),
                        description=text[:300], track_ids=[c.id for c in chosen])
    db.add(playlist)
    await write(db, user, playlist)
    await db.commit()
    # what the model planned and how many tracks met it: on «Задачи», to see why a playlist came out as it did
    return {"playlist_id": playlist.id, "name": playlist.name, "tracks": len(chosen),
            "fitting": sum(verdicts.get(c.id) is True for c in chosen),
            "plan": {"mood_text": plan["mood_text"], "sound_en": plan.get("sound_en", ""),
                     "seed_artists": plan["seed_artists"], **asked}}


async def explain(db: AsyncSession, user: User, track_id: str) -> str:
    """One or two sentences on why a track fits the user, from its neighbours among what they like."""
    track = await db.get(Track, track_id)
    if track is None:
        return _("Трек не найден.")
    neighbours = []
    if track.embedding is not None:
        rows = (await db.execute(
            select(Track.artist, Track.title).join(Listen, Listen.track_id == Track.id)
            .where(Listen.user_id == user.id, Listen.play_count > 0, Track.id != track.id)
            .order_by(Track.embedding.cosine_distance(track.embedding)).limit(5))).all()
        neighbours = [f"{a} — {t}" for a, t in rows]
    taste_row = await db.get(Taste, user.id)
    top = ", ".join(a["name"] for a in (taste_row.top_artists if taste_row else [])[:8])
    sound = await db.get(TrackAudio, track.id)
    heard = describe(sound) if sound is not None and sound.error is None else ""
    return await llm.complete(
        db, "explain", "Ты объясняешь слушателю, почему ему может понравиться трек. 1–2 предложения, по-русски.",
        f"Трек: {track.artist} — {track.title} ({track.album}, {', '.join(track.genres or [])}).\n"
        + (f"Звучание (по анализу звука): {heard}.\n" if heard else "") +
        f"Похожее из того, что он слушает: {'; '.join(neighbours) or 'нет данных'}.\nЕго любимые исполнители: {top}.",
        max_tokens=200)
