"""The pure parts: recommendation arithmetic, the night window, the gateway's routes, security, model answers."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from kurai import llm, outbound, security
from kurai.jobs import window
from kurai.recommend.vectors import (
    Candidate,
    listen_weight,
    mmr_select,
    order_smoothly,
    taste_vector,
)

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


# ---------- taste and selection ----------


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


def test_order_smoothly_keeps_neighbours_close_and_artists_apart():
    tracks = [_cand(0, "A", [1, 0], 0.9), _cand(1, "A", [0.99, 0.1], 0.8), _cand(2, "B", [0.95, 0.2], 0.7),
              _cand(3, "C", [0, 1], 0.6)]
    ordered = order_smoothly(tracks)
    assert ordered[0].id == "0"
    assert ordered[1].artist != "A"  # the same artist isn't played twice in a row when avoidable
    assert len(ordered) == 4


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
    assert security.verify_totp(secret, pyotp.TOTP(secret).now())
    assert not security.verify_totp(secret, "000000x")
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
