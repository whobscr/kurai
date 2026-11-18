"""The recommendation arithmetic, without the database: taste vectors, the joint vector space, choosing a varied set
of tracks (MMR), ordering them so neighbours sound alike, and shuffling them with artists spread out. Pure functions,
so they're easy to test."""

import math
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from kurai.artists import artist_key


def listen_weight(play_count: int, starred: bool, rating: int, last_played: datetime | None,
                  now: datetime | None = None, half_life_days: float = 120, floor: float = 0.3) -> float:
    """How much one track says about a user's taste: plays (with diminishing returns), a star, a rating, and recency:
    the weight halves every `half_life_days` since the last play, never below `floor` of it («Память вкуса»).

    An explicit «no» (a rating of 1–2) outranks the plays and the star: they say the track was heard, not that it is
    liked, and used to be added to it — a 👎 on a track played seven times, or on any starred track, came out positive
    and kept pulling the taste toward itself and its artist into the favourites. Both directions decay alike now:
    a 👎 from years ago used to keep its full weight while an old love fell to `floor`."""
    if rating in (1, 2):
        weight = -2.0 if rating == 1 else -1.0
    else:
        weight = math.log1p(play_count) + (2.0 if starred else 0.0)
        if rating:
            weight += (rating - 3) * 1.0
    if weight == 0:
        return 0.0
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
    returns it with up to `count` more.

    The unit vectors are made once and the redundancy is one matrix product: they used to be recomputed for every
    (candidate, chosen) pair at every step — some 700k norms per part of a wave."""
    chosen = list(chosen or [])
    taken = {c.id for c in chosen}
    per_artist: dict[str, int] = {}
    for c in chosen:
        per_artist[artist_key(c.artist)] = per_artist.get(artist_key(c.artist), 0) + 1
    pool = [c for c in candidates if c.id not in taken]
    if not pool or count <= 0:
        return chosen
    units = {c.id: normalize(c.vector) for c in pool}
    keys = {c.id: artist_key(c.artist) for c in pool}
    picked = [normalize(c.vector) for c in chosen]
    matrix = np.asarray(picked, dtype=np.float32) if picked else None
    added = 0
    while pool and added < count:
        best, best_score = None, -math.inf
        for c in pool:
            if per_artist.get(keys[c.id], 0) >= max_per_artist:
                continue
            u = units[c.id]
            redundancy = float(np.max(matrix @ u)) if matrix is not None else 0.0
            score = (1 - diversity) * c.relevance - diversity * redundancy
            if score > best_score:
                best, best_score = c, score
        if best is None:
            break
        chosen.append(best)
        per_artist[keys[best.id]] = per_artist.get(keys[best.id], 0) + 1
        u = units[best.id]
        matrix = u[None, :] if matrix is None else np.vstack((matrix, u))
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
    """The relevance as a mix weighs it: an artist heard lately (`resting`, by kurai/artists.py's key) lowered by
    `rest_penalty` spreads; a track like one of `away` (the skipped ones' vectors) lowered by `push` × AWAY spreads
    × its likeness (the cosine, from 0) to the nearest of them; random noise of `randomness` spreads added. Best
    first."""
    scale = scale if scale is not None else spread(candidates)
    rng = rng or random.Random()
    resting = resting or set()
    away = [normalize(np.asarray(a, dtype=np.float32)) for a in away or []] if push else []
    out = []
    for c in candidates:
        r = c.relevance
        if artist_key(c.artist) in resting:
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
            max_per_artist: int, chosen: list[Candidate] | None = None, relax: bool = True) -> list[Candidate]:
    """A mix of parts (new, related, known…), each `(candidates, how many)`, chosen in turn by MMR so they don't
    repeat each other; what a part lacks is filled from all of them. Continues `chosen` (another part of the same
    mix: no repeats, the artists counted): returns it with up to `size` more.

    `relax`: the limit per artist is the last thing to hold. It used to be absolute, so a pool of three artists gave
    a six-track wave instead of forty; now it rises until the mix is full or the candidates run out. Pass False to
    keep it absolute."""
    chosen = list(chosen or [])
    goal = len(chosen) + size
    for pool, count in parts:
        chosen = mmr_select(pool, min(count, goal - len(chosen)), diversity, max_per_artist, chosen)
    if len(chosen) < goal:
        seen: set[str] = set()
        everything: list[Candidate] = []
        for c in sorted((c for pool, _n in parts for c in pool), key=lambda c: -c.relevance):
            if c.id not in seen:  # a track both near the query and of a «слушают вместе» artist
                seen.add(c.id)
                everything.append(c)
        chosen = mmr_select(everything, goal - len(chosen), diversity, max_per_artist, chosen)
        cap = max_per_artist
        while relax and len(chosen) < goal and cap < goal:
            cap += 1
            before = len(chosen)
            chosen = mmr_select(everything, goal - len(chosen), diversity, cap, chosen)
            if len(chosen) == before:  # nothing left to take at any limit
                break
    return chosen[:goal]


def arrange(tracks: list[Candidate], smooth: bool = True, artist_gap: int = 1) -> list[Candidate]:
    """The playing order: starts with the most relevant track; each next one is the most similar remaining one
    (`smooth`) or the most relevant, with no artist of the last `artist_gap` tracks where another is left. An artist
    with so many tracks left that they need every (gap + 1)-th place from here goes now, or they'd end up side by side
    at the end."""
    remaining = sorted(tracks, key=lambda c: -c.relevance)
    if len(remaining) < 3:
        return remaining
    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        recent = {artist_key(c.artist) for c in ordered[-artist_gap:]} if artist_gap > 0 else set()
        options = [c for c in remaining if artist_key(c.artist) not in recent] or remaining
        if artist_gap > 0:
            left = Counter(artist_key(c.artist) for c in remaining)
            crowded = [c for c in options if left[artist_key(c.artist)] * (artist_gap + 1) >= len(remaining)]
            options = crowded or options
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
        groups.setdefault(artist_key(artist(item)), []).append(item)
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