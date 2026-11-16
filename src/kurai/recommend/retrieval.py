"""Finding candidate tracks across the vector spaces: the text embedding of the tags ("text"), the sound (Essentia's
Discogs-EffNet, "sound") and CLAP's space of sound and words ("clap"). Each space with a query proposes its nearest
tracks; every proposed track is then scored in all active spaces at once (vectors.Joint)."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import settings
from kurai.artists import artist_key
from kurai.audio import CLAP_MODEL, EFFNET_MODEL
from kurai.db.models import Listen, Track, TrackAudio, User
from kurai.recommend.vectors import Candidate, Joint, mean_unit, weights_of

POOL = 300
SPACES = ("text", "sound", "clap")


def _column(space: str):
    return {"text": Track.embedding, "sound": TrackAudio.effnet, "clap": TrackAudio.clap}[space]


def _valid(space: str, text_model: str):
    """The rows whose vector in this space is there and comes from the current model."""
    if space == "text":
        return [Track.embedding.is_not(None), Track.embedding_model == text_model]
    # a file whose analysis failed may still hold the vectors of the file before it: not used
    if space == "sound":
        return [TrackAudio.effnet.is_not(None), TrackAudio.effnet_model == EFFNET_MODEL, TrackAudio.error.is_(None)]
    return [TrackAudio.clap.is_not(None), TrackAudio.clap_model == CLAP_MODEL, TrackAudio.error.is_(None)]


def _from():
    return select().select_from(Track).outerjoin(TrackAudio, TrackAudio.track_id == Track.id)


async def space_weights(db: AsyncSession, task: str) -> dict[str, float]:
    """How much each space counts: the tags only while the audio analysis is off; with it, the share set on the
    «Звук» tab (mixes: the sound; «Спросить»: CLAP and the sound)."""
    values = await settings.get_all(db)
    if not values["audio.enabled"]:
        return {"text": 1.0}
    if task == "mix":
        share = float(values["audio.mix_weight"])
        return weights_of({"text": 1 - share, "sound": share})
    share = float(values["audio.ask_weight"])
    clap = values["audio.clap"]
    return weights_of({"text": 1 - share, "clap": share * 0.7 if clap else 0, "sound": share * (0.3 if clap else 1)})


async def joint(db: AsyncSession, weights: dict[str, float]) -> Joint:
    """The joint space of the given spaces that have vectors in the library, with their means."""
    text_model = await settings.get(db, "embed.model")
    means = {}
    for space in weights:
        col = _column(space)
        stmt = _from().add_columns(func.avg(func.l2_normalize(col), type_=Vector())).where(*_valid(space, text_model))
        mean = (await db.execute(stmt)).scalar_one_or_none()
        if mean is not None:
            means[space] = np.asarray(mean, dtype=np.float32)
    return Joint(weights_of({s: w for s, w in weights.items() if s in means}), means)


async def seeds(db: AsyncSession, j: Joint, artists: list[str], limit: int = 200) -> dict[str, np.ndarray]:
    """The artists' sound in each active space: the mean of their tracks' unit vectors (not centred)."""
    if not artists:
        return {}
    text_model = await settings.get(db, "embed.model")
    out = {}
    for space in j.weights:
        col = _column(space)
        rows = (await db.execute(_from().add_columns(col).where(
            Track.artist_key.in_({artist_key(a) for a in artists}), *_valid(space, text_model)).limit(limit)
        )).scalars().all()
        if (v := mean_unit(list(rows))) is not None:
            out[space] = v
    return out


async def candidates(db: AsyncSession, j: Joint, queries: dict[str, np.ndarray], user: User | None = None,
                     rest_days: float | None = None, exclude_artists: set[str] | None = None,
                     only_artists: Collection[str] | None = None, pool: int = POOL) -> list[Candidate]:
    """The tracks near the (centred) queries, best first; relevance and vector are in the joint space. With a user
    and `rest_days`: without what they played within those days and what they disliked. `only_artists`: just their
    tracks (names, any case; compared as kurai/artists.py's keys, guests and all)."""
    text_model = await settings.get(db, "embed.model")
    queries = {s: q for s, q in queries.items() if q is not None and s in j.weights}
    if not queries:
        return []
    filters = []
    if rest_days is not None and user is not None:
        disliked = select(Listen.track_id).where(Listen.user_id == user.id, Listen.rating.in_([1, 2]))
        filters.append(Track.id.not_in(disliked))
        if rest_days > 0:
            since = datetime.now(UTC) - timedelta(days=rest_days)
            recent = select(Listen.track_id).where(Listen.user_id == user.id, Listen.last_played >= since)
            filters.append(Track.id.not_in(recent))
    if exclude_artists:
        filters.append(Track.artist_key.not_in({artist_key(a) for a in exclude_artists}))
    if only_artists is not None:
        filters.append(Track.artist_key.in_({artist_key(a) for a in only_artists}))
    ids: set[str] = set()
    for space, q in queries.items():
        # the nearest by raw cosine: the query moved back from the centred space is close enough for a pool
        raw = (q + j.means[space]).tolist()
        stmt = (_from().add_columns(Track.id).where(*_valid(space, text_model), *filters)
                .order_by(_column(space).cosine_distance(raw)).limit(pool))
        ids.update((await db.execute(stmt)).scalars())
    if not ids:
        return []
    query = j.vector(queries)
    out = []
    for tid, (artist, raw) in (await track_vectors(db, ids)).items():
        vector = j.item(raw)
        out.append(Candidate(tid, artist, vector, float(np.dot(vector, query))))
    out.sort(key=lambda c: -c.relevance)
    return out


async def track_vectors(db: AsyncSession, ids: Collection[str]) -> dict[str, tuple[str, dict[str, Any]]]:
    """The tracks' artist and raw vectors by space (those of the current models only)."""
    text_model = await settings.get(db, "embed.model")
    rows = (await db.execute(_from().add_columns(
        Track.id, Track.artist, Track.embedding, Track.embedding_model, TrackAudio.effnet, TrackAudio.effnet_model,
        TrackAudio.clap, TrackAudio.clap_model, TrackAudio.error).where(Track.id.in_(list(ids))))).all()
    return {tid: (artist, {"text": emb if emb_model == text_model else None,
                           "sound": effnet if effnet_model == EFFNET_MODEL and not error else None,
                           "clap": clap if clap_model == CLAP_MODEL and not error else None})
            for tid, artist, emb, emb_model, effnet, effnet_model, clap, clap_model, error in rows}


async def played(db: AsyncSession, user: User, ids: Collection[str]) -> set[str]:
    """Which of the tracks the user has ever played (the rest are new to them)."""
    if not ids:
        return set()
    return set((await db.execute(select(Listen.track_id).where(
        Listen.user_id == user.id, Listen.play_count > 0, Listen.track_id.in_(list(ids))))).scalars())
