"""Deterministic time-bucket periodic scheduler preventing duplicate cron jobs across workers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock, SystemClock
from mindweave.core.db import DatabaseManager
from mindweave.workers.ports import (
    TASK_EXPIRE_VENTING_INBOX,
    TASK_PURGE_SOFT_DELETED,
    TASK_SCAN_DUE_REMINDERS,
    JobQueuePort,
)
from mindweave.workers.queue import PostgresJobQueue


@dataclass(frozen=True, slots=True)
class PeriodicTaskSpec:
    """Configuration for a recurring maintenance task enqueued on deterministic time buckets."""

    task_name: str
    interval_seconds: int
    payload: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be >= 1")
        if not self.task_name.strip():
            raise ValueError("task_name must not be empty")


DEFAULT_PERIODIC_TASKS: tuple[PeriodicTaskSpec, ...] = (
    PeriodicTaskSpec(task_name=TASK_SCAN_DUE_REMINDERS, interval_seconds=60),
    PeriodicTaskSpec(task_name=TASK_EXPIRE_VENTING_INBOX, interval_seconds=15 * 60),
    PeriodicTaskSpec(task_name=TASK_PURGE_SOFT_DELETED, interval_seconds=24 * 60 * 60),
)


def build_cron_idempotency_key(task_name: str, now: datetime, interval_seconds: int) -> str:
    """Compute deterministic bucketed idempotency key (e.g. `cron:reminders.scan_due:29905122`)."""
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be >= 1")
    utc_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    bucket = int(utc_now.timestamp()) // interval_seconds
    return f"cron:{task_name}:{bucket}"


class PeriodicScheduler:
    """Enqueues due periodic maintenance tasks using bucketed `idempotency_key` deduplication."""

    def __init__(
        self,
        db: DatabaseManager,
        queue: JobQueuePort | None = None,
        *,
        clock: Clock | None = None,
        tasks: Sequence[PeriodicTaskSpec] = DEFAULT_PERIODIC_TASKS,
    ) -> None:
        self._db = db
        self._clock: Clock = clock or SystemClock()
        self._queue: JobQueuePort = queue or PostgresJobQueue(clock=self._clock)
        self._tasks: tuple[PeriodicTaskSpec, ...] = tuple(tasks)

    @property
    def tasks(self) -> tuple[PeriodicTaskSpec, ...]:
        return self._tasks

    async def tick(self, session: AsyncSession | None = None) -> list[UUID]:
        """Enqueue all periodic tasks for the current time bucket; deduplicates across workers."""
        if session is not None:
            return await self._enqueue_all(session)
        async with self._db.system_session() as sys_sess:
            return await self._enqueue_all(sys_sess)

    async def _enqueue_all(self, session: AsyncSession) -> list[UUID]:
        now = self._clock.now()
        utc_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        job_ids: list[UUID] = []
        for spec in self._tasks:
            idem_key = build_cron_idempotency_key(
                spec.task_name,
                utc_now,
                spec.interval_seconds,
            )
            job_id = await self._queue.enqueue(
                session,
                task_name=spec.task_name,
                payload=dict(spec.payload),
                run_at=utc_now,
                idempotency_key=idem_key,
                max_attempts=spec.max_attempts,
            )
            job_ids.append(job_id)
        return job_ids
