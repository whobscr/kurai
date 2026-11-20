"""The start of a listener who hasn't played anything yet: what they said they like (styles of the library, their own
words) as a first taste, and the library's popular tracks (here, then in the world) for the wave.
The first taste joins the taste from listens as a prior that fades: it counts like PRIOR tracks, so it leads at first
and is a hint once dozens of tracks are played (library.build_taste).

The first taste is computed once, at start_asked_at, and stored on the User. It used to be re-drawn at random nightly,
so a new user's wave swung for no reason and the top artists seeded «слушают вместе» from a different sample every
night."""

import logging

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import llm, settings
from kurai.artists import artist_key, top_artists
from kurai.audio import EFFNET_MODEL
from kurai.db.models import Listen, Track, TrackAudio, User
from kurai.recommend import popularity
from kurai.recommend.vectors import mean_unit

log = logging.getLogger(__name__)

STYLES_SHOWN = 24
STYLE_TRACKS = 400
PRIOR = 10
POPULAR_PER_ARTIST = 2


async def library_styles(db: AsyncSession, limit: int = STYLES_SHOWN) -> list[str]:
    """The library's most common genres, for the question (only what there is music of)."""
    genre = func.jsonb_array_elements_text(Track.genres).table_valued("value").render_derived()
    rows = (await db.execute(select(genre.c.value, func.count()).select_from(Track).join(genre, genre.c.value != "")
                             .group_by(genre.c.value).order_by(func.count().desc()).limit(limit))).all()
    return [name for name, _count in rows]


async def first_taste(db: AsyncSession, user: User, recompute: bool = False) -> dict | None:
    """The taste the answers point at: the mean of the chosen styles' tracks in each space, the own words' embedding
    joined to the text one; with the styles' most frequent artists. None without answers. The result is stored on
    the user (User.start_* columns) so it doesn't move from night to night — `store` is called when the answers are
    saved; `recompute` ignores the stored taste (the answers changed)."""
    if not recompute:
        stored = await _read_stored(db, user)
        if stored is not None:
            return stored
    styles = [s for s in user.start_styles or [] if isinstance(s, str)]
    words = (user.start_text or "").strip()
    if not styles and not words:
        return None
    model_name = await settings.get(db, "embed.model")
    text, sound, artists = [], [], []
    if styles:
        rows = (await db.execute(
            select(Track.artist, Track.embedding, Track.embedding_model, TrackAudio.effnet)
            .outerjoin(TrackAudio, (TrackAudio.track_id == Track.id) & (TrackAudio.effnet_model == EFFNET_MODEL)
                       & TrackAudio.error.is_(None))
            .where(Track.genres.has_any(array(styles))).order_by(func.random()).limit(STYLE_TRACKS))).all()
        for artist, embedding, embedding_model, effnet in rows:
            if embedding is not None and embedding_model == model_name:
                text.append(np.asarray(embedding, dtype=np.float32))
            if effnet is not None:
                sound.append(np.asarray(effnet, dtype=np.float32))
            artists.append((artist, 1))
    text_vector = mean_unit(text)
    described = ", ".join(styles) + (f". {words}" if words else "")
    try:
        said = np.asarray((await (await llm.embed_model(db)).embed([described]))[0], dtype=np.float32)
        said /= np.linalg.norm(said) or 1.0
        text_vector = said if text_vector is None else 0.5 * text_vector + 0.5 * said
    except Exception as e:
        log.info("the answers not embedded: %s", e)
    top = top_artists(artists)
    return {"text": text_vector, "sound": mean_unit(sound), "top_artists": top,
            "text_model": model_name, "audio_model": EFFNET_MODEL}


async def _read_stored(db: AsyncSession, user: User) -> dict | None:
    """The stored first taste, if the user has one and its models still match the current ones (a taste of another
    model can't be compared with today's tracks). None means "compute it"."""
    if user.start_text_vector is None and user.start_audio_vector is None and not user.start_top_artists:
        return None
    from kurai.audio import EFFNET_MODEL  # local: avoids a cycle at module load
    if user.start_text_vector is not None and user.start_text_model != await settings.get(db, "embed.model"):
        return None
    if user.start_audio_vector is not None and user.start_audio_model != EFFNET_MODEL:
        return None
    return {"text": np.asarray(user.start_text_vector, dtype=np.float32) if user.start_text_vector is not None
             else None,
            "sound": np.asarray(user.start_audio_vector, dtype=np.float32) if user.start_audio_vector is not None
             else None,
            "top_artists": list(user.start_top_artists or []),
            "text_model": user.start_text_model, "audio_model": user.start_audio_model or EFFNET_MODEL}


async def store(db: AsyncSession, user: User, taste: dict) -> None:
    """Persist a computed first taste on the user. Call once, at start_asked_at. Commit is the caller's."""
    text = taste.get("text")
    sound = taste.get("sound")
    user.start_text_vector = text.tolist() if text is not None else None
    user.start_audio_vector = sound.tolist() if sound is not None else None
    user.start_top_artists = list(taste.get("top_artists") or [])
    user.start_text_model = taste.get("text_model")
    user.start_audio_model = taste.get("audio_model")
    db.add(user)


def with_prior(learned: np.ndarray | None, first: np.ndarray | None, played: int) -> np.ndarray | None:
    """The taste from listens with the first taste as a prior worth PRIOR tracks (both are means of unit vectors)."""
    if first is None:
        return learned
    if learned is None:
        return first
    share = PRIOR / (PRIOR + played)
    return (1 - share) * learned + share * first


async def popular(db: AsyncSession, count: int, leave_out: set[str]) -> list[str]:
    """The library's popular tracks, a couple per artist: first what people here play and star most, then what the
    world listens to most (ListenBrainz, by album or artist: popularity.py), then a varied draw from the library."""
    here = func.sum(Listen.play_count) + 5 * func.sum(func.cast(Listen.starred, type_=Listen.play_count.type))
    rows = (await db.execute(select(Track.id, Track.artist).join(Listen, Listen.track_id == Track.id)
                             .group_by(Track.id).order_by(here.desc()).limit(count * 5))).all()
    if len(rows) < count:
        world, joins = popularity.track_score()
        stmt = select(Track.id, Track.artist)
        for table, on in joins:
            stmt = stmt.outerjoin(table, on)
        rows += (await db.execute(stmt.where(world > 0).order_by(world.desc(), func.random())
                                  .limit(count * 5))).all()
    if len(rows) < count:
        rows += (await db.execute(select(Track.id, Track.artist).order_by(func.random()).limit(count * 5))).all()
    out, per_artist = [], {}
    for track_id, artist in rows:
        artist = artist_key(artist)
        if track_id in leave_out or track_id in out or per_artist.get(artist, 0) >= POPULAR_PER_ARTIST:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        out.append(track_id)
        if len(out) == count:
            break
    return out
