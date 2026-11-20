"""Mood as a context layer over the wave. A track's mood comes from its audio tags (audio/tags.mood_of); the
session's current mood comes from the tracks the user ACCEPTED — never from skips, because a skip is ambiguous
(wrong track, wrong artist, wrong context, wrong mood, or just being busy). The permanent taste is never touched
by mood: a bad evening must not rewrite what the user generally likes (library.build_taste stays the source of
the long-term profile).

Shadow first: wave.plan computes the session mood and reports it in the job's result, but does not rerank by it
until the admin turns «Учитывать настроение» on (mix.wave.mood). Even then the bonus is capped and never
hard-filters, so a small library can't run out of candidates."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from kurai.audio.tags import mood_of
from kurai.db.models import WaveEvent

# accepted tracks needed before the session mood is trusted at full confidence
REQUIRED_EFFECTIVE_TRACKS = 3.0


@dataclass
class SessionMood:
    vector: np.ndarray | None   # unit vector (valence, arousal); None: no evidence
    confidence: float           # 0..1
    effective_n: float          # freshness-weighted count of accepted tracks with known mood
    coherence: float            # how alike the accepted tracks are in mood space, -1..1
    regime: str                 # "ok" | "bad_day" | "no_evidence"


def mood_of_tags(moods: dict | None, danceability: float | None, voice: float | None) -> np.ndarray | None:
    """A track's mood from raw tag columns. Wraps tags.mood_of for callers that hold columns, not an Analysed."""
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


def session_mood(events: list[WaveEvent], tag_vectors: dict[str, np.ndarray], half_life_min: float,
                 now: datetime) -> SessionMood:
    """The session's current mood from the tracks the user ACCEPTED (skips are ignored for inference).

    `tag_vectors`: track id -> mood vector, only for tracks whose mood is known. Freshness decays with
    `half_life_min`. Confidence blends the amount of evidence with its coherence.

    A session with many skips and few acceptances is flagged "bad_day": there the right response is to damp ALL
    implicit negatives and lean on the familiar, not to explain each skip by mood-match — mood inferred from
    acceptances cannot represent a state where the user accepts nothing."""
    accepted = [e for e in events if not e.skipped]
    skipped = sum(1 for e in events if e.skipped)
    if not accepted:
        return SessionMood(None, 0.0, 0.0, 0.0, "no_evidence" if skipped == 0 else "bad_day")

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
        return SessionMood(None, 0.0, 0.0, 0.0, "no_evidence")

    total = sum(weights)
    mean = sum(v * w for v, w in zip(vectors, weights, strict=True)) / total
    norm = float(np.linalg.norm(mean))
    if norm < 1e-6:
        return SessionMood(None, 0.0, total, 0.0, "no_evidence")
    mean = mean / norm

    coherence = float(np.mean([float(np.dot(mean, v / (np.linalg.norm(v) or 1.0))) for v in vectors]))
    evidence = min(1.0, total / REQUIRED_EFFECTIVE_TRACKS)
    confidence = evidence * max(0.0, coherence)
    regime = "bad_day" if skipped >= 2 * max(1, len(accepted)) else "ok"
    return SessionMood(mean, confidence, total, coherence, regime)


def mood_bonus(track_mood: np.ndarray | None, mood: SessionMood, strength: float) -> float:
    """How many spreads to add to a candidate's relevance for its fit to the session mood. Unknown track mood ->
    0 (neutral), never a penalty for unanalysed tracks. Capped at ±strength so mood steers but never overrides
    taste. No bonus at all on a bad day (see session_mood)."""
    if track_mood is None or mood.vector is None or strength <= 0 or mood.regime != "ok":
        return 0.0
    fit = float(np.dot(track_mood / (np.linalg.norm(track_mood) or 1.0), mood.vector))
    return max(-strength, min(strength, strength * mood.confidence * fit))


def shadow_correlation(events: list[WaveEvent], tag_vectors: dict[str, np.ndarray], half_life_min: float,
                       now: datetime) -> dict:
    """The acceptance check for turning the mood bonus on: are completed tracks, on average, closer to the session
    mood than skipped ones? `passes` is the gate. If they aren't, the feature is wrong, not untuned — leave the
    checkbox off."""
    mood = session_mood(events, tag_vectors, half_life_min, now)
    if mood.vector is None:
        return {"completed": None, "skipped": None, "passes": False}
    comp: list[float] = []
    skip: list[float] = []
    for e in events:
        v = tag_vectors.get(e.track_id)
        if v is None:
            continue
        fit = float(np.dot(v / (np.linalg.norm(v) or 1.0), mood.vector))
        (skip if e.skipped else comp).append(fit)
    if not comp or not skip:
        return {"completed": None, "skipped": None, "passes": False}
    c, s = float(np.mean(comp)), float(np.mean(skip))
    return {"completed": round(c, 3), "skipped": round(s, 3), "passes": c > s}
