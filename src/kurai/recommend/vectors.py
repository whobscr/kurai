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