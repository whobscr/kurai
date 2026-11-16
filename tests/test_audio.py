"""The audio analysis: tags and the joint space (pure), the analysis job with a fake analyzer, and mixes and
«Спросить» that follow the sound and CLAP. With KURAI_AUDIO_MODELS (a folder with the models, as the audio container
keeps them) the real models also run on a generated file."""

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import delete, select

from kurai import security, settings
from kurai.audio import CLAP_DIM, CLAP_MODEL, EFFNET_DIM, EFFNET_MODEL
from kurai.audio.analyze import needs
from kurai.audio.tags import describe, describe_set, positive, top
from kurai.db.models import Job, Listen, Playlist, Track, TrackAudio, User
from kurai.recommend.vectors import Joint, normalize, weights_of

# ---------- pure ----------


def test_heads_become_tags_and_words():
    frames = np.array([[0.9, 0.1], [0.7, 0.3]])
    assert positive(frames, ["happy", "non_happy"], "happy") == 0.8
    assert top(np.array([[0.05, 0.6, 0.3]]), ["a", "b", "c"], n=2) == [["b", 0.6], ["c", 0.3]]
    a = SimpleNamespace(bpm=121.4, key="A minor", danceability=0.9, voice=0.1,
                        moods={"relaxed": 0.8, "sad": 0.2, "electronic": 0.95},
                        styles=[["Electronic---Ambient", 0.4]], themes=[["calm", 0.3]])
    line = describe(a)
    assert "121 BPM" in line and "A minor" in line and "танцевальное" in line and "инструментал" in line
    assert "настроение: электронное, спокойное" in line and "Ambient" in line
    b = SimpleNamespace(bpm=90, key=None, danceability=None, voice=None, moods={"relaxed": 0.2}, styles=[],
                        themes=[])
    assert "~106 BPM" in describe_set([a, b]) and describe_set([]) == ""


def test_joint_space_is_a_weighted_sum_of_centred_cosines():
    rng = np.random.default_rng(0)
    means = {"text": rng.normal(size=4) * 0.1, "sound": rng.normal(size=6) * 0.1}
    j = Joint(weights_of({"text": 3, "sound": 1}), means)
    assert j.weights == {"text": 0.75, "sound": 0.25}
    x = {"text": rng.normal(size=4), "sound": rng.normal(size=6)}
    y = {"text": rng.normal(size=4), "sound": rng.normal(size=6)}
    cos = {s: float(np.dot(j.center(s, normalize(x[s])), j.center(s, normalize(y[s])))) for s in x}
    assert np.isclose(np.dot(j.item(x), j.item(y)), 0.75 * cos["text"] + 0.25 * cos["sound"], atol=1e-5)
    # a missing part counts as average: only the text part is compared
    assert np.isclose(np.dot(j.item({"text": x["text"]}), j.item(y)), 0.75 * cos["text"], atol=1e-5)
    assert weights_of({"text": 0, "sound": 0}) == {}


def test_what_a_track_still_needs():
    assert needs(None, 10, clap=True) == (True, True)
    done = SimpleNamespace(size=10, error=None, effnet_model=EFFNET_MODEL, clap_model=None)
    assert needs(done, 10, clap=False) == (False, False)
    assert needs(done, 10, clap=True) == (False, True)  # CLAP turned on later: only its part
    assert needs(done, 11, clap=False) == (True, False)  # another file
    broken = SimpleNamespace(size=10, error="bad file", effnet_model=None, clap_model=None)
    assert needs(broken, 10, clap=True) == (False, False)  # until it changes or an admin retries


# ---------- with the database ----------

TEXT_MODEL = "audio-test-embeddings"


@pytest.fixture
async def audio_on(db):
    """Audio analysis on, and a text-embedding model of its own so other tests' tracks don't take part."""
    before = await settings.get_all(db)
    keys = ("embed.model", "audio.enabled", "audio.clap", "audio.mix_weight", "audio.ask_weight")
    await settings.put(db, {"embed.model": TEXT_MODEL, "audio.enabled": True, "audio.clap": True})
    await db.commit()
    yield
    await settings.put(db, {k: before[k] for k in keys})
    await db.execute(delete(Track).where(Track.id.like("au%")))
    await db.execute(delete(Job).where(Job.kind.in_(["analyze_audio", "nightly"])))
    await db.commit()


def _unit(v):
    return (np.asarray(v) / np.linalg.norm(v)).tolist()


async def _library(db, rng, n=40):
    """Tracks whose tags say nothing (random text vectors) but whose sound falls in two clusters: au0..au19 sound
    «calm», au20..au39 «loud»; CLAP vectors follow the same split."""
    calm_s, loud_s = rng.normal(size=EFFNET_DIM), rng.normal(size=EFFNET_DIM)
    calm_c, loud_c = rng.normal(size=CLAP_DIM), rng.normal(size=CLAP_DIM)
    for i in range(n):
        calm = i < n // 2
        db.add(Track(id=f"au{i}", title=f"T{i}", artist=f"Band {i % 10}", album="X", duration=240, size=1000 + i,
                     path=f"Band/X/{i}.flac", embedding=rng.normal(size=8).tolist(), embedding_model=TEXT_MODEL))
        await db.flush()
        db.add(TrackAudio(track_id=f"au{i}", size=1000 + i,
                          effnet=_unit((calm_s if calm else loud_s) + 0.3 * rng.normal(size=EFFNET_DIM)),
                          effnet_model=EFFNET_MODEL, moods={"relaxed": 0.9 if calm else 0.1},
                          bpm=72.0 if calm else 150.0, danceability=0.2 if calm else 0.8,
                          clap=_unit((calm_c if calm else loud_c) + 0.3 * rng.normal(size=CLAP_DIM)),
                          clap_model=CLAP_MODEL))
    await db.commit()
    return calm_c, loud_c


async def _user(db, name):
    user = User(username=name, display_name=name, password_hash="x", navidrome_id=f"nd-{name}",
                navidrome_password=security.encrypt("pw"))
    db.add(user)
    await db.commit()
    return user


@pytest.mark.usefixtures("database", "audio_on")
async def test_mixes_follow_the_sound_when_the_tags_say_nothing(db, navidrome, monkeypatch):
    from kurai import llm
    from kurai.recommend import library, playlists

    await _library(db, np.random.default_rng(2))
    user = await _user(db, "mia")
    for i in range(0, 8):  # she plays only «calm» tracks
        db.add(Listen(user_id=user.id, track_id=f"au{i}", play_count=10, starred=True))
    await db.commit()
    taste = await library.build_taste(db, user)
    assert taste["by_sound"] == 8

    prompts = []

    async def fake_name(db_, task, system, prompt, schema, max_tokens=2000):
        prompts.append(prompt)
        return {"name": "Тихо", "description": "Спокойно."}

    monkeypatch.setattr(llm, "complete_json", fake_name)
    await settings.put(db, {"audio.mix_weight": 0.9})
    await db.commit()
    await settings.put(db, {"mix.playlists.size": 10})
    result = await playlists.nightly(db, user, count=1)
    assert result["playlists"] == ["Тихо"]
    playlist = (await db.execute(select(Playlist).where(Playlist.user_id == user.id))).scalars().one()
    calm = [int(t[2:]) < 20 for t in playlist.track_ids]
    assert sum(calm) >= 9, playlist.track_ids  # recent plays are left out, the rest is calm
    assert "спокойное" in prompts[0]  # the name is made knowing how the mix sounds


@pytest.mark.usefixtures("database", "audio_on")
async def test_ask_finds_tracks_by_a_description_of_their_sound(db, navidrome, monkeypatch):
    from kurai import llm
    from kurai.audio import client
    from kurai.recommend import playlists

    calm_c, loud_c = await _library(db, np.random.default_rng(3))
    user = await _user(db, "leo")

    async def plan(db_, task, system, prompt, schema, max_tokens=2000):
        return {"intent": "playlist", "answer": "", "mood_text": "громко", "sound_en": "loud distorted guitars",
                "seed_artists": [], "exclude_artists": [], "minutes": 30, "name": "Громко"}

    class RandomText:
        async def embed(self, texts):
            return [np.random.default_rng(9).normal(size=8).tolist()]

    async def embed_model(db_):
        return RandomText()

    asked = []

    async def clap_text(db_, text):
        asked.append(text)
        return np.asarray(_unit(loud_c), dtype=np.float32)

    monkeypatch.setattr(llm, "complete_json", plan)
    monkeypatch.setattr(llm, "embed_model", embed_model)
    monkeypatch.setattr(client, "clap_text", clap_text)
    await settings.put(db, {"audio.ask_weight": 0.9})
    await db.commit()
    result = await playlists.ask(db, user, "что-нибудь громкое")
    assert asked == ["loud distorted guitars"]
    playlist = await db.get(Playlist, result["playlist_id"])
    assert playlist.track_ids and all(int(t[2:]) >= 20 for t in playlist.track_ids)

    # the audio service down: the playlist is still made, from the other spaces
    async def down(db_, text):
        return None

    monkeypatch.setattr(client, "clap_text", down)
    assert "playlist_id" in await playlists.ask(db, user, "что-нибудь громкое")


@pytest.mark.usefixtures("database", "audio_on")
async def test_ask_keeps_to_the_tempo_asked_even_against_the_listeners_taste(db, navidrome, monkeypatch):
    """«Быстрое для тренировки» from someone who plays only calm music, where the description's search can't tell fast
    from slow: the tempo and energy the model names are checked against the analysis, and the taste weighs little."""
    from kurai import llm
    from kurai.audio import client
    from kurai.db.models import Listen
    from kurai.recommend import library, playlists

    await _library(db, np.random.default_rng(4))
    user = await _user(db, "fit")
    for i in range(0, 10):  # only calm tracks played
        db.add(Listen(user_id=user.id, track_id=f"au{i}", play_count=10, starred=True))
    await db.commit()
    await library.build_taste(db, user)

    async def plan(db_, task, system, prompt, schema, max_tokens=2000):
        assert "bpm_min" in prompt and "bpm_min" in schema["required"]
        return {"intent": "playlist", "answer": "", "mood_text": "быстрая музыка для тренировки",
                "sound_en": "fast energetic workout music", "seed_artists": [], "exclude_artists": [], "minutes": 30,
                "name": "Тренировка", "bpm_min": 120, "bpm_max": 0, "energy": "energetic", "vocals": "any"}

    class RandomText:
        async def embed(self, texts):
            return [np.random.default_rng(5).normal(size=8).tolist()]

    async def embed_model(db_):
        return RandomText()

    async def clap_text(db_, text):  # a description that says nothing about the speed
        return np.asarray(_unit(np.random.default_rng(6).normal(size=CLAP_DIM)), dtype=np.float32)

    monkeypatch.setattr(llm, "complete_json", plan)
    monkeypatch.setattr(llm, "embed_model", embed_model)
    monkeypatch.setattr(client, "clap_text", clap_text)
    result = await playlists.ask(db, user, "быстрая музыка для тренировки")
    playlist = await db.get(Playlist, result["playlist_id"])
    assert playlist.track_ids and all(int(t[2:]) >= 20 for t in playlist.track_ids), playlist.track_ids
    assert result["fitting"] == len(playlist.track_ids) and result["plan"]["bpm_min"] == 120


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    def analyze(self, path, effnet, clap):
        from kurai.audio.models import Result

        self.calls.append((Path(path).name, effnet, clap))
        if "broken" in Path(path).read_text():
            raise ValueError("не удалось декодировать")
        return Result(effnet=_unit(np.ones(EFFNET_DIM)), bpm=120.0, key="C major", danceability=0.7, voice=0.4,
                      moods={"happy": 0.8}, styles=[["Rock---Indie Rock", 0.5]], themes=[],
                      clap=_unit(np.ones(CLAP_DIM)) if clap else None)


@pytest.mark.usefixtures("database", "audio_on")
async def test_the_analysis_job_downloads_analyses_and_remembers(db, navidrome, monkeypatch, tmp_path):
    from kurai.audio import analyze

    for i in range(3):
        db.add(Track(id=f"au-job{i}", title=f"J{i}", artist="A", album="B", size=500, path=f"A/B/{i}.mp3"))
    db.add(Listen(user_id=(await _user(db, "zoe")).id, track_id="au-job2", play_count=1))
    await db.commit()

    async def download(account, song_id, dest):
        Path(dest).write_text("broken" if song_id == "au-job1" else "audio")

    navidrome.download = download
    fake = FakeAnalyzer()
    monkeypatch.setattr(analyze, "shared_analyzer", lambda: fake)

    async def no_download(db_, models_dir, clap):
        return 0

    monkeypatch.setattr(analyze, "ensure_models", no_download)
    await settings.put(db, {"audio.clap": False})
    await db.commit()
    job = Job(kind="analyze_audio", payload={"limit": 1000, "manual": True})
    result = await analyze.run(db, job)  # other tests' tracks are in the library too
    assert result["analyzed"] >= 2 and result["failed"] == 1 and result["left"] == 0
    assert fake.calls[0] == ("track.mp3", True, False)  # the listened track first, in its own format
    row = await db.get(TrackAudio, "au-job0")
    assert row.key == "C major" and row.effnet_model == EFFNET_MODEL and row.clap is None
    assert "декодировать" in (await db.get(TrackAudio, "au-job1")).error

    # CLAP turned on: only CLAP runs; the broken file waits for a retry
    await settings.put(db, {"audio.clap": True})
    await db.commit()
    fake.calls.clear()
    result = await analyze.run(db, job)
    assert result["analyzed"] >= 2 and result["left"] == 0 and {c[1:] for c in fake.calls} == {(False, True)}
    progress = await analyze.progress(db)
    assert progress["failed"] == 1 and progress["failed_rows"][0][0] == "au-job1"
    assert await analyze.retry_failed(db) == 1

    # a daytime run leaves a big backlog to the night
    monkeypatch.setattr(analyze, "DAYTIME_BACKLOG", 0)
    assert "skipped" in await analyze.run(db, Job(kind="analyze_audio", payload={"limit": 10}))


@pytest.mark.usefixtures("database", "audio_on")
async def test_the_worker_schedules_the_analysis_day_and_night(db):
    from datetime import datetime

    from kurai.jobs import worker

    await worker.schedule(datetime(2026, 11, 15, 8, 0).astimezone())  # inside the night window
    night = (await db.execute(select(Job).where(Job.kind == "analyze_audio"))).scalars().one()
    assert night.night_only and "until" in night.payload
    nightly = (await db.execute(select(Job).where(Job.dedupe_key.like("nightly:%")))).scalars().one()
    for job in (night, nightly):
        job.status = "done"
    await db.commit()
    await worker.schedule(datetime(2026, 11, 15, 8, 30).astimezone())  # the same night, after both finished
    for key in ("analyze_audio:%", "nightly:%"):
        assert len((await db.execute(select(Job).where(Job.dedupe_key.like(key)))).scalars().all()) == 1  # not again
    await settings.put(db, {"audio.enabled": False})
    await db.commit()
    await db.execute(delete(Job).where(Job.kind == "analyze_audio"))
    await db.commit()
    await worker.schedule(datetime(2026, 11, 15, 13, 0).astimezone())
    assert not (await db.execute(select(Job).where(Job.kind == "analyze_audio"))).scalars().all()


# ---------- the real models (optional) ----------


@pytest.mark.skipif(not os.environ.get("KURAI_AUDIO_MODELS"), reason="KURAI_AUDIO_MODELS not set")
def test_real_models_on_a_generated_song(tmp_path):
    es = pytest.importorskip("essentia.standard")
    from kurai.audio.models import Analyzer

    rate = 44100
    t = np.arange(rate * 60) / rate
    beat = (np.mod(t, 0.5) < 0.05) * np.sin(2 * np.pi * 60 * t) * np.exp(-np.mod(t, 0.5) * 30)
    chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 261.6, 329.6)) * 0.1
    song = (0.6 * beat + chord).astype(np.float32)
    path = str(tmp_path / "song.flac")
    es.AudioWriter(filename=path, format="flac", sampleRate=rate)(np.stack([song, song], axis=1))
    analyzer = Analyzer(Path(os.environ["KURAI_AUDIO_MODELS"]), 2)
    result = analyzer.analyze(path, effnet=True, clap=True)
    assert len(result.effnet) == EFFNET_DIM and len(result.clap) == CLAP_DIM
    assert 115 <= result.bpm <= 125 and result.key == "A minor"  # 120 BPM, an A minor chord
    beat_text, violin = analyzer.clap_text(["electronic music with a steady beat", "solo violin, sad and slow"])
    assert np.dot(result.clap, beat_text) > np.dot(result.clap, violin)
