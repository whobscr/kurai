"""The pure parts: recommendation arithmetic, the night window, the gateway's routes, security, model answers."""

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from kurai import llm, outbound, security
from kurai.jobs import window
from kurai.recommend import mixing
from kurai.recommend.vectors import (
    Candidate,
    adjust,
    arrange,
    listen_weight,
    mmr_select,
    shuffle_apart,
    taste_vector,
)

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


# ---------- taste and selection ----------


def test_the_taste_memory_sets_how_fast_and_how_far_a_listen_fades():
    """«Память вкуса»: the weight halves every half-life since the last play, down to the floor; floor 0 forgets."""
    fresh = listen_weight(5, False, 0, NOW, NOW, 30, 0.3)
    month = listen_weight(5, False, 0, NOW - timedelta(days=30), NOW, 30, 0.3)
    assert abs(month / fresh - (0.5 * 0.7 + 0.3)) < 1e-9
    assert listen_weight(5, False, 0, NOW - timedelta(days=30), NOW, 120, 0.3) > month  # a longer memory
    ancient = NOW - timedelta(days=3000)
    assert abs(listen_weight(5, False, 0, ancient, NOW, 30, 0.3) / fresh - 0.3) < 1e-6
    assert listen_weight(5, False, 0, ancient, NOW, 30, 0.0) / fresh < 1e-6  # floor 0: forgotten


def test_listen_weight_counts_plays_stars_ratings_and_fades():
    assert listen_weight(0, False, 0, None) == 0
    assert listen_weight(10, True, 5, NOW, NOW) > listen_weight(10, False, 0, NOW, NOW) > 0
    assert listen_weight(0, False, 1, None) < 0  # a bad rating counts against
    fresh = listen_weight(5, False, 0, NOW, NOW)
    old = listen_weight(5, False, 0, NOW - timedelta(days=365), NOW)
    assert 0.3 * fresh <= old < fresh  # fades, but never below 30%


def test_taste_vector_follows_likes_and_avoids_dislikes():
    a, b = np.array([1.0, 0.0]), np.array([0.0, 1.0])
    assert taste_vector([a, b], [-1.0, -2.0]) is None
    v = taste_vector([a, b], [3.0, -1.0])
    assert v[0] > 0.9 and v[1] < 0


def _cand(i, artist, vec, rel):
    return Candidate(str(i), artist, np.array(vec, dtype=float), rel)


def test_mmr_caps_artists_and_prefers_variety():
    same = [_cand(i, "A", [1, 0.01 * i], 0.95 - 0.01 * i) for i in range(5)]
    other = [_cand(10, "B", [0, 1], 0.6)]
    chosen = mmr_select(same + other, 3, diversity=0.5, max_per_artist=2)
    assert [c.artist for c in chosen].count("A") == 2
    assert any(c.artist == "B" for c in chosen)


def test_arrange_keeps_neighbours_close_and_artists_apart():
    tracks = [_cand(0, "A", [1, 0], 0.9), _cand(1, "A", [0.99, 0.1], 0.8), _cand(2, "B", [0.95, 0.2], 0.7),
              _cand(3, "C", [0, 1], 0.6)]
    ordered = arrange(tracks)
    assert ordered[0].id == "0"
    assert ordered[1].artist != "A"  # the same artist isn't played twice in a row when avoidable
    assert len(ordered) == 4
    # a wider gap: two others between the same artist's tracks, as long as others are left
    many = [_cand(i, "A" if i < 2 else f"X{i}", [1, 0.01 * i], 1 - 0.01 * i) for i in range(6)]
    order = [c.artist for c in arrange(many, smooth=False, artist_gap=3)]
    assert order.index("A", 1) - order.index("A") >= 4
    assert [c.id for c in arrange(tracks, smooth=False, artist_gap=0)] == ["0", "1", "2", "3"]  # by relevance


def test_a_resting_artist_is_lowered_and_randomness_shuffles():
    import random

    pool = [_cand(i, f"Artist {i}", [1, 0.1 * i], 0.9 - 0.05 * i) for i in range(10)]
    lowered = adjust(pool, resting={"artist 0"}, rest_penalty=1.0)
    assert [c.id for c in lowered].index("0") > 0  # no longer first
    assert [c.id for c in adjust(pool)] == [c.id for c in pool]  # no randomness, no pause: as it was
    shuffled = {tuple(c.id for c in adjust(pool, randomness=1.0, rng=random.Random(seed))) for seed in range(5)}
    assert len(shuffled) > 1


def test_a_resting_artist_is_left_out_while_others_can_fill_the_mix():
    """«Пауза для исполнителя»: an artist heard lately isn't in the new version even when they're the best match by
    far — unless the others can't fill it; then they come back, after everyone else."""
    import random

    mix = mixing.Mix("wave", {"mix.wave.randomness": 0, "mix.wave.artist_max": 2, "mix.wave.size": 10})
    best = [_cand(f"a{i}", "Favourite", [1, 0.01 * i], 0.99 - 0.001 * i) for i in range(5)]
    others = [_cand(f"o{i}", f"Other {i}", [0.5, 0.1 * i], 0.5 - 0.01 * i) for i in range(12)]
    picked = mixing.assemble(best + others, [], set(), mix, 10, resting={"favourite"}, rng=random.Random(1))
    assert len(picked) == 10 and "Favourite" not in {c.artist for c in picked}
    assert {c.artist for c in mixing.assemble(best + others, [], set(), mix, 10, rng=random.Random(1))} >= {"Favourite"}
    few = mixing.assemble(best + others[:9], [], set(), mix, 10, resting={"favourite"}, rng=random.Random(1))
    assert len(few) == 10 and [c.artist for c in few].count("Favourite") == 1  # 9 others: one comes back


def test_a_mix_takes_its_shares_of_new_known_and_related():
    import random

    mix = mixing.Mix("wave", {"mix.wave.new_share": 0.5, "mix.wave.related_share": 0.25, "mix.wave.randomness": 0,
                              "mix.wave.artist_max": 5})
    pool = [_cand(f"n{i}", f"N{i}", [1, 0.01 * i], 0.95 - 0.01 * i) for i in range(10)]
    pool += [_cand(f"k{i}", f"K{i}", [1, 0.02 * i], 0.95 - 0.01 * i) for i in range(10)]
    near = [_cand(f"r{i}", f"R{i}", [0, 1], 0.2) for i in range(10)]
    known = {f"k{i}" for i in range(10)} | {"r0"}
    chosen = mixing.assemble(pool, near, known, mix, 8, rng=random.Random(1))
    kinds = [c.kind for c in chosen]
    assert kinds.count("new") == 4 and kinds.count("related") == 2 and kinds.count("known") == 2
    assert "r0" not in {c.id for c in chosen}  # the related artists' unheard tracks first
    # no known tracks: the new ones fill their place
    alone = mixing.assemble(pool[:10], [], set(), mix, 8, rng=random.Random(1))
    assert len(alone) == 8 and {c.kind for c in alone} == {"new"}


def test_a_share_takes_only_what_fits_and_the_halves_continue_each_other():
    """New and known come from the better half of the pool: familiar tracks far from the query don't fill the known
    share (what fits fills it instead). A second part of a version continues the first: no repeats, artists counted."""
    import random

    mix = mixing.Mix("wave", {"mix.wave.new_share": 0.5, "mix.wave.related_share": 0, "mix.wave.randomness": 0,
                              "mix.wave.artist_max": 1})
    near_new = [_cand(f"n{i}", f"N{i}", [1, 0.01 * i], 0.9 - 0.01 * i) for i in range(10)]
    far_known = [_cand(f"k{i}", f"K{i}", [0, 1], 0.1 - 0.01 * i) for i in range(10)]
    known = {c.id for c in far_known}
    picked = mixing.choose(near_new + far_known, [], known, mix, 6, rng=random.Random(1))
    assert {c.id for c in picked} <= {c.id for c in near_new}  # the far familiar ones didn't get in
    more = mixing.choose(near_new + far_known, [], known, mix, 4, rng=random.Random(1), chosen=picked)
    assert len(more) == 10 and len({c.id for c in more}) == 10 and more[:6] == picked
    assert len({c.artist for c in more}) == 10  # one track an artist across both parts


def test_an_artist_comes_back_within_the_pause_by_its_shape():
    """«Форма паузы»: one draw per artist and play (the same at every rebuild), the chance of being back by a part x
    of the pause x^shape, never after the pause."""
    keys = [f"1|artist {i}|2026-11-16T10:00:00" for i in range(4000)]
    for shape in (1, 2, 8):
        back = [mixing.returns_after(6, shape, k) for k in keys]
        assert max(back) <= 6 and back == [mixing.returns_after(6, shape, k) for k in keys]
        for part in (0.25, 0.5, 0.75):
            share = sum(b <= 6 * part for b in back) / len(back)
            assert abs(share - part ** shape) < 0.03, (shape, part, share)
    assert mixing.returns_after(6, 2, keys[0]) != mixing.returns_after(6, 2, keys[0].replace("10:00", "11:00"))


def test_mixing_parameters_are_checked():
    assert mixing.validate("wave", {"mix.wave.new_share": 0.5, "mix.wave.related_share": 0.4}) is None
    assert "не больше 1" in mixing.validate("wave", {"mix.wave.new_share": 0.8, "mix.wave.related_share": 0.4})
    assert "от 1 до 20" in mixing.validate("wave", {"mix.wave.artist_max": 0})
    assert "mix.playlists.now_weight" not in mixing.defaults()  # the wave's only
    assert mixing.standard("playlists").taste_weight == 0.15 and mixing.standard("wave").size == 40


def test_a_request_s_tempo_energy_and_vocals_are_checked_against_the_sound():
    from types import SimpleNamespace

    from kurai.audio.tags import fits

    def track(bpm, dance, relaxed, voice=0.9):
        return SimpleNamespace(bpm=bpm, danceability=dance, moods={"relaxed": relaxed, "party": dance,
                                                                    "aggressive": 0.3}, voice=voice)

    fast, slow = track(150, 0.8, 0.1), track(70, 0.2, 0.9)
    assert fits(fast, bpm_min=120) is True and fits(slow, bpm_min=120) is False
    assert fits(track(80, 0.9, 0.05), bpm_min=120) is True  # a drum-and-bass track read at half its speed
    assert fits(slow, wanted_energy="calm") is True and fits(fast, wanted_energy="calm") is False
    assert fits(fast, vocals="instrumental") is False and fits(track(150, 0.8, 0.1, 0.1), vocals="instrumental")
    assert fits(None, bpm_min=120) is None and fits(fast) is None  # nothing known, nothing asked


# ---------- the night window ----------


def test_window_same_day_and_across_midnight():
    tz = UTC
    assert window.current(datetime(2026, 1, 1, 7, tzinfo=tz), "06:00", "10:00")
    assert window.current(datetime(2026, 1, 1, 11, tzinfo=tz), "06:00", "10:00") is None
    start, end = window.current(datetime(2026, 1, 2, 1, tzinfo=tz), "23:00", "05:00")
    assert start.day == 1 and end.day == 2
    assert window.current(datetime(2026, 1, 2, 12, tzinfo=tz), "23:00", "05:00") is None
    assert window.hours("06:00", "10:00") == 4 and window.hours("23:00", "05:00") == 6


# ---------- the outbound gateway ----------


def test_routes_follow_the_checkboxes_and_never_fall_back():
    values = {"outbound.proxy": "socks5h://10.0.0.2:1080", "outbound.user": "u", "outbound.password": "p@ss",
              "outbound.dns": "", **{f"outbound.via.{n}": n == "telegram" for n in
                                     ("telegram", "llm", "webpush", "models")}}
    tg = outbound.route_from(values, "telegram")
    assert tg.via_proxy and tg.proxy == "socks5h://u:p%40ss@10.0.0.2:1080"
    assert not outbound.route_from(values, "llm").via_proxy
    with pytest.raises(KeyError):
        outbound.route_from(values, "nonsense")


def test_dns_parsing_and_local_urls():
    assert outbound.parse_dns("10.8.0.1:5353") == ("10.8.0.1", 5353)
    assert outbound.parse_dns("[fd00::1]:53") == ("fd00::1", 53)
    with pytest.raises(ValueError):
        outbound.parse_dns("dns.example.org")
    assert outbound.is_local_url("http://llama:8080/v1")
    assert outbound.is_local_url("http://127.0.0.1:11434/v1")
    assert not outbound.is_local_url("https://api.anthropic.com")


# ---------- security ----------


def test_passwords_secrets_totp_recovery():
    h = security.hash_password("correct horse")
    assert security.verify_password(h, "correct horse") and not security.verify_password(h, "wrong")
    assert security.decrypt(security.encrypt("token")) == "token"
    assert security.decrypt("garbage") == ""
    import pyotp

    secret = security.new_totp_secret()
    step = security.totp_step(secret, pyotp.TOTP(secret).now())
    assert step is not None
    assert security.totp_step(secret, pyotp.TOTP(secret).now(), after=step) is None  # a code works once
    assert security.totp_step(secret, "000000x") is None
    assert security.looks_like_recovery_code("ABC123-def456") and not security.looks_like_recovery_code("123456")
    codes, hashes = security.new_recovery_codes()
    left = security.use_recovery_code(hashes, codes[0].upper())
    assert left is not None and len(left) == len(hashes) - 1
    assert security.use_recovery_code(left, codes[0]) is None  # one use only


# ---------- model answers ----------


def test_thinking_is_stripped_and_json_parsed():
    assert llm.strip_thinking("<think>hmm</think>\nAnswer") == "Answer"
    assert llm.strip_thinking("lost opening</think> Answer") == "Answer"
    assert llm.parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(llm.ModelError):
        llm.parse_json("not json")


def test_openai_compatible_body_by_server_type():
    from kurai.llm.providers import OpenAICompatible

    values = {"llm.base_url": "http://llama:8080/v1", "llm.api_key": "", "llm.model": "qwen3:8b",
              "llm.server_type": "llama_cpp", "llm.timeout": 10, "outbound.proxy": "", "outbound.dns": ""}
    body = OpenAICompatible(values)._body(llm.TextRequest("sys", "hi", {"type": "object"}, think=False))
    assert body["messages"][1]["content"].endswith("/no_think")
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert "max_tokens" in body
    thinking = OpenAICompatible(values)._body(llm.TextRequest("sys", "hi", think=True))
    assert not thinking["messages"][1]["content"].endswith("/no_think")
    openai = OpenAICompatible({**values, "llm.server_type": "openai", "llm.model": "gpt"})._body(
        llm.TextRequest("s", "p"))
    assert "max_completion_tokens" in openai and "max_tokens" not in openai


def test_times_are_shown_in_the_servers_time_zone(monkeypatch):
    import time

    from kurai.web.templating import templates

    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        moment = datetime(2026, 11, 15, 21, 30, tzinfo=UTC)  # as the database hands it over
        render = templates.env.from_string('{{ t | local }} · {{ t | local("%d.%m") }}').render
        assert render(t=moment) == "16.11 00:30 · 16.11"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_wave_watcher_tells_plays_from_skips():
    from kurai.recommend.wave import Watcher

    w = Watcher()
    t0 = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)
    entry = {"username": "ann", "playerId": "1", "duration": 240}
    # already playing when the watching began: how long it played is unknown, so it gives no event
    assert w.update(t0, [{**entry, "id": "a"}]) == []
    assert w.update(t0 + timedelta(seconds=30), [{**entry, "id": "b"}]) == []
    assert w.update(t0 + timedelta(seconds=45), [{**entry, "id": "c"}]) == [("ann", "b", True, 15)]  # skipped
    # another app of hers is followed apart
    assert w.update(t0 + timedelta(seconds=60), [{**entry, "id": "c"}, {**entry, "playerId": "2", "id": "x"}]) == []
    assert w.update(t0 + timedelta(seconds=45 + 200), [{**entry, "id": "d"}]) == [("ann", "c", False, 200)]
    assert ("ann", "2") not in w.playing  # that app stopped


def test_the_bot_goes_through_a_socks5h_proxy():
    """aiogram refuses «socks5h://» (the worker crashed on it at every start); the bot gets «socks5://», which it
    resolves through the proxy anyway."""
    from aiohttp_socks import ProxyConnector

    from kurai.telegram.bot import session_for

    session = session_for(outbound.Route("socks5h://user:pw@10.0.0.2:1080", None))
    assert session._connector_type is ProxyConnector and session._connector_init["rdns"] is True
    assert session._connector_init["host"] == "10.0.0.2" and session._connector_init["username"] == "user"
    assert session_for(outbound.Route(None, None)).proxy is None


async def test_a_failing_bot_never_stops_the_worker(monkeypatch):
    import asyncio

    from kurai.telegram import bot

    calls = []

    async def broken():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("Invalid scheme component: socks5h")
        raise asyncio.CancelledError  # the test's way out

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(bot, "_run_bot", broken)
    monkeypatch.setattr(bot.asyncio, "sleep", no_wait)
    with pytest.raises(asyncio.CancelledError):
        await bot.run_bot()
    assert len(calls) == 3  # tried again after each failure instead of raising


def test_job_results_read_as_written():
    """Cyrillic as letters, not \\uXXXX; still escaped as HTML by the page."""
    from kurai.web.templating import templates

    html = templates.env.from_string("{{ r | readable }}").render(r={"skipped": "вкус ещё не известен", "x": "<b>"})
    assert "вкус ещё не известен" in html and "\\u0432" not in html and "&lt;b&gt;" in html


def test_shuffle_apart_shuffles_with_no_artist_twice_in_a_row_wherever_possible():
    """«Любимое»'s order: everything kept, a different order each time, and never more neighbours of one artist than
    the counts force (an artist with more than half the tracks can't be kept apart entirely)."""
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.randint(1, 40)
        artists = [rng.choice("ABCDEFG"[:rng.randint(1, 7)]) for _ in range(n)]
        ordered = shuffle_apart(artists, lambda a: a, rng)
        assert sorted(ordered) == sorted(artists)
        side_by_side = sum(a == b for a, b in zip(ordered, ordered[1:], strict=False))
        assert side_by_side == max(0, 2 * max(Counter(artists).values()) - n - 1)
    tracks = [(i, "ABCDE"[i % 5]) for i in range(30)]
    def by_artist(t):
        return t[1]

    assert shuffle_apart(tracks, by_artist, random.Random(1)) != shuffle_apart(tracks, by_artist, random.Random(2))
    assert shuffle_apart([], lambda t: t) == []
