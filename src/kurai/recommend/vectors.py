"""The recommendation arithmetic, without the database: taste vectors, the joint vector space, choosing a varied set
of tracks (MMR), ordering them so neighbours sound alike, and shuffling them with artists spread out. Pure functions,
so they're easy to test."""

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np


def listen_weight(play_count: int, starred: bool, rating: int, last_played: datetime | None,
                  now: datetime | None = None, half_life_days: float = 120, floor: float = 0.3) -> float:
    """How much one track says about a user's taste: plays (with diminishing returns), a star, a rating (1-2 count
    against), and recency: the weight halves every `half_life_days` since the last play, never below `floor` of it
    («Память вкуса»)."""
    weight = math.log1p(play_count) + (2.0 if starred else 0.0)
    if rating:
        weight += (rating - 3) * 1.0
    if weight <= 0:
        return weight
    if last_played is not None:
        now = now or datetime.now(UTC)
        age_days = max(0.0, (now - last_played).total_seconds() / 86400)
        weight *= 0.5 ** (age_days / max(half_life_days, 1e-6)) * (1 - floor) + floor
    return weight


def normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def taste_vector(vectors: list[np.ndarray], weights: list[float], push: float = 0.3) -> np.ndarray | None:
    """The weighted mean of liked tracks' unit vectors, minus `push` of the disliked ones'; None without positives.
    Not normalized: it stays on the tracks' scale so Joint can centre it like a track."""
    pos = [(v, w) for v, w in zip(vectors, weights, strict=True) if w > 0]
    if not pos:
        return None
    liked = sum(normalize(v) * w for v, w in pos) / sum(w for _, w in pos)
    neg = [(v, -w) for v, w in zip(vectors, weights, strict=True) if w < 0]
    if neg:
        liked = liked - push * sum(normalize(v) * w for v, w in neg) / sum(w for _, w in neg)
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
    relevance: float  # cosine similarity to the query, -1..1 (lowered or shuffled by `adjust`)
    kind: str = ""  # which part of a mix it came for: "new", "known", "related", "popular" (the admins' preview)


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


def mmr_select(candidates: list[Candidate], count: int, diversity: float = 0.35, max_per_artist: int = 2,
               chosen: list[Candidate] | None = None) -> list[Candidate]:
    """Maximal marginal relevance: each next track is the most relevant one that is least like those already chosen,
    with at most `max_per_artist` tracks of one artist. Continues `chosen` (what other parts of a mix took) when given:
    returns it with up to `count` more."""
    chosen = list(chosen or [])
    taken = {c.id for c in chosen}
    per_artist: dict[str, int] = {}
    for c in chosen:
        per_artist[c.artist.lower()] = per_artist.get(c.artist.lower(), 0) + 1
    pool = [c for c in candidates if c.id not in taken]
    added = 0
    while pool and added < count:
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
        added += 1
    return chosen


def spread(candidates: list[Candidate]) -> float:
    """How much the candidates' relevance differs (its standard deviation, never ~0): the scale «Случайность» and an
    artist's pause are measured in, so they mean the same whatever the vector spaces."""
    if len(candidates) < 2:
        return 0.05
    return max(0.01, float(np.std([c.relevance for c in candidates])))


AWAY = 6.0  # how many spreads a track sounding like a skipped one loses at «Отталкивание от пропущенного» 1


def adjust(candidates: list[Candidate], randomness: float = 0.0, resting: set[str] | None = None,
           rest_penalty: float = 0.0, rng: random.Random | None = None, scale: float | None = None,
           away: list[np.ndarray] | None = None, push: float = 0.0) -> list[Candidate]:
    """The relevance as a mix weighs it: an artist heard lately (`resting`, lower case) lowered by `rest_penalty`
    spreads; a track like one of `away` (the skipped ones' vectors) lowered by `push` × AWAY spreads × its likeness
    (the cosine, from 0) to the nearest of them; random noise of `randomness` spreads added. Best first."""
    scale = scale if scale is not None else spread(candidates)
    rng = rng or random.Random()
    resting = resting or set()
    away = [normalize(np.asarray(a, dtype=np.float32)) for a in away or []] if push else []
    out = []
    for c in candidates:
        r = c.relevance
        if c.artist.lower() in resting:
            r -= rest_penalty * scale * 2
        if away:
            v = normalize(c.vector)
            r -= push * AWAY * scale * max(0.0, max(float(np.dot(v, a)) for a in away))
        if randomness:
            r += rng.gauss(0, randomness * scale)
        out.append(replace(c, relevance=r))
    out.sort(key=lambda c: -c.relevance)
    return out


def compose(parts: list[tuple[list[Candidate], int]], size: int, diversity: float,
            max_per_artist: int, chosen: list[Candidate] | None = None) -> list[Candidate]:
    """A mix of parts (new, related, known…), each `(candidates, how many)`, chosen in turn by MMR so they don't
    repeat each other; what a part lacks is filled from all of them. Continues `chosen` (another part of the same
    mix: no repeats, the artists counted): returns it with up to `size` more."""
    chosen = list(chosen or [])
    goal = len(chosen) + size
    for pool, count in parts:
        chosen = mmr_select(pool, min(count, goal - len(chosen)), diversity, max_per_artist, chosen)
    if len(chosen) < goal:
        everything = sorted((c for pool, _n in parts for c in pool), key=lambda c: -c.relevance)
        chosen = mmr_select(everything, goal - len(chosen), diversity, max_per_artist, chosen)
    return chosen[:goal]


def arrange(tracks: list[Candidate], smooth: bool = True, artist_gap: int = 1) -> list[Candidate]:
    """The playing order: starts with the most relevant track; each next one is the most similar remaining one
    (`smooth`) or the most relevant, with no artist of the last `artist_gap` tracks where another is left."""
    remaining = sorted(tracks, key=lambda c: -c.relevance)
    if len(remaining) < 3:
        return remaining
    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        recent = {c.artist.lower() for c in ordered[-artist_gap:]} if artist_gap > 0 else set()
        options = [c for c in remaining if c.artist.lower() not in recent] or remaining
        if smooth:
            nxt = max(options, key=lambda c: float(np.dot(normalize(c.vector), normalize(last.vector))))
        else:
            nxt = options[0]
        ordered.append(nxt)
        remaining.remove(nxt)
    return ordered


def shuffle_apart[T](items: list[T], artist: Callable[[T], str], rng: random.Random | None = None) -> list[T]:
    """A random order with no artist twice in a row wherever the items allow it (no artist has more than half of
    them). Each next item is a random one of another artist than the last, an artist with more left being likelier;
    an artist with at least half of what's left must come now, or its items would end up side by side."""
    rng = rng or random.Random()
    groups: dict[str, list[T]] = {}
    for item in items:
        groups.setdefault((artist(item) or "").casefold(), []).append(item)
    for group in groups.values():
        rng.shuffle(group)
    ordered: list[T] = []
    last = None
    while groups:
        left = sum(len(g) for g in groups.values())
        allowed = [a for a in groups if a != last] or [last]
        crowded = [a for a in allowed if 2 * len(groups[a]) >= left]
        last = rng.choice(crowded) if crowded else rng.choices(allowed, [len(groups[a]) for a in allowed])[0]
        ordered.append(groups[last].pop())
        if not groups[last]:
            del groups[last]
    return ordered
