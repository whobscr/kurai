"""The heavy part, only in the audio image: which model files are needed, and running Essentia and CLAP on a file.
Everything here blocks; the service calls it from a thread, one analysis at a time."""

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from kurai.audio import CLAP_MODEL, CLAP_REVISION, EFFNET_MODEL
from kurai.audio.tags import MOOD_HEADS, positive, top
from kurai.i18n import _

log = logging.getLogger(__name__)

ESSENTIA_URL = "https://essentia.upf.edu/models/"
EFFNET_PATH = f"feature-extractors/discogs-effnet/{EFFNET_MODEL}"
# the small classifiers on top of the EffNet vectors: name -> path on Essentia's site
HEADS = {
    "styles": "classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1",
    "themes": "classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1",
    "danceability": "classification-heads/danceability/danceability-discogs-effnet-1",
    "voice": "classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1",
    **{m: f"classification-heads/mood_{m}/mood_{m}-discogs-effnet-1" for m in MOOD_HEADS},
}
CLAP_URL = f"https://huggingface.co/{CLAP_MODEL}/resolve/{CLAP_REVISION}/"
CLAP_FILES = ["config.json", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json",
              "special_tokens_map.json", "vocab.json", "merges.txt", "pytorch_model.bin"]

# the middle three minutes say enough about a track's sound and keep long tracks from taking long
SEGMENT_SECONDS = 180
# CLAP hears 10 seconds at a time: three windows across the track
CLAP_WINDOWS = (0.2, 0.5, 0.8)
CLAP_SECONDS = 10
CLAP_RATE = 48000
EFFNET_RATE = 16000


def model_files(models_dir: Path, clap: bool, essentia: bool = True) -> list[tuple[str, Path]]:
    """(url, local path) of every file the analysis needs (Essentia's models with their metadata, .json; CLAP's
    files); `essentia=False`: CLAP's only (a description's vector needs no more)."""
    files = []
    for path in [EFFNET_PATH, *HEADS.values()] if essentia else []:
        name = path.rsplit("/", 1)[-1]
        files += [(f"{ESSENTIA_URL}{path}.{ext}", models_dir / "essentia" / f"{name}.{ext}") for ext in ("pb", "json")]
    if clap:
        files += [(CLAP_URL + f, models_dir / "clap" / f) for f in CLAP_FILES]
    return files


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


@dataclass
class Result:
    effnet: list[float] | None = None
    bpm: float | None = None
    key: str | None = None
    danceability: float | None = None
    voice: float | None = None
    moods: dict[str, float] = field(default_factory=dict)
    styles: list[list[Any]] = field(default_factory=list)
    themes: list[list[Any]] = field(default_factory=list)
    clap: list[float] | None = None


def _middle(x: np.ndarray, rate: int, seconds: int) -> np.ndarray:
    n = seconds * rate
    if len(x) <= n:
        return x
    start = (len(x) - n) // 2
    return x[start:start + n]


class Analyzer:
    """Loads each model on first use and keeps it. CLAP is shared with the text requests, under a lock."""

    def __init__(self, models_dir: Path, threads: int):
        self.dir = models_dir
        self.threads = threads
        self._effnet = None
        self._heads: dict[str, tuple[Any, list[str]]] = {}
        self._clap = None
        self._clap_lock = threading.Lock()
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> dict[str, bool]:
        return {"effnet": self._effnet is not None, "clap": self._clap is not None}

    # ---------- loading ----------

    def _meta(self, path: str) -> dict:
        return json.loads((self.dir / "essentia" / f"{path.rsplit('/', 1)[-1]}.json").read_text())

    def _essentia(self) -> None:
        if self._effnet is not None:
            return
        import essentia
        import essentia.standard as es

        essentia.log.infoActive = False
        essentia.log.warningActive = False
        meta = self._meta(EFFNET_PATH)
        embeddings = next(o["name"] for o in meta["schema"]["outputs"] if o.get("output_purpose") == "embeddings")
        graph = str(self.dir / "essentia" / f"{EFFNET_MODEL}.pb")
        heads = {}
        for name, path in HEADS.items():
            m = self._meta(path)
            output = next(o["name"] for o in m["schema"]["outputs"] if o.get("output_purpose") == "predictions")
            graph_file = str(self.dir / "essentia" / f"{path.rsplit('/', 1)[-1]}.pb")
            heads[name] = (es.TensorflowPredict2D(graphFilename=graph_file, input=m["schema"]["inputs"][0]["name"],
                                                  output=output), list(m["classes"]))
        self._effnet = es.TensorflowPredictEffnetDiscogs(graphFilename=graph, output=embeddings)
        self._heads = heads

    def _clap_model(self):
        with self._load_lock:
            if self._clap is None:
                import torch
                from transformers import ClapModel, ClapProcessor

                torch.set_num_threads(self.threads)
                path = str(self.dir / "clap")
                self._clap = (ClapModel.from_pretrained(path).eval(), ClapProcessor.from_pretrained(path))
        return self._clap

    # ---------- analysis ----------

    def analyze(self, path: str, effnet: bool, clap: bool) -> Result:
        import essentia.standard as es

        result = Result()
        if effnet:
            self._essentia()
            x = es.MonoLoader(filename=path, sampleRate=EFFNET_RATE, resampleQuality=4)()
            if len(x) < EFFNET_RATE * 3:
                raise ValueError(_("в файле меньше трёх секунд звука"))
            x = _middle(x, EFFNET_RATE, SEGMENT_SECONDS)
            frames = self._effnet(x)
            result.effnet = _unit(np.mean(frames, axis=0)).tolist()
            for name, (model, classes) in self._heads.items():
                preds = model(frames)
                if name in ("styles", "themes"):
                    setattr(result, name, top(preds, classes))
                elif name == "danceability":
                    result.danceability = positive(preds, classes, "danceable")
                elif name == "voice":
                    result.voice = positive(preds, classes, "voice")
                else:
                    result.moods[name] = positive(preds, classes, MOOD_HEADS[name])
            result.bpm = round(float(es.PercivalBpmEstimator(sampleRate=EFFNET_RATE)(x)), 1) or None
            key, scale, _strength = es.KeyExtractor(sampleRate=EFFNET_RATE)(x)
            result.key = f"{key} {scale}"
        if clap:
            result.clap = self._clap_audio(path)
        return result

    def _clap_audio(self, path: str) -> list[float]:
        import essentia.standard as es
        import torch

        whole = es.MonoLoader(filename=path, sampleRate=CLAP_RATE, resampleQuality=4)()
        n = CLAP_SECONDS * CLAP_RATE
        if len(whole) <= n:
            windows = [whole]
        else:
            windows = [whole[int((len(whole) - n) * p):int((len(whole) - n) * p) + n] for p in CLAP_WINDOWS]
        model, processor = self._clap_model()
        with self._clap_lock, torch.inference_mode():
            out = model.get_audio_features(**processor(audio=windows, sampling_rate=CLAP_RATE, return_tensors="pt"))
        vectors = getattr(out, "pooler_output", out).numpy()
        return _unit(np.mean([_unit(v) for v in vectors], axis=0)).tolist()

    def clap_text(self, texts: list[str]) -> list[list[float]]:
        import torch

        model, processor = self._clap_model()
        with self._clap_lock, torch.inference_mode():
            out = model.get_text_features(**processor(text=texts, padding=True, return_tensors="pt"))
        return [_unit(v).tolist() for v in getattr(out, "pooler_output", out).numpy()]
