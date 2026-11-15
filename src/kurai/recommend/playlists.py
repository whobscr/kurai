"""Playlists: the nightly mixes, «Спросить» (the model only plans the query; code picks the tracks) and «Почему этот
трек». Every playlist is written into Navidrome as the user's private playlist, so it appears in any music app."""

import logging

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import llm, settings
from kurai.audio import client as audio_client
from kurai.audio.tags import describe, describe_set
from kurai.db.models import Listen, Playlist, Taste, Track, TrackAudio, User
from kurai.i18n import _
from kurai.recommend import retrieval
from kurai.recommend.library import navidrome, user_account
from kurai.recommend.vectors import Candidate, blend, mmr_select, normalize, order_smoothly

log = logging.getLogger(__name__)

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
    },
    "required": ["intent", "answer", "mood_text", "sound_en", "seed_artists", "exclude_artists", "minutes", "name"],
    "additionalProperties": False,
}


async def taste_of(db: AsyncSession, user: User) -> dict[str, np.ndarray]:
    """The user's taste in each space it's known in (not centred)."""
    taste = await db.get(Taste, user.id)
    if taste is None:
        return {}
    parts = {"text": taste.vector, "sound": taste.audio_vector}
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


async def nightly(db: AsyncSession, user: User, count: int | None = None, size: int = 30) -> dict:
    """The night's mixes for one user: different corners of their taste, each a varied, smoothly ordered set."""
    j = await retrieval.joint(db, await retrieval.space_weights(db, "mix"))
    taste = {s: j.center(s, v) for s, v in (await taste_of(db, user)).items() if s in j.weights}
    if not taste:
        return {"skipped": "вкус ещё не известен (мало прослушиваний)"}
    count = count or int(await settings.get(db, "playlists.per_night"))
    pool = await retrieval.candidates(db, j, taste, user, exclude_recent=True)
    taste_joint = j.vector(taste)
    made = []
    for i in range(count):
        if len(pool) < size // 2:
            break
        # each next mix starts from a different seed: the first from the taste itself, then from tracks not yet used
        seed = normalize(taste_joint) if i == 0 else blend([(taste_joint, 0.5),
                                                            (pool[min(len(pool) - 1, i * 7)].vector, 0.5)])
        assert seed is not None
        ranked = [Candidate(c.id, c.artist, c.vector, float(np.dot(normalize(c.vector), seed))) for c in pool]
        ranked.sort(key=lambda c: -c.relevance)
        chosen = order_smoothly(mmr_select(ranked, size))
        used = {c.id for c in chosen}
        pool = [c for c in pool if c.id not in used]
        sound = describe_set(await _sound_of(db, [c.id for c in chosen]))
        name, description = await _name(db, [c.artist for c in chosen], "Ночной микс по вкусу слушателя."
                                        + (f"\nЗвучание: {sound}." if sound else ""))
        playlist = Playlist(user_id=user.id, kind="nightly", name=name, description=description,
                            track_ids=[c.id for c in chosen])
        db.add(playlist)
        await write(db, user, playlist)
        made.append(name)
    await db.commit()
    return {"playlists": made}


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


async def ask(db: AsyncSession, user: User, text: str) -> dict:
    """A request in words: the model turns it into a plan (or answers a question); retrieval picks the tracks."""
    taste_row = await db.get(Taste, user.id)
    taste_artists = ", ".join(a["name"] for a in (taste_row.top_artists if taste_row else [])[:10]) or "неизвестно"
    plan = await llm.complete_json(
        db, "ask",
        "Ты — помощник музыкального сервера. Превращаешь просьбу слушателя в план плейлиста или отвечаешь на вопрос "
        "о его музыке. Отвечай только JSON по схеме, тексты — по-русски.",
        f"Просьба: {text}\nЛюбимые исполнители слушателя: {taste_artists}\n{await _library_summary(db)}\n"
        "Если это просьба о музыке: intent=playlist, mood_text — описание звучания для поиска (жанры, настроение, "
        "эпоха), sound_en — то же звучание по-английски, 5–12 слов для поиска по звуку (инструменты, темп, "
        "настроение, жанр, например «slow melancholic piano with soft strings»), seed_artists — исполнители "
        "из просьбы, exclude_artists — кого исключить, minutes — длительность (по умолчанию 60), name — название "
        "до 4 слов, answer — пустая строка. Если это вопрос: intent=answer, "
        "answer — ответ, остальные строки пустые, minutes=0.",
        PLAN_SCHEMA, max_tokens=600)
    if plan["intent"] == "answer":
        return {"answer": plan["answer"]}
    j = await retrieval.joint(db, await retrieval.space_weights(db, "ask"))
    taste = await taste_of(db, user)
    seeds = await retrieval.seeds(db, j, plan["seed_artists"])
    queries: dict[str, np.ndarray | None] = {}
    if "text" in j.weights:
        embedder = await llm.embed_model(db)
        mood = np.asarray((await embedder.embed([plan["mood_text"] or text]))[0], dtype=np.float32)
        queries["text"] = mix_query(j, "text", [(normalize(mood), 0.6), (seeds.get("text"), 0.25),
                                           (taste.get("text"), 0.15)])
    if "clap" in j.weights:
        described = await audio_client.clap_text(db, plan.get("sound_en") or plan["mood_text"] or text)
        queries["clap"] = mix_query(j, "clap", [(described, 0.75), (seeds.get("clap"), 0.25)])
    if "sound" in j.weights:
        queries["sound"] = mix_query(j, "sound", [(seeds.get("sound"), 0.6), (taste.get("sound"), 0.4)])
    candidates = await retrieval.candidates(db, j, queries, user, exclude_artists=set(plan["exclude_artists"]))
    if not candidates:
        return {"answer": _("В библиотеке пока нет подходящих треков с векторами (идёт индексация?).")}
    chosen = mmr_select(candidates, 60)
    chosen = order_smoothly(_fit_minutes(chosen, await _durations(db, [c.id for c in chosen]),
                                         max(10, min(240, plan["minutes"] or 60))))
    playlist = Playlist(user_id=user.id, kind="ask", name=plan["name"][:80] or _("По запросу"),
                        description=text[:300], track_ids=[c.id for c in chosen])
    db.add(playlist)
    await write(db, user, playlist)
    await db.commit()
    return {"playlist_id": playlist.id, "name": playlist.name, "tracks": len(chosen)}


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
