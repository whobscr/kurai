"""The recommendation arithmetic, without the database: taste vectors, the joint vector space, choosing a varied set
of tracks (MMR), and ordering them so neighbours sound alike. Pure functions, so they're easy to test."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np


def listen_weight(play_count: int, starred: bool, rating: int, last_played: datetime | None,
                  now: datetime | None = None, half_life_days: float = 120) -> float:
    """How much one track says about a user's taste: plays (with diminishing returns), a star, a rating (1-2 count
    against), and recency (older plays fade with a half-life)."""
    weight = math.log1p(play_count) + (2.0 if starred else 0.0)
    if rating:
        weight += (rating - 3) * 1.0
    if weight <= 0:
        return weight
    if last_played is not None:
        now = now or datetime.now(UTC)
        age_days = max(0.0, (now - last_played).total_seconds() / 86400)
        weight *= 0.5 ** (age_days / half_life_days) * 0.7 + 0.3  # never below 30%: old favourites still count
    return weight


def normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def taste_vector(vectors: list[np.ndarray], weights: list[float]) -> np.ndarray | None:
    """The weighted mean of liked tracks' unit vectors, minus a share of the disliked ones; None without positives.
    Not normalized: it stays on the tracks' scale so Joint can centre it like a track."""
    pos = [(v, w) for v, w in zip(vectors, weights, strict=True) if w > 0]
    if not pos:
        return None
    liked = sum(normalize(v) * w for v, w in pos) / sum(w for _, w in pos)
    neg = [(v, -w) for v, w in zip(vectors, weights, strict=True) if w < 0]
    if neg:
        liked = liked - 0.3 * sum(normalize(v) * w for v, w in neg) / sum(w for _, w in neg)
    return liked


def mean_unit(vectors: list[np.ndarray]) -> np.ndarray | None:
    """The mean of unit vectors (e.g. an artist's tracks), on the tracks' scale like a taste."""
    return np.mean([normalize(np.asarray(v, dtype=np.float32)) for v in vectors], axis=0) if vectors else None


def blend(parts: list[tuple[np.ndarray | None, float]]) -> np.ndarray | None:
    """A query from several directions (e.g. a request's text and the user's taste), by weight."""
    present = [(normalize(v), w) for v, w in parts if v is not None]
    if not present:
        return None
    return normalize(sum(v * w for v, w in present))


@dataclass(frozen=True)
class Candidate:
    id: str
    artist: str
    vector: np.ndarray
    relevance: float  # cosine similarity to the query, -1..1


@dataclass(frozen=True)
class Joint:
    """Several vector spaces as one (the tags' text, the sound, CLAP). Each part is centred on its library mean, so
    cosine measures how tracks differ rather than the direction all music shares, and is scaled by √weight: the dot
    product of two joint vectors is the weighted sum of their parts' cosines. A missing part counts as «average»."""

    weights: dict[str, float]  # active spaces only, summing to 1
    means: dict[str, np.ndarray]  # each space's mean of unit vectors

    def center(self, space: str, v) -> np.ndarray:
        """A unit vector, or a mean of unit vectors (a taste, an artist), centred and normalized."""
        return normalize(np.asarray(v, dtype=np.float32) - self.means[space])

    def vector(self, centred: dict[str, np.ndarray | None]) -> np.ndarray:
        return np.concatenate([centred[s] * math.sqrt(w) if centred.get(s) is not None else np.zeros_like(self.means[s])
                               for s, w in self.weights.items()]).astype(np.float32)

    def item(self, raw: dict[str, Any]) -> np.ndarray:
        """A track's joint vector from its raw vectors (any scale)."""
        return self.vector({s: self.center(s, normalize(np.asarray(v, dtype=np.float32)))
                            for s, v in raw.items() if v is not None and s in self.weights})


def weights_of(parts: dict[str, float]) -> dict[str, float]:
    """Positive weights, scaled to sum to 1."""
    positive = {k: w for k, w in parts.items() if w > 0}
    total = sum(positive.values())
    return {k: w / total for k, w in positive.items()} if total else {}


def mmr_select(candidates: list[Candidate], count: int, diversity: float = 0.35,
               max_per_artist: int = 2) -> list[Candidate]:
    """Maximal marginal relevance: each next track is the most relevant one that is least like those already chosen,
    with at most `max_per_artist` tracks of one artist."""
    chosen: list[Candidate] = []
    per_artist: dict[str, int] = {}
    pool = list(candidates)
    while pool and len(chosen) < count:
        best, best_score = None, -math.inf
        for c in pool:
            if per_artist.get(c.artist.lower(), 0) >= max_per_artist:
                continue
            redundancy = max((float(np.dot(normalize(c.vector), normalize(s.vector))) for s in chosen), default=0.0)
            score = (1 - diversity) * c.relevance - diversity * redundancy
            if score > best_score:
                best, best_score = c, score
        if best is None:
            break
        chosen.append(best)
        per_artist[best.artist.lower()] = per_artist.get(best.artist.lower(), 0) + 1
        pool.remove(best)
    return chosen


def order_smoothly(tracks: list[Candidate]) -> list[Candidate]:
    """Starts with the most relevant track and always continues with the most similar remaining one, so neighbours
    sound alike; artists are kept apart where possible."""
    if len(tracks) < 3:
        return list(tracks)
    remaining = sorted(tracks, key=lambda c: -c.relevance)
    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        options = [c for c in remaining if c.artist.lower() != last.artist.lower()] or remaining
        nxt = max(options, key=lambda c: float(np.dot(normalize(c.vector), normalize(last.vector))))
        ordered.append(nxt)
        remaining.remove(nxt)
    return ordered
