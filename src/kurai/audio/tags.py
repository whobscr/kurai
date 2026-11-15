"""Turning the heads' predictions into tags and the tags into words for the model. Pure functions (numpy only)."""

from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

# the binary heads: name -> the class that means «yes»
MOOD_HEADS = {"happy": "happy", "sad": "sad", "relaxed": "relaxed", "aggressive": "aggressive", "party": "party",
              "acoustic": "acoustic", "electronic": "electronic"}
MOOD_LABELS = {"happy": "радостное", "sad": "грустное", "relaxed": "спокойное", "aggressive": "агрессивное",
               "party": "для вечеринки", "acoustic": "акустическое", "electronic": "электронное"}
MOOD_THRESHOLD = 0.6


class Analysed(Protocol):
    bpm: float | None
    key: str | None
    danceability: float | None
    voice: float | None
    moods: dict
    styles: list
    themes: list


def positive(frames: np.ndarray, classes: Sequence[str], yes: str) -> float:
    """A binary head's probability of `yes`, averaged over the track's frames."""
    return round(float(np.mean(np.asarray(frames)[:, list(classes).index(yes)])), 3)


def top(frames: np.ndarray, classes: Sequence[str], n: int = 5, minimum: float = 0.1) -> list[list[Any]]:
    """A multi-label head's strongest classes over the whole track, as [[name, probability], …]."""
    mean = np.mean(np.asarray(frames), axis=0)
    order = np.argsort(-mean)[:n]
    return [[classes[i], round(float(mean[i]), 3)] for i in order if mean[i] >= minimum]


def _style(name: str) -> str:
    return name.split("---")[-1]


def describe(a: Analysed) -> str:
    """One track's sound in a line of Russian, for the model's prompts."""
    parts = []
    if a.bpm:
        parts.append(f"темп ~{round(a.bpm)} BPM")
    if a.key:
        parts.append(f"тональность {a.key}")
    if a.danceability is not None and a.danceability >= MOOD_THRESHOLD:
        parts.append("танцевальное")
    if a.voice is not None:
        parts.append("с вокалом" if a.voice >= 0.5 else "инструментал")
    moods = [MOOD_LABELS[k] for k, v in sorted((a.moods or {}).items(), key=lambda kv: -kv[1])
             if v >= MOOD_THRESHOLD and k in MOOD_LABELS]
    if moods:
        parts.append("настроение: " + ", ".join(moods))
    if a.styles:
        parts.append("стили: " + ", ".join(_style(s) for s, _ in a.styles[:3]))
    if a.themes:
        parts.append("темы: " + ", ".join(t for t, _ in a.themes[:3]))
    return "; ".join(parts)


def describe_set(items: Sequence[Analysed]) -> str:
    """A playlist's sound in a line: its usual tempo, its prevailing moods and styles."""
    if not items:
        return ""
    parts = []
    bpms = [a.bpm for a in items if a.bpm]
    if bpms:
        parts.append(f"темп обычно ~{round(float(np.median(bpms)))} BPM")
    moods: dict[str, list[float]] = {}
    for a in items:
        for k, v in (a.moods or {}).items():
            moods.setdefault(k, []).append(v)
    strong = [MOOD_LABELS[k] for k, vs in sorted(moods.items(), key=lambda kv: -np.mean(kv[1]))
              if k in MOOD_LABELS and np.mean(vs) >= MOOD_THRESHOLD]
    if strong:
        parts.append("настроение: " + ", ".join(strong))
    styles = Counter(_style(s) for a in items for s, _ in (a.styles or [])[:2])
    if styles:
        parts.append("стили: " + ", ".join(s for s, _ in styles.most_common(4)))
    return "; ".join(parts)
