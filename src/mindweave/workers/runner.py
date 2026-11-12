"""PostgreSQL `FOR UPDATE SKIP LOCKED` background worker runner with exponential backoff & DLQ."""

from __future__ import annotations

import asyncio
import inspect
import re
import signal
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update

from mindweave.core.clock import Clock, SystemClock
from mindweave.core.db import DatabaseManager
from mindweave.core.logging import get_logger, sanitize_log_value
from mindweave.core.orm import BackgroundJobRow
from mindweave.shared.enums import JobStatus
from mindweave.shared.pii import mask_hard_pii
from mindweave.workers.scheduler import PeriodicScheduler

logger = get_logger(__name__)

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_KV_SECRET_RE = re.compile(
    r"((?:token|secret|password|raw_text|transcript|content|body|display_name|person_name|alias)"
    r"\s*[=:]\s*)([^\s,;]+)",
    re.IGNORECASE,
)
_MAX_ERROR_CHARS = 240


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Immutable snapshot of a claimed `background_jobs` row passed to registered handlers."""

    id: UUID
    user_id: UUID | None
    task_name: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    run_at: datetime
    idempotency_key: str | None = None

    @property
    def payload_json(self) -> dict[str, Any]:
        return self.payload

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __contains__(self, key: object) -> bool:
        return key in self.payload


JobHandler = Callable[[ClaimedJob], Awaitable[Any]]
ZeroArgJobHandler = Callable[[], Awaitable[Any]]


def compute_backoff_seconds(attempts: int) -> int:
    """Compute bounded exponential retry delay: `min(3600, 2 ** attempts * 5)` seconds."""
    exponent = max(1, int(attempts))
    return int(min(3600, (2**exponent) * 5))


def sanitize_job_error(exc: BaseException, *, max_length: int = _MAX_ERROR_CHARS) -> str:
    """Produce a truncated, single-line, zero-PII diagnostic summary for `last_error`."""
    raw_msg = " ".join(str(exc).split())
    masked = str(sanitize_log_value(raw_msg))
    masked = _PHONE_RE.sub("[REDACTED_PHONE]", masked)
    masked = _KV_SECRET_RE.sub(r"\1[REDACTED]", masked)
    masked = mask_hard_pii(masked).masked_text
    prefix = type(exc).__name__
    combined = f"{prefix}: {masked}" if masked else prefix
    if len(combined) > max_length:
        return combined[: max_length - 3] + "..."
    return combined


def _accepts_zero_positional_args(fn: Callable[..., Any]) -> bool:
    sig = inspect.signature(fn)
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return False
    return True


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class WorkerRunner:
    """Polls `background_jobs` via `FOR UPDATE SKIP LOCKED`, manages leases, retries, and DLQ."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        clock: Clock | None = None,
        batch_size: int = 10,
        lease_seconds: int = 120,
        scheduler: PeriodicScheduler | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be >= 1")
        self._db = db
        self._clock: Clock = clock or SystemClock()
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._scheduler = scheduler
        self._handlers: dict[str, tuple[Callable[..., Awaitable[Any]], bool]] = {}
        self._stop_event: asyncio.Event | None = None

    def register_handler(
        self,
        task_name: str,
        handler_fn: JobHandler | ZeroArgJobHandler,
    ) -> None:
        cleaned = task_name.strip()
        if not cleaned:
            raise ValueError("task_name must not be empty")
        zero_arg = _accepts_zero_positional_args(handler_fn)
        self._handlers[cleaned] = (handler_fn, zero_arg)

    def request_stop(self) -> None:
        """Signal `run_forever` to finish the active batch and exit cleanly."""
        if self._stop_event is not None:
            self._stop_event.set()

    async def claim_due_batch(self) -> list[ClaimedJob]:
        """Atomically claim up to `batch_size` queued or expired-lease jobs."""
        now = _ensure_utc(self._clock.now())
        lease_until = now + timedelta(seconds=self._lease_seconds)
        claimed: list[ClaimedJob] = []

        async with self._db.system_session() as session:
            stmt = (
                select(BackgroundJobRow)
                .where(
                    or_(
                        and_(
                            BackgroundJobRow.status == JobStatus.QUEUED.value,
                            BackgroundJobRow.run_at <= now,
                        ),
                        and_(
                            BackgroundJobRow.status == JobStatus.RUNNING.value,
                            BackgroundJobRow.locked_until.is_not(None),
                            BackgroundJobRow.locked_until <= now,
                        ),
                    )
                )
                .order_by(BackgroundJobRow.run_at.asc(), BackgroundJobRow.created_at.asc())
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.scalars(stmt)).all()
            for row in rows:
                if row.attempts >= row.max_attempts:
                    row.status = JobStatus.DEAD.value
                    row.locked_until = None
                    row.last_error = row.last_error or "Lease expired after max_attempts"
                    row.updated_at = now
                    continue

                row.status = JobStatus.RUNNING.value
                row.attempts = row.attempts + 1
                row.locked_until = lease_until
                row.updated_at = now
                claimed.append(
                    ClaimedJob(
                        id=row.id,
                        user_id=row.user_id,
                        task_name=row.task_name,
                        payload=dict(row.payload_json or {}),
                        attempts=row.attempts,
                        max_attempts=row.max_attempts,
                        run_at=row.run_at,
                        idempotency_key=row.idempotency_key,
                    )
                )
        return claimed

    async def run_once(self) -> int:
        """Claim and execute at most one batch of due jobs. Returns number of jobs processed."""
        claimed = await self.claim_due_batch()
        for job in claimed:
            await self._execute_claimed_job(job)
        return len(claimed)

    async def drain(self, max_iterations: int = 100) -> int:
        """Deterministically execute due jobs until queue is empty or `max_iterations` reached."""
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        total_processed = 0
        for _ in range(max_iterations):
            batch_count = await self.run_once()
            if batch_count == 0:
                break
            total_processed += batch_count
        return total_processed

    async def run_forever(
        self,
        poll_interval: float = 1.0,
        *,
        stop_event: asyncio.Event | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        """Continuously poll & execute jobs until SIGINT/SIGTERM or `request_stop()`."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")

        event = stop_event or asyncio.Event()
        self._stop_event = event
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []

        if install_signal_handlers and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, event.set)
                    installed_signals.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass

        logger.info(
            "worker.started",
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            poll_interval=poll_interval,
        )
        try:
            while not event.is_set():
                if self._scheduler is not None:
                    await self._scheduler.tick()

                processed = await self.run_once()
                if event.is_set():
                    break
                if processed > 0:
                    await asyncio.sleep(0)
                    continue

                try:
                    await asyncio.wait_for(event.wait(), timeout=poll_interval)
                except TimeoutError:
                    continue
        finally:
            for sig in installed_signals:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            logger.info("worker.stopped")

    async def _execute_claimed_job(self, job: ClaimedJob) -> None:
        entry = self._handlers.get(job.task_name)
        try:
            if entry is None:
                raise KeyError(f"Unregistered background task {job.task_name!r}")
            handler_fn, zero_arg = entry
            if zero_arg:
                await handler_fn()
            else:
                await handler_fn(job)
        except Exception as exc:
            await self._mark_failed(job, exc)
            return

        await self._mark_succeeded(job)

    async def _mark_succeeded(self, job: ClaimedJob) -> None:
        finished_at = _ensure_utc(self._clock.now())
        async with self._db.system_session() as session:
            await session.execute(
                update(BackgroundJobRow)
                .where(BackgroundJobRow.id == job.id)
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    locked_until=None,
                    last_error=None,
                    updated_at=finished_at,
                )
            )
        logger.info(
            "worker.job.succeeded",
            job_id=str(job.id),
            task_name=job.task_name,
            attempts=job.attempts,
        )

    async def _mark_failed(self, job: ClaimedJob, exc: BaseException) -> None:
        failed_at = _ensure_utc(self._clock.now())
        sanitized_err = sanitize_job_error(exc)

        if job.attempts >= job.max_attempts:
            async with self._db.system_session() as session:
                await session.execute(
                    update(BackgroundJobRow)
                    .where(BackgroundJobRow.id == job.id)
                    .values(
                        status=JobStatus.DEAD.value,
                        locked_until=None,
                        last_error=sanitized_err,
                        updated_at=failed_at,
                    )
                )
            logger.error(
                "worker.job.dead_lettered",
                job_id=str(job.id),
                task_name=job.task_name,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                error=sanitized_err,
            )
            return

        backoff_sec = compute_backoff_seconds(job.attempts)
        next_run = failed_at + timedelta(seconds=backoff_sec)
        async with self._db.system_session() as session:
            await session.execute(
                update(BackgroundJobRow)
                .where(BackgroundJobRow.id == job.id)
                .values(
                    status=JobStatus.QUEUED.value,
                    run_at=next_run,
                    locked_until=None,
                    last_error=sanitized_err,
                    updated_at=failed_at,
                )
            )
        logger.warning(
            "worker.job.retry_scheduled",
            job_id=str(job.id),
            task_name=job.task_name,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            backoff_seconds=backoff_sec,
            error=sanitized_err,
        )
