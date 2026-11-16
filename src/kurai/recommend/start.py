"""The start of a listener who hasn't played anything yet: what they said they like (styles of the library, their own
words) as a first taste, and the library's popular tracks (here, then in the world) for the wave.

The first taste joins the taste from listens as a prior that fades: it counts like PRIOR tracks, so it leads at first
and is a hint once dozens of tracks are played (library.build_taste)."""

import logging

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import llm, settings
from kurai.audio import EFFNET_MODEL
from kurai.db.models import Listen, Track, TrackAudio, User
from kurai.recommend import popularity
from kurai.recommend.vectors import mean_unit

log = logging.getLogger(__name__)

STYLES_SHOWN = 24  # the library's styles offered in the question
STYLE_TRACKS = 400  # tracks of the chosen styles a first taste is made of, at most
PRIOR = 10  # the first taste counts like this many played tracks
POPULAR_PER_ARTIST = 2


async def library_styles(db: AsyncSession, limit: int = STYLES_SHOWN) -> list[str]:
    """The library's most common genres, for the question (only what there is music of)."""
    genre = func.jsonb_array_elements_text(Track.genres).table_valued("value").render_derived()
    rows = (await db.execute(select(genre.c.value, func.count()).select_from(Track).join(genre, genre.c.value != "")
                             .group_by(genre.c.value).order_by(func.count().desc()).limit(limit))).all()
    return [name for name, _count in rows]


async def first_taste(db: AsyncSession, user: User) -> dict | None:
    """The taste the answers point at: the mean of the chosen styles' tracks in each space, the own words' embedding
    joined to the text one; with the styles' most frequent artists. None without answers."""
    styles = [s for s in user.start_styles or [] if isinstance(s, str)]
    words = (user.start_text or "").strip()
    if not styles and not words:
        return None
    model_name = await settings.get(db, "embed.model")
    text, sound, artists = [], [], {}
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
            if artist:
                artists[artist] = artists.get(artist, 0) + 1
    text_vector = mean_unit(text)
    described = ", ".join(styles) + (f". {words}" if words else "")
    try:  # the own words in the text space: as much as all the styles' tracks together
        said = np.asarray((await (await llm.embed_model(db)).embed([described]))[0], dtype=np.float32)
        said /= np.linalg.norm(said) or 1.0
        text_vector = said if text_vector is None else 0.5 * text_vector + 0.5 * said
    except Exception as e:  # no embedding model: the styles' tracks alone
        log.info("the answers not embedded: %s", e)
    top = [{"name": a, "weight": n} for a, n in sorted(artists.items(), key=lambda kv: -kv[1])[:20]]
    return {"text": text_vector, "sound": mean_unit(sound), "top_artists": top}


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
        if track_id in leave_out or track_id in out or per_artist.get(artist, 0) >= POPULAR_PER_ARTIST:
            continue
        per_artist[artist] = per_artist.get(artist, 0) + 1
        out.append(track_id)
        if len(out) == count:
            break
    return out
