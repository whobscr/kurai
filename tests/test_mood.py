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