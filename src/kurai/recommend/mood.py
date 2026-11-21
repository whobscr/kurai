"""Mood as a context layer over the wave. A track's mood comes from its audio tags (audio/tags.mood_of), z-scored
across the library (mood.centered) so cosine measures difference, not the direction all music shares. The session's
current mood comes from the tracks the user ACCEPTED — never from skips (ambiguous), explicit 👎 (a deliberate «no»,
not a mood signal) or «Не сейчас». Permanent taste is touched only through the opt-in context weighting in
library.build_taste; a bad evening damps implicit negatives there and in the wave, it does not rewrite what the user
generally likes.

Shadow first: wave.plan always reports the session mood; it reranks only when «Учитывать настроение» (mix.wave.mood)
is on, by a capped bonus that never hard-filters and never touches the «слушают вместе» part."""

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai.audio import EFFNET_MODEL
from kurai.audio.tags import mood_of
from kurai.db.models import SpaceMean, TrackAudio, WaveEvent

REQUIRED_EFFECTIVE_TRACKS = 3.0
MOOD_FLOOR = 0.5            # z-score magnitude below which a track's mood is neutral/unknown: no evidence, no bonus
STATS_SPACE = "mood"
MIN_TRACKS_FOR_STATS = 10


@dataclass
class SessionMood:
    vector: np.ndarray | None
    confidence: float
    effective_n: float
    coherence: float
    regime: str             # "ok" | "bad_day" | "no_evidence"


def mood_of_tags(moods: dict | None, danceability: float | None, voice: float | None) -> np.ndarray | None:
    """A track's raw mood axes from tag columns. Wraps tags.mood_of for callers that hold columns, not an Analysed."""
    class _A:
        bpm = None
        key = None
        styles: list = []
        themes: list = []

    a = _A()
    a.moods = moods or {}
    a.danceability = danceability
    a.voice = voice
    return mood_of(a)


def centered(raw: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray | None:
    """Z-score each axis across the library, then unit-normalize. A track near the library mean on both axes (or with
    only numerical noise) is None — unknown/neutral, never a spurious confident direction."""
    z = (np.asarray(raw, dtype=np.float32) - mean) / std
    n = float(np.linalg.norm(z))
    return (z / n).astype(np.float32) if n >= MOOD_FLOOR else None


async def _compute_stats(db: AsyncSession) -> tuple[np.ndarray, np.ndarray] | None:
    rows = (await db.execute(select(TrackAudio.moods, TrackAudio.danceability).where(
        TrackAudio.effnet_model == EFFNET_MODEL, TrackAudio.error.is_(None)))).all()
    raws = [v for moods, dance in rows if (v := mood_of_tags(moods, dance, None)) is not None]
    if len(raws) < MIN_TRACKS_FOR_STATS:
        return None
    arr = np.stack(raws)
    return arr.mean(axis=0).astype(np.float32), np.maximum(arr.std(axis=0), 1e-3).astype(np.float32)


async def refresh_stats(db: AsyncSession) -> bool:
    """Persist the library's mood mean/std. Called by retrieval.refresh_means (nightly + model change). No commit."""
    stats = await _compute_stats(db)
    if stats is None:
        return False
    mean, std = stats
    await db.execute(SpaceMean.__table__.delete().where(
        (SpaceMean.space == STATS_SPACE) & (SpaceMean.model == EFFNET_MODEL)))
    db.add(SpaceMean(space=STATS_SPACE, model=EFFNET_MODEL, vector=[*mean.tolist(), *std.tolist()],
                     computed_at=datetime.now(UTC)))
    return True


async def read_stats(db: AsyncSession) -> tuple[np.ndarray, np.ndarray] | None:
    row = (await db.execute(select(SpaceMean).where(SpaceMean.space == STATS_SPACE,
                                                    SpaceMean.model == EFFNET_MODEL))).scalar_one_or_none()
    if row is not None:
        v = np.asarray(row.vector, dtype=np.float32)
        return v[:2], v[2:]
    return await _compute_stats(db)


async def track_vectors(db: AsyncSession, ids: set[str]) -> dict[str, np.ndarray]:
    """Centered unit mood vectors of the given tracks; unanalysed or neutral tracks are simply absent."""
    if not ids:
        return {}
    stats = await read_stats(db)
    if stats is None:
        return {}
    mean, std = stats
    rows = (await db.execute(
        select(
            TrackAudio.track_id,
            TrackAudio.moods,
            TrackAudio.danceability,
            TrackAudio.voice,
        ).where(
            TrackAudio.track_id.in_(list(ids)),
            TrackAudio.effnet_model == EFFNET_MODEL,
            TrackAudio.error.is_(None),
        )
    )).all()
    out: dict[str, np.ndarray] = {}
    for tid, moods, dance, voice in rows:
        raw = mood_of_tags(moods, dance, voice)
        if raw is not None and (v := centered(raw, mean, std)) is not None:
            out[tid] = v
    return out


def _contextual(e: WaveEvent) -> bool:
    """Neither an explicit 👎 nor «Не сейчас» — those are deliberate, not ambiguous contextual signals."""
    return not getattr(e, "disliked", False) and not getattr(e, "not_now", False)


def session_mood(events: list[WaveEvent], tag_vectors: dict[str, np.ndarray], half_life_min: float,
                 now: datetime) -> SessionMood:
    """The session mood from ACCEPTED tracks. The regime is decided from the counts alone, so a bad day is still
    detected when the accepted tracks have no mood analysis. Coherence is freshness-weighted like the mean."""
    accepted = [e for e in events if not e.skipped and _contextual(e)]
    skipped = sum(1 for e in events if e.skipped and _contextual(e))
    regime = "bad_day" if skipped >= 2 * max(1, len(accepted)) else ("ok" if accepted else "no_evidence")
    vectors: list[np.ndarray] = []
    weights: list[float] = []
    for e in accepted:
        v = tag_vectors.get(e.track_id)
        if v is None:
            continue
        age_min = (now - e.at).total_seconds() / 60
        weights.append(0.5 ** (age_min / max(half_life_min, 1e-6)))
        vectors.append(v)
    if not vectors:
        return SessionMood(None, 0.0, 0.0, 0.0, regime)
    total = sum(weights)
    mean = sum(v * w for v, w in zip(vectors, weights, strict=True)) / total
    norm = float(np.linalg.norm(mean))
    if norm < 1e-6:
        return SessionMood(None, 0.0, total, 0.0, regime)
    mean = mean / norm
    coherence = float(sum(w * float(np.dot(mean, v / (np.linalg.norm(v) or 1.0)))
                          for v, w in zip(vectors, weights, strict=True)) / total)
    evidence = min(1.0, total / REQUIRED_EFFECTIVE_TRACKS)
    confidence = evidence * max(0.0, coherence)
    return SessionMood(mean, confidence, total, coherence, regime)


def mood_bonus(track_mood: np.ndarray | None, mood: SessionMood, strength: float) -> float:
    """Spreads to add for fit to the session mood; unknown track mood -> 0, capped at ±strength, off on a bad day."""
    if track_mood is None or mood.vector is None or strength <= 0 or mood.regime != "ok":
        return 0.0
    fit = float(np.dot(track_mood / (np.linalg.norm(track_mood) or 1.0), mood.vector))
    return max(-strength, min(strength, strength * mood.confidence * fit))


def shadow_correlation(events: list[WaveEvent], tag_vectors: dict[str, np.ndarray], half_life_min: float,
                       now: datetime) -> dict:
    """The acceptance gate, leave-one-out: each completed track is scored against the mood of the OTHER accepted
    tracks, so a single completion can't trivially pass by defining the mood it is compared to."""
    accepted = [e for e in events if not e.skipped and _contextual(e)]
    skips = [e for e in events if e.skipped and _contextual(e)]
    comp: list[float] = []
    for e in accepted:
        v = tag_vectors.get(e.track_id)
        if v is None:
            continue
        m = session_mood([o for o in accepted if o is not e], tag_vectors, half_life_min, now)
        if m.vector is None:
            continue
        comp.append(float(np.dot(v / (np.linalg.norm(v) or 1.0), m.vector)))
    full = session_mood(accepted, tag_vectors, half_life_min, now)
    skip: list[float] = []
    if full.vector is not None:
        for e in skips:
            v = tag_vectors.get(e.track_id)
            if v is not None:
                skip.append(float(np.dot(v / (np.linalg.norm(v) or 1.0), full.vector)))
    if not comp or not skip:
        return {"completed": None, "skipped": None, "passes": False}
    c, s = float(np.mean(comp)), float(np.mean(skip))
    return {"completed": round(c, 3), "skipped": round(s, 3), "passes": c > s}
