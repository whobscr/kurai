"""Turning the heads' predictions into tags and the tags into words for the model. Pure functions (numpy only)."""

from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

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


ENERGETIC, CALM = 0.55, 0.4


def energy(a: Analysed) -> float | None:
    """How energetic a track sounds, 0..1: danceability, «party» and «aggressive» up, «relaxed» down. None unknown."""
    moods = a.moods or {}
    parts = [v for v in (a.danceability, moods.get("party"), moods.get("aggressive")) if v is not None]
    if moods.get("relaxed") is not None:
        parts.append(1 - moods["relaxed"])
    return float(np.mean(parts)) if parts else None


def fits(a: Analysed | None, bpm_min: int = 0, bpm_max: int = 0, wanted_energy: str = "any",
         vocals: str = "any") -> bool | None:
    """Whether a track's analysis meets a request's tempo (0: no bound), energy («calm» | «medium» | «energetic») and
    vocals («instrumental» | «vocal»); None when nothing asked of it is known. The tempo estimate often reads a fast
    track at half its speed (or a slow one at double): that counts when the energy agrees."""
    if a is None:
        return None
    level = energy(a)
    checks: list[bool] = []
    if (bpm_min or bpm_max) and a.bpm:
        def within(b: float) -> bool:
            return (not bpm_min or b >= bpm_min) and (not bpm_max or b <= bpm_max)

        checks.append(within(a.bpm) or (level is not None and (
            (within(a.bpm * 2) and level >= ENERGETIC) or (within(a.bpm / 2) and level <= CALM))))
    if wanted_energy != "any" and level is not None:
        checks.append({"calm": level <= CALM, "energetic": level >= ENERGETIC,
                       "medium": CALM - 0.05 <= level <= ENERGETIC + 0.1}.get(wanted_energy, True))
    if vocals != "any" and a.voice is not None:
        checks.append(a.voice >= 0.5 if vocals == "vocal" else a.voice < 0.5)
    return all(checks) if checks else None


def mood_of(a: Analysed | None) -> np.ndarray | None:
    """A track's mood as a 2-vector: valence (happy/sad), arousal (party/aggressive/danceability vs relaxed).
    Pure, no DB. None when none of the mood heads produced a usable probability (effnet not run, or unanalysed);
    an axis with no inputs of its own is 0 (a happy-only track is bright, its arousal unknown, not unanalysed).
    The acoustic/electronic heads are deliberately left out: they're timbre, collinear with the sound embedding,
    and would make mood a proxy for genre. Raw bpm is left out too — PercivalBpmEstimator octave-errors freely,
    and arousal already carries tempo via danceability/party. `None` is the unknown-track signal: callers skip
    such tracks when inferring session mood and give them mood bonus 0 (neutral), never a spurious negative."""
    if a is None:
        return None
    moods = a.moods or {}
    if not moods:
        return None
    happy = moods.get("happy")
    sad = moods.get("sad")
    party = moods.get("party")
    aggressive = moods.get("aggressive")
    relaxed = moods.get("relaxed")
    dance = getattr(a, "danceability", None)
    if not any(v is not None for v in (happy, sad, party, aggressive, relaxed, dance)):
        return None
    valence_parts = [v for v in (happy,) if v is not None]
    if sad is not None:
        valence_parts.append(-sad)
    if aggressive is not None:
        valence_parts.append(-0.5 * aggressive)
    arousal_parts = [v for v in (party, aggressive, dance) if v is not None]
    if relaxed is not None:
        arousal_parts.append(-relaxed)
    valence = float(np.mean(valence_parts)) if valence_parts else 0.0
    arousal = float(np.mean(arousal_parts)) if arousal_parts else 0.0
    out = np.array([valence, arousal], dtype=np.float32)
    norm = np.linalg.norm(out)
    return out / norm if norm > 0 else out
