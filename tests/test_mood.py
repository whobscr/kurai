import datetime

import numpy as np

from kurai.audio.tags import mood_of
from kurai.db.models import WaveEvent
from kurai.recommend.mood import SessionMood, mood_bonus, mood_of_tags, session_mood, shadow_correlation

NOW = datetime.datetime.now(datetime.UTC)


class _A:
    def __init__(self, moods=None, danceability=None, voice=None):
        self.moods = moods or {}
        self.danceability = danceability
        self.voice = voice
        self.bpm = None
        self.key = None
        self.styles: list = []
        self.themes: list = []


def _event(skipped, minutes_ago, track_id):
    e = WaveEvent(user_id=1, track_id=track_id, skipped=skipped, seconds=100)
    e.at = NOW - datetime.timedelta(minutes=minutes_ago)
    return e


def test_mood_of_none_when_no_tags():
    assert mood_of(None) is None
    assert mood_of(_A()) is None
    assert mood_of(_A(moods={"happy": 0.9})) is not None


def test_mood_of_valence_sign():
    happy = mood_of(_A(moods={"happy": 0.9, "sad": 0.1, "relaxed": 0.2}))
    sad = mood_of(_A(moods={"happy": 0.1, "sad": 0.9, "relaxed": 0.2}))
    assert happy[0] > sad[0]


def test_mood_of_tags_wrapper():
    assert mood_of_tags({"happy": 0.9}, 0.8, 0.6) is not None
    assert mood_of_tags({}, None, None) is None


def test_session_mood_no_evidence():
    m = session_mood([], {}, 45, NOW)
    assert m.vector is None
    assert m.regime == "no_evidence"


def test_session_mood_from_accepted_not_skips():
    v = np.array([1.0, 0.0], dtype=np.float32)
    events = [_event(False, 1, "a"), _event(True, 1, "b")]
    m = session_mood(events, {"a": v, "b": -v}, 45, NOW)
    assert m.vector is not None
    assert float(np.dot(m.vector, v)) > 0.9  # skips don't pull the mood


def test_bad_day_regime():
    v = np.array([1.0, 0.0], dtype=np.float32)
    events = [_event(True, 1, "s1"), _event(True, 2, "s2"), _event(True, 3, "s3"), _event(False, 1, "a")]
    m = session_mood(events, {k: v for k in ("s1", "s2", "s3", "a")}, 45, NOW)
    assert m.regime == "bad_day"


def test_mood_bonus_neutral_for_unknown_track_and_off_cases():
    mood = SessionMood(np.array([1.0, 0.0]), 1.0, 3.0, 1.0, "ok")
    assert mood_bonus(None, mood, 0.25) == 0.0
    assert mood_bonus(np.array([1.0, 0.0]), mood, 0.0) == 0.0
    assert mood_bonus(np.array([1.0, 0.0]), mood, 0.25) > 0
    capped = mood_bonus(np.array([1.0, 0.0]), mood, 0.25)
    assert capped <= 0.25


def test_mood_bonus_off_on_bad_day():
    mood = SessionMood(np.array([1.0, 0.0]), 1.0, 3.0, 1.0, "bad_day")
    assert mood_bonus(np.array([1.0, 0.0]), mood, 0.25) == 0.0


def test_shadow_gate_passes_when_completed_closer():
    fit = np.array([1.0, 0.0], dtype=np.float32)
    off = np.array([-1.0, 0.0], dtype=np.float32)
    events = [_event(False, 1, "c1"), _event(False, 2, "c2"), _event(True, 1, "s1")]
    vectors = {"c1": fit, "c2": fit, "s1": off}
    out = shadow_correlation(events, vectors, 45, NOW)
    assert out["passes"] is True
    assert out["completed"] > out["skipped"]


def test_shadow_gate_needs_both_sides():
    fit = np.array([1.0, 0.0], dtype=np.float32)
    events = [_event(False, 1, "c1"), _event(False, 2, "c2")]
    out = shadow_correlation(events, {"c1": fit, "c2": fit}, 45, NOW)
    assert out["passes"] is False  # no skips to compare against


def test_centered_floor_and_noise():
    from kurai.recommend.mood import centered
    mean = np.array([0.0, 0.2], dtype=np.float32)
    std = np.array([0.2, 0.2], dtype=np.float32)
    assert centered(np.array([0.0, 0.2]), mean, std) is None          # neutral
    assert centered(np.array([0.001, 0.2]), mean, std) is None        # noise
    v = centered(np.array([0.5, 0.2]), mean, std)
    assert v is not None and abs(float(np.linalg.norm(v)) - 1) < 1e-5


def test_coherence_is_freshness_weighted():
    v = np.array([1.0, 0.0], dtype=np.float32)
    m = session_mood([_event(False, 1, "f"), _event(False, 179, "o")], {"f": v, "o": -v}, 45, NOW)
    assert m.confidence > 0.2  # an opposite track at the session edge no longer zeroes confidence


def test_bad_day_without_mood_coverage():
    events = [_event(True, i, f"s{i}") for i in range(1, 6)] + [_event(False, 1, "a")]
    assert session_mood(events, {}, 45, NOW).regime == "bad_day"


def test_shadow_gate_needs_two_completions():
    v = np.array([1.0, 0.0], dtype=np.float32)
    out = shadow_correlation([_event(False, 1, "c"), _event(True, 1, "s")],
                             {"c": v, "s": np.array([0.99, 0.14], dtype=np.float32)}, 45, NOW)
    assert out["passes"] is False  # one completion can't define the mood it is scored against


async def test_track_vectors_filters_current_effnet_model(db, monkeypatch):
    from kurai.audio import EFFNET_MODEL
    from kurai.recommend import mood

    mean = np.array([0.0, 0.0], dtype=np.float32)
    std = np.array([1.0, 1.0], dtype=np.float32)

    async def fake_read_stats(_db):
        return mean, std

    monkeypatch.setattr(mood, "read_stats", fake_read_stats)

    executed_statements = []

    class FakeResult:
        def all(self):
            return [
                (
                    "model-test",
                    {"happy": 0.9, "sad": 0.1},
                    0.8,
                    0.5,
                )
            ]

    async def fake_execute(statement):
        executed_statements.append(statement)
        return FakeResult()

    monkeypatch.setattr(db, "execute", fake_execute)

    vectors = await mood.track_vectors(db, {"model-test"})

    assert "model-test" in vectors
    assert len(vectors) == 1

    # Ensure the query filters out analyses produced by older models.
    sql = str(executed_statements[0])
    assert "effnet_model" in sql
    assert EFFNET_MODEL in executed_statements[0].compile().params.values()

    expected = mood.centered(
        mood.mood_of_tags(
            {"happy": 0.9, "sad": 0.1},
            0.8,
            0.5,
        ),
        mean,
        std,
    )
    np.testing.assert_allclose(vectors["model-test"], expected)

