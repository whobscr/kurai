"""The jobs table as a queue: enqueue (deduplicated by key), claim (FOR UPDATE SKIP LOCKED), finish."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kurai.db.models import Job

# lanes: «general» runs one job at a time (syncs, the night's work, plugins' jobs); «interactive» runs what a person
# waits for (a request to the model, an explanation, «Моя волна»), several at once up to «Параллельные запросы к
# модели»; «audio» is the audio container's (kurai.audio), so the main worker never takes its jobs
INTERACTIVE = {"ask", "explain", "wave"}
AUDIO = {"analyze_audio"}
LANES = {"interactive": INTERACTIVE, "audio": AUDIO}
KEEP_DAYS = 30  # finished jobs older than this are removed at night


def _in_lane(lane: str):
    if lane == "general":
        return Job.kind.not_in(INTERACTIVE | AUDIO)
    return Job.kind.in_(LANES[lane])


async def enqueue(db: AsyncSession, kind: str, payload: dict[str, Any] | None = None, *, user_id: int | None = None,
                  priority: int = 50, night_only: bool = False, dedupe_key: str | None = None,
                  once: bool = False) -> Job:
    """A new job, or the unfinished one with the same dedupe key; with `once`, any job ever made with that key (e.g.
    one night's run: it must not start again after it has finished)."""
    if dedupe_key:
        same = select(Job).where(Job.dedupe_key == dedupe_key)
        if not once:
            same = same.where(Job.status.in_(["queued", "running"]))
        existing = (await db.execute(same.order_by(Job.id.desc()))).scalars().first()
        if existing:
            return existing
    job = Job(kind=kind, payload=payload or {}, user_id=user_id, priority=priority, night_only=night_only,
              dedupe_key=dedupe_key)
    db.add(job)
    await db.commit()
    return job


async def claim(db: AsyncSession, lane: str, in_night: bool, kinds: Collection[str] | None = None) -> Job | None:
    """The next queued job of the lane; only of these kinds when given (a switched-off plugin's jobs wait)."""
    stmt = (select(Job).where(Job.status == "queued", _in_lane(lane)).order_by(Job.priority, Job.id)
            .limit(1).with_for_update(skip_locked=True))
    if kinds is not None:
        stmt = stmt.where(Job.kind.in_(list(kinds)))
    if not in_night:
        stmt = stmt.where(Job.night_only.is_(False))
    job = (await db.execute(stmt)).scalars().first()
    if job is None:
        await db.rollback()
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    await db.commit()
    return job


async def finish(db: AsyncSession, job: Job, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    job.status = "failed" if error else "done"
    job.result = result
    job.error = error
    job.finished_at = datetime.now(UTC)
    await db.commit()


async def requeue_interrupted(db: AsyncSession, lanes: tuple[str, ...]) -> None:
    """Jobs a stopped process left running in its lanes start again (another process's running jobs are left alone)."""
    await db.execute(update(Job).where(Job.status == "running", or_(*(_in_lane(lane) for lane in lanes)))
                     .values(status="queued", started_at=None))
    await db.commit()


async def prune(db: AsyncSession, days: int = KEEP_DAYS) -> int:
    """Removes the finished jobs older than `days` (the night's work does it); the once-only ones are keyed by date,
    so removing them can't start them again."""
    before = datetime.now(UTC) - timedelta(days=days)
    removed = (await db.execute(delete(Job).where(Job.status.in_(["done", "failed"]),
                                                  Job.finished_at < before))).rowcount
    await db.commit()
    return removed


async def last_done(db: AsyncSession, kind: str) -> datetime | None:
    return (await db.execute(select(Job.finished_at).where(Job.kind == kind, Job.status == "done")
                             .order_by(Job.finished_at.desc()).limit(1))).scalar_one_or_none()
