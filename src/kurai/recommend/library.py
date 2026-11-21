"""The library as Kurai sees it: tracks from Navidrome, their text embeddings, each user's plays and stars, and each
user's taste vector."""

import logging
import bisect
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import llm, security, settings
from kurai.artists import top_artists
from kurai.audio import EFFNET_MODEL
from kurai.config import get_config
from kurai.db.models import Listen, Taste, Track, TrackAudio, User, WaveEvent
from kurai.i18n import _
from kurai.integrations.navidrome import Account, Navidrome, song_fields
from kurai.recommend import artist_ids, start
from kurai.recommend.vectors import SKIP_CAP, listen_weight, skip_weight, taste_vector
from kurai.recommend import mood as moodmod

log = logging.getLogger(__name__)

EMBED_BATCH = 16
# sync_library: refuse to delete more than this fraction of the known library in one run. A transient partial answer
# from Navidrome must not cost days of audio analysis.
MAX_DELETE_FRACTION = 0.25
NEVER_LOCALLY_WRITTEN = datetime(1970, 1, 1, tzinfo=UTC)  # sync's own rows: the grace hour protects only local writes


async def _skip_context(db: AsyncSession, user: User, values: dict, now: datetime,
                        half_life: float, floor: float) -> tuple[dict[str, float], set[str]]:
    """Contextual skips as gentle, mood-weighted negatives (only when «Учитывать настроение» is on) and the tracks
    the user chose to play. Explicit 👎 stays in listen_weight at full weight; «Не сейчас» says nothing."""
    if not values.get("mix.wave.mood"):
        return {}, set()
    events = [e for e in (await db.execute(select(WaveEvent).where(WaveEvent.user_id == user.id)
                                           .order_by(WaveEvent.at))).scalars() if not e.not_now]
    if not events:
        return {}, set()
    chosen = {e.track_id for e in events if e.chosen and not e.skipped}
    moods = await moodmod.track_vectors(db, {e.track_id for e in events})
    hours = float(values.get("mix.wave.session_hours", 3))
    hl = float(values.get("mix.wave.session_half_life", 45))
    ats = [e.at for e in events]
    neg: dict[str, float] = {}
    for i, e in enumerate(events):
        if not e.skipped or e.disliked:
            continue
        lo = bisect.bisect_left(ats, e.at - timedelta(hours=hours))
        sm = moodmod.session_mood(events[lo:i], moods, hl, e.at)
        v = moods.get(e.track_id)
        fit = float(np.dot(v, sm.vector)) if (v is not None and sm.vector is not None) else None
        age_days = (now - e.at).total_seconds() / 86400
        neg[e.track_id] = neg.get(e.track_id, 0.0) + skip_weight(fit, sm.regime == "bad_day", age_days, half_life,
         floor)
    return {t: max(-SKIP_CAP, w) for t, w in neg.items()}, chosen


def navidrome() -> Navidrome:
    return Navidrome(get_config().navidrome_url)


async def admin_account(db: AsyncSession) -> Account:
    return Account(await settings.get(db, "navidrome.admin_user"), await settings.get(db, "navidrome.admin_password"))


async def admin_login(db: AsyncSession) -> tuple[Navidrome, str]:
    """Navidrome and a token of Kurai's admin account there, the account made first when it's missing (Navidrome was
    still starting when Kurai did, or it came up empty)."""
    nd = navidrome()
    account = await admin_account(db)
    await nd.ensure_admin(account)
    return nd, await nd.login(account)


def user_account(user: User) -> Account | None:
    password = security.decrypt(user.navidrome_password)
    return Account(user.username, password) if password else None


def track_text(t: Track) -> str:
    """What a track's text embedding is made of."""
    parts = [t.artist, t.title, t.album, ", ".join(t.genres or [])]
    if t.year:
        parts.append(str(t.year))
    return " — ".join(p for p in parts if p)


async def sync_library(db: AsyncSession) -> dict:
    """Every song Navidrome has, upserted; songs it no longer has are removed (with their plays and sound). An empty
    answer while Kurai knows tracks removes nothing: the music folder is more likely missing (an unmounted disk)
    than empty. A partial answer that would delete more than MAX_DELETE_FRACTION of the known library is also refused:
    Navidrome's paging by title is unstable on ties, and a transient outage must not cost days of audio analysis."""
    nd, token = await admin_login(db)
    started = datetime.now(UTC)
    count = 0
    async for song in nd.songs(token, sort="id"):  # id is unique and stable across pages; title was not on ties
        fields = song_fields(song)
        stmt = insert(Track).values(**fields, seen_at=started)
        changed = {k: stmt.excluded[k] for k in fields if k != "id"} | {"seen_at": started}
        # a changed text (tags fixed in Navidrome) needs a new embedding
        await db.execute(stmt.on_conflict_do_update(index_elements=[Track.id], set_=changed))
        count += 1
    total = (await db.execute(select(func.count()).select_from(Track))).scalar_one()
    if count == 0 and total:
        await db.rollback()
        raise RuntimeError(_("Navidrome не вернул ни одного трека: библиотека не тронута (папка с музыкой не "
                             "подключена или идёт пересканирование?)"))
    # the rows the sync hasn't touched are exactly what the delete would remove — new tracks in the answer must not
    # mask a mass deletion (total - count undercounts it)
    stale = (await db.execute(select(func.count()).select_from(Track).where(Track.seen_at < started))).scalar_one()
    if stale > MAX_DELETE_FRACTION * total:
        await db.rollback()
        raise RuntimeError(_("Navidrome вернул только {n} из {t} известных треков: библиотека не тронута "
                             "(похоже на временную ошибку, а не на удаление {r} треков)").format(
            n=total - stale, t=total, r=stale))
    removed = (await db.execute(delete(Track).where(Track.seen_at < started))).rowcount
    await artist_ids.apply(db)  # the ids found by name stay, where the tags still have none
    await db.commit()
    return {"tracks": count, "removed": removed}


async def embed_tracks(db: AsyncSession, limit: int | None = None) -> dict:
    """Embeds the tracks without a vector of the current embedding model (all of them after a model change)."""
    model_name = await settings.get(db, "embed.model")
    model = await llm.embed_model(db)
    query = select(Track).where((Track.embedding.is_(None)) | (Track.embedding_model != model_name)).order_by(Track.id)
    if limit:
        query = query.limit(limit)
    tracks = list((await db.execute(query)).scalars())
    for i in range(0, len(tracks), EMBED_BATCH):
        batch = tracks[i:i + EMBED_BATCH]
        vectors = await model.embed([track_text(t) for t in batch])
        for t, v in zip(batch, vectors, strict=True):
            t.embedding = v
            t.embedding_model = model_name
        await db.commit()
    return {"embedded": len(tracks)}


async def pending_embeddings(db: AsyncSession) -> int:
    model_name = await settings.get(db, "embed.model")
    return (await db.execute(select(func.count()).select_from(Track).where(
        (Track.embedding.is_(None)) | (Track.embedding_model != model_name)))).scalar_one()


async def sync_listens(db: AsyncSession, user: User) -> dict:
    """The user's play counts, last plays, stars and ratings from Navidrome (its annotations are per user). A local
    row Kurai itself wrote within the last hour (an optimistic annotation whose Navidrome write failed) is kept —
    the user's explicit action is not erased just because Navidrome is behind."""
    account = user_account(user)
    if account is None:
        return {"skipped": "нет учётной записи Navidrome"}
    nd = navidrome()
    token = await nd.login(account)
    known = set((await db.execute(select(Track.id))).scalars())
    rows = 0
    active: set[str] = set()
    async for song in nd.songs(token, sort="id"):
        if song["id"] not in known:
            continue
        play_count = int(song.get("playCount") or 0)
        starred = bool(song.get("starred"))
        rating = int(song.get("rating") or 0)
        if not (play_count or starred or rating):
            continue
        active.add(song["id"])
        played = song.get("playDate")
        last = datetime.fromisoformat(played.replace("Z", "+00:00")) if played else None
        values = {"user_id": user.id, "track_id": song["id"], "play_count": play_count, "last_played": last,
                  "starred": starred, "rating": rating}
        stmt = insert(Listen).values(updated_at=NEVER_LOCALLY_WRITTEN,**values)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[Listen.user_id, Listen.track_id],
            set_={k: stmt.excluded[k] for k in ("play_count", "last_played", "starred", "rating")}))
        rows += 1
    # a star or a rating taken back on a track never played: it says nothing any more. Rows Kurai itself wrote within
    # the last hour (note_listen's optimistic writes) are kept: they may not have reached Navidrome yet.
    grace = datetime.now(UTC) - timedelta(hours=1)
    cleared = (await db.execute(
        delete(Listen).where(Listen.user_id == user.id, Listen.track_id.not_in(active),
                             Listen.updated_at < grace).execution_options(synchronize_session=False))).rowcount
    await db.commit()
    return {"listens": rows, "cleared": cleared}


async def build_taste(db: AsyncSession, user: User) -> dict:
    """The user's taste vectors (from the tags' text and, once analysed, from the sound) and top artists; what they
    said they like at the start is a prior that fades as they play more (start.py). The audio vector records the
    effnet model it was made with; a model change leaves it stale until the next rebuild."""
    model_name = await settings.get(db, "embed.model")
    audio_model = EFFNET_MODEL
    rows = (await db.execute(
        select(Listen, Track, TrackAudio.effnet).join(Track, Track.id == Listen.track_id)
        .outerjoin(TrackAudio, (TrackAudio.track_id == Track.id) & (TrackAudio.effnet_model == audio_model)
                   & TrackAudio.error.is_(None))
        .where(Listen.user_id == user.id)
    )).all()
    now = datetime.now(UTC)
    values = await settings.get_all(db)
    half_life, floor = float(values["taste.half_life_days"]), float(values["taste.floor"]) / 100
    since = now - timedelta(days=float(values["taste.window_days"])) if values["taste.window"] else None
    skip_neg, chosen_tids = await _skip_context(db, user, values, now, half_life, floor)
    text_vectors, text_weights, sound_vectors, sound_weights = [], [], [], []
    artists_all, artists_explicit, artists_chosen = [], [], []
    seen: set[str] = set()

    def add(track, effnet, w):
        if w == 0:
            return
        if track.embedding is not None and track.embedding_model == model_name:
            text_vectors.append(np.asarray(track.embedding, dtype=np.float32))
            text_weights.append(w)
        if effnet is not None:
            sound_vectors.append(np.asarray(effnet, dtype=np.float32))
            sound_weights.append(w)

    for listen, track, effnet in rows:
        if (since is not None and listen.last_played is not None and listen.last_played < since
                and listen.rating not in (1, 2)):
            continue
        seen.add(track.id)
        w = listen_weight(listen.play_count, listen.starred, listen.rating, listen.last_played, now, half_life, floor)
        w += skip_neg.get(track.id, 0.0)
        add(track, effnet, w)
        if w > 0:
            artists_all.append((track.artist, w))
            if listen.starred or listen.rating >= 4:
                artists_explicit.append((track.artist, w))
            if track.id in chosen_tids:
                artists_chosen.append((track.artist, w))
    # tracks only skipped (never scrobbled): their contextual negatives still belong in the taste
    extra_ids = [t for t in skip_neg if t not in seen]
    if extra_ids:
        extra = (await db.execute(
            select(Track, TrackAudio.effnet).join(Track, Track.id == TrackAudio.track_id)
            .outerjoin(TrackAudio, (TrackAudio.track_id == Track.id) & (TrackAudio.effnet_model == audio_model)
                       & TrackAudio.error.is_(None))
            .where(Track.id.in_(extra_ids)))).all()
        for track, effnet in extra:
            add(track, effnet, skip_neg[track.id])
    vector = taste_vector(text_vectors, text_weights)
    sound = taste_vector(sound_vectors, sound_weights)
    # top artists from explicit feedback and deliberate plays first; passive wave plays only as a fallback
    top = top_artists(artists_explicit + artists_chosen) or top_artists(artists_all)
    first = await start.first_taste(db, user) if user.start_asked_at else None
    if first is not None:
        vector = start.with_prior(vector, first["text"], len(text_vectors))
        sound = start.with_prior(sound, first["sound"], len(sound_vectors))
        top = top or first["top_artists"]
    stmt = insert(Taste).values(user_id=user.id, vector=vector.tolist() if vector is not None else None,
                                embedding_model=model_name, audio_vector=sound.tolist() if sound is not None else None,
                                audio_model=audio_model,
                                top_artists=top, tracks_used=len(text_vectors), updated_at=now)
    await db.execute(stmt.on_conflict_do_update(index_elements=[Taste.user_id], set_={
        k: stmt.excluded[k] for k in ("vector", "embedding_model", "audio_vector", "audio_model", "top_artists",
                                      "tracks_used", "updated_at")}))
    await db.commit()
    return {"tracks_used": len(text_vectors), "has_vector": vector is not None, "by_sound": len(sound_vectors),
            "from_answers": first is not None}


async def recent_listens(db: AsyncSession, user: User, limit: int, offset: int = 0) -> list[tuple[Track, Listen]]:
    """What the user played last, in any app (Navidrome's last plays; the web player's at once): «История»."""
    return list((await db.execute(
        select(Track, Listen).join(Listen, Listen.track_id == Track.id)
        .where(Listen.user_id == user.id, Listen.last_played.is_not(None))
        .order_by(Listen.last_played.desc(), Track.id).limit(limit).offset(offset))).all())


async def note_listen(db: AsyncSession, user: User, track_id: str, **values) -> Listen:
    """The user's record of a track changed at once (a play, a ♥, a rating), not waiting for the next sync with
    Navidrome, which is told too and brings the same back. `played_at`: one more play then. `updated_at` is touched
    so sync_listens's grace window protects the write if the Navidrome call fails."""
    listen = (await db.execute(select(Listen).where(Listen.user_id == user.id, Listen.track_id == track_id))
              ).scalar_one_or_none() or Listen(user_id=user.id, track_id=track_id, play_count=0, starred=False,
                                               rating=0)
    played_at = values.pop("played_at", None)
    if played_at is not None:
        listen.play_count = (listen.play_count or 0) + 1
        listen.last_played = played_at
    for key, value in values.items():
        setattr(listen, key, value)
    listen.updated_at = datetime.now(UTC)
    db.add(listen)
    await db.commit()
    return listen


async def active_users(db: AsyncSession) -> list[User]:
    return list((await db.execute(select(User).where(User.active.is_(True)).order_by(User.id))).scalars())
