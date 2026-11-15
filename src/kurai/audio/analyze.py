"""The analysis job («analyze_audio», taken only by the audio container's lane): the tracks not heard yet, the ones
someone listened to first, are fetched from Navidrome one at a time and analysed, until the night window ends (the
night's run) or the limit is reached (the daytime runs, for new imports). Also: the models' download, and the progress
the «Звук» tab shows."""

import asyncio
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kurai import outbound, settings
from kurai.audio import CLAP_MODEL, EFFNET_MODEL
from kurai.config import get_config
from kurai.db.models import Job, Listen, Track, TrackAudio
from kurai.i18n import _
from kurai.integrations.navidrome import NavidromeError

log = logging.getLogger(__name__)

DAYTIME_LIMIT = 50  # tracks per daytime run
DAYTIME_BACKLOG = 200  # more waiting than this: the daytime runs leave it to the night
BATCH = 10
DOWNLOAD_FAILURES = 3  # in a row: Navidrome itself is in trouble, the run stops

_analyzer = None
_downloading = asyncio.Lock()


def shared_analyzer():
    """The process's one analyzer (its models stay loaded between runs and serve the text requests too)."""
    global _analyzer
    if _analyzer is None:
        from kurai.audio.models import Analyzer

        c = get_config()
        _analyzer = Analyzer(Path(c.audio_models_dir), c.audio_threads)
    return _analyzer


def needs(existing: TrackAudio | None, size: int, clap: bool) -> tuple[bool, bool]:
    """What a track still needs: (the Essentia part, the CLAP part)."""
    if existing is None or existing.size != size:
        return True, clap
    if existing.error:
        return False, False  # until the file changes or an admin retries
    return existing.effnet_model != EFFNET_MODEL, clap and existing.clap_model != CLAP_MODEL


def _pending(clap: bool):
    stale = or_(TrackAudio.track_id.is_(None), TrackAudio.size != Track.size)
    missing = [TrackAudio.effnet_model.is_distinct_from(EFFNET_MODEL)]
    if clap:
        missing.append(TrackAudio.clap_model.is_distinct_from(CLAP_MODEL))
    return or_(stale, and_(TrackAudio.error.is_(None), or_(*missing)))


def _with_audio():
    return select().select_from(Track).outerjoin(TrackAudio, TrackAudio.track_id == Track.id)


async def pending_count(db: AsyncSession, clap: bool) -> int:
    return (await db.execute(_with_audio().add_columns(func.count()).where(_pending(clap)))).scalar_one()


async def progress(db: AsyncSession) -> dict:
    """For the «Звук» tab: how much of the library is analysed, what failed, how long a track takes."""
    clap = bool(await settings.get(db, "audio.clap"))
    total = (await db.execute(select(func.count()).select_from(Track))).scalar_one()
    ok = TrackAudio.error.is_(None)
    sound = (await db.execute(select(func.count()).select_from(TrackAudio)
                              .where(ok, TrackAudio.effnet_model == EFFNET_MODEL))).scalar_one()
    described = (await db.execute(select(func.count()).select_from(TrackAudio)
                                  .where(ok, TrackAudio.clap_model == CLAP_MODEL))).scalar_one()
    failed = list((await db.execute(select(TrackAudio.track_id, Track.artist, Track.title, TrackAudio.error)
                                    .join(Track, Track.id == TrackAudio.track_id)
                                    .where(TrackAudio.error.is_not(None)).order_by(Track.artist).limit(50))).all())
    failed_count = (await db.execute(select(func.count()).select_from(TrackAudio)
                                     .where(TrackAudio.error.is_not(None)))).scalar_one()
    recent = select(TrackAudio.seconds).where(ok, TrackAudio.seconds.is_not(None)) \
        .order_by(TrackAudio.analyzed_at.desc()).limit(200).subquery()
    per_track = (await db.execute(select(func.avg(recent.c.seconds)))).scalar_one()
    last = (await db.execute(select(Job).where(Job.kind == "analyze_audio", Job.status.in_(["done", "failed"]))
                             .order_by(Job.finished_at.desc()).limit(1))).scalar_one_or_none()
    return {"total": total, "sound": sound, "clap": described, "failed": failed_count, "failed_rows": failed,
            "pending": await pending_count(db, clap), "per_track": round(per_track, 1) if per_track else None,
            "last_job": last}


async def retry_failed(db: AsyncSession) -> int:
    count = (await db.execute(delete(TrackAudio).where(TrackAudio.error.is_not(None)))).rowcount
    await db.commit()
    return count


async def ensure_models(db: AsyncSession, models_dir: Path, clap: bool, essentia: bool = True) -> int:
    """Downloads the model files that aren't there yet (through the gateway when «Загрузка моделей» is ticked);
    returns how many were fetched. One download at a time: the analysis and a description's request may both ask."""
    from kurai.audio.models import model_files

    async with _downloading:
        return await _download(db, [(url, path) for url, path in model_files(models_dir, clap, essentia)
                                    if not path.exists()])


async def _download(db: AsyncSession, missing: list[tuple[str, Path]]) -> int:
    if not missing:
        return 0
    async with await outbound.session_for(db, "models", 3600) as s:
        for url, path in missing:
            log.info("downloading %s", url)
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            async with s.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(_("модель не скачалась: {url}: HTTP {status}").format(url=url,
                                                                                             status=resp.status))
                with open(part, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
            part.rename(path)
    return len(missing)


def _store(row: TrackAudio, result, effnet: bool, clap: bool, size: int, seconds: float) -> None:
    if row.size != size:  # another file: nothing of the old one stays
        row.clap, row.clap_model = None, None
    if effnet:
        row.effnet, row.effnet_model = result.effnet, EFFNET_MODEL
        row.bpm, row.key, row.danceability, row.voice = result.bpm, result.key, result.danceability, result.voice
        row.moods, row.styles, row.themes = result.moods, result.styles, result.themes
    if clap:
        row.clap, row.clap_model = result.clap, CLAP_MODEL
    row.size, row.error, row.seconds, row.analyzed_at = size, None, round(seconds, 1), datetime.now(UTC)


async def run(db: AsyncSession, job: Job) -> dict:
    from kurai.recommend.library import admin_account, navidrome

    values = await settings.get_all(db)
    if not values["audio.enabled"]:
        return {"skipped": _("анализ звука выключен")}
    clap = bool(values["audio.clap"])
    limit = job.payload.get("limit")
    until = datetime.fromisoformat(job.payload["until"]) if job.payload.get("until") else None
    if limit and not job.payload.get("manual") and await pending_count(db, clap) > DAYTIME_BACKLOG:
        return {"skipped": _("много неразобранных треков: они проанализируются ночью")}
    fetched = await ensure_models(db, Path(get_config().audio_models_dir), clap)
    analyzer = shared_analyzer()
    nd, account = navidrome(), await admin_account(db)
    listened = exists(select(Listen.id).where(Listen.track_id == Track.id))
    done = failed = download_failures = 0
    spent = 0.0

    def over() -> bool:
        return bool((limit and done + failed >= limit) or (until and datetime.now(until.tzinfo) >= until))

    while not over():
        rows = (await db.execute(_with_audio().add_columns(Track, TrackAudio).where(_pending(clap))
                                 .order_by(listened.desc(), Track.id).limit(BATCH))).all()
        if not rows:
            break
        for track, existing in rows:
            if over():
                break
            effnet, clap_part = needs(existing, track.size, clap)
            row = existing or TrackAudio(track_id=track.id, size=track.size)
            if existing is None:
                db.add(row)
            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "track" + Path(track.path).suffix.lower()[:8])
                    await nd.download(account, track.id, path)
                    download_failures = 0
                    result = await asyncio.to_thread(analyzer.analyze, path, effnet, clap_part)
                seconds = time.monotonic() - started
                _store(row, result, effnet, clap_part, track.size, seconds)
                done += 1
                spent += seconds
            except NavidromeError as e:
                download_failures += 1
                if download_failures >= DOWNLOAD_FAILURES:
                    await db.rollback()
                    raise
                row.size, row.error = track.size, _("не скачался из Navidrome: {e}").format(e=e)[:500]
                failed += 1
            except Exception as e:  # an unreadable or odd file: noted, skipped until it changes
                log.warning("audio analysis of %s failed: %s", track.id, e)
                row.size, row.error, row.seconds = track.size, (str(e) or e.__class__.__name__)[:500], None
                failed += 1
            await db.commit()
    return {"analyzed": done, "failed": failed, "left": await pending_count(db, clap), "models_downloaded": fetched,
            "seconds_per_track": round(spent / done, 1) if done else None}
