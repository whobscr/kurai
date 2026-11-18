import numpy as np

from kurai.recommend.vectors import Candidate, compose, listen_weight, mmr_select


def test_dislike_outranks_plays_and_star():
    for plays in (0, 7, 50, 200):
        assert listen_weight(plays, False, 1, None) < 0
        assert listen_weight(plays, True, 1, None) < 0
    assert listen_weight(50, False, 2, None) < 0


def test_other_ratings_unchanged():
    assert listen_weight(0, False, 3, None) == 0
    assert listen_weight(0, False, 5, None) == 2.0
    assert listen_weight(0, True, 0, None) == 2.0


def _c(i, artist, rel, dim=32):
    return Candidate(f"t{i}", artist, np.random.default_rng(i).normal(size=dim).astype(np.float32), rel)


def test_compose_relaxes_artist_cap_on_scarcity():
    scarce = [_c(i, f"A{i % 3}", 0.9 - i * 0.001) for i in range(90)]
    assert len(compose([(scarce, 40)], 40, 0.35, 2, relax=True)) == 40
    assert len(compose([(scarce, 40)], 40, 0.35, 2, relax=False)) == 6


def test_compose_stops_on_exhausted_pool():
    few = [_c(i, f"A{i}", 0.9) for i in range(5)]
    assert len(compose([(few, 40)], 40, 0.35, 2)) == 5


def test_mmr_respects_artist_cap():
    pool = [_c(i, f"A{i % 2}", 0.9 - i * 0.001) for i in range(50)]
    chosen = mmr_select(pool, 10, 0.35, 2)
    per: dict[str, int] = {}
    for c in chosen:
        per[c.artist] = per.get(c.artist, 0) + 1
    assert all(n <= 2 for n in per.values())