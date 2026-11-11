"""CLI entrypoint for PostgreSQL background worker (`python -m mindweave.workers`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update

from mindweave.core.config import get_settings
from mindweave.core.dependencies import AppContainer
from mindweave.core.logging import configure_logging, get_logger
from mindweave.core.orm import (
    FactRow,
    IdempotencyKeyRow,
    InboxItemRow,
    PersonRow,
    RecordRow,
    UserRow,
)
from mindweave.modules.capture.application.service import handle_process_capture_job
from mindweave.modules.companion.application.writeback import register_companion_worker_handlers
from mindweave.modules.data_rights.application.service import build_export_archive
from mindweave.modules.reminders.application.service import scan_and_deliver_due_reminders
from mindweave.shared.enums import (
    FactStatus,
    InboxItemKind,
    InboxItemStatus,
    RecordReviewState,
)
from mindweave.shared.scopes import UserScope
from mindweave.wiring import wire_container_services
from mindweave.workers.ports import (
    TASK_BUILD_EXPORT_ARCHIVE,
    TASK_EXPIRE_VENTING_INBOX,
    TASK_PROCESS_CAPTURE,
    TASK_PURGE_SOFT_DELETED,
    TASK_SCAN_DUE_REMINDERS,
)
from mindweave.workers.queue import PostgresJobQueue
from mindweave.workers.runner import ClaimedJob, WorkerRunner
from mindweave.workers.scheduler import PeriodicScheduler

logger = get_logger(__name__)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _default_expire_venting_inbox(container: AppContainer) -> int:
    """Default periodic handler downgrading expired 72h venting-hold items to `note_only`."""
    now = _ensure_utc(container.clock.now())
    async with container.db.system_session() as sys_sess:
        due_items = (
            await sys_sess.execute(
                select(InboxItemRow.id, InboxItemRow.user_id)
                .where(
                    InboxItemRow.status == InboxItemStatus.OPEN.value,
                    InboxItemRow.kind == InboxItemKind.VENTING_HOLD.value,
                    InboxItemRow.expires_at.is_not(None),
                    InboxItemRow.expires_at <= now,
                )
                .limit(100)
            )
        ).all()

    expired_count = 0
    for item_id, user_id in due_items:
        scope = UserScope(user_id=user_id)
        async with container.db.user_session(scope) as session:
            item = await session.scalar(
                select(InboxItemRow)
                .where(
                    InboxItemRow.id == item_id,
                    InboxItemRow.user_id == scope.user_id,
                    InboxItemRow.status == InboxItemStatus.OPEN.value,
                )
                .with_for_update(skip_locked=True)
            )
            if item is None:
                continue
            item.status = InboxItemStatus.EXPIRED.value
            item.resolution_action = "keep_as_note"
            item.resolved_at = now
            if item.record_id is not None:
                await session.execute(
                    update(RecordRow)
                    .where(
                        RecordRow.id == item.record_id,
                        RecordRow.user_id == scope.user_id,
                    )
                    .values(review_state=RecordReviewState.NOTE_ONLY.value, updated_at=now)
                )
            if item.fact_ids:
                await session.execute(
                    update(FactRow)
                    .where(
                        FactRow.id.in_(item.fact_ids),
                        FactRow.user_id == scope.user_id,
                    )
                    .values(status=FactStatus.REJECTED.value, updated_at=now)
                )
            expired_count += 1
    return expired_count


async def _default_purge_soft_deleted(container: AppContainer) -> None:
    """Default periodic maintenance handler purging expired idempotency keys and soft-deleted rows."""
    now = _ensure_utc(container.clock.now())
    cutoff = now - timedelta(days=container.settings.soft_delete_retention_days)
    async with container.db.system_session() as sys_sess:
        await sys_sess.execute(delete(IdempotencyKeyRow).where(IdempotencyKeyRow.expires_at <= now))
        await sys_sess.execute(
            delete(RecordRow).where(
                RecordRow.deleted_at.is_not(None), RecordRow.deleted_at <= cutoff
            )
        )
        await sys_sess.execute(
            delete(PersonRow).where(
                PersonRow.deleted_at.is_not(None), PersonRow.deleted_at <= cutoff
            )
        )
        await sys_sess.execute(
            delete(UserRow).where(UserRow.deleted_at.is_not(None), UserRow.deleted_at <= cutoff)
        )


def build_default_worker_runner(
    container: AppContainer,
    *,
    batch_size: int = 10,
    lease_seconds: int = 120,
    enable_scheduler: bool = True,
) -> WorkerRunner:
    """Create a `WorkerRunner` wired with all domain task handlers and `PeriodicScheduler`."""
    wire_container_services(container)
    queue = container.extras["job_queue"]
    scheduler = (
        PeriodicScheduler(container.db, queue, clock=container.clock) if enable_scheduler else None
    )
    runner = WorkerRunner(
        container.db,
        clock=container.clock,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
        scheduler=scheduler,
    )

    async def _handle_scan_due(job: ClaimedJob) -> None:
        batch_limit = int(job.payload.get("batch_limit", 100))
        await scan_and_deliver_due_reminders(container, batch_limit=batch_limit)

    async def _handle_expire_venting(_job: ClaimedJob) -> None:
        await _default_expire_venting_inbox(container)

    async def _handle_purge_deleted(_job: ClaimedJob) -> None:
        await _default_purge_soft_deleted(container)

    async def _handle_process_capture(job: ClaimedJob) -> None:
        await handle_process_capture_job(container, job)

    async def _handle_build_export(job: ClaimedJob) -> None:
        await build_export_archive(container, job=job)

    runner.register_handler(TASK_SCAN_DUE_REMINDERS, _handle_scan_due)
    runner.register_handler(TASK_EXPIRE_VENTING_INBOX, _handle_expire_venting)
    runner.register_handler(TASK_PURGE_SOFT_DELETED, _handle_purge_deleted)
    runner.register_handler(TASK_PROCESS_CAPTURE, _handle_process_capture)
    runner.register_handler(TASK_BUILD_EXPORT_ARCHIVE, _handle_build_export)
    register_companion_worker_handlers(runner, container)
    return runner


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mindweave.workers",
        description="MindWeave PostgreSQL background job worker & periodic scheduler",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Tick periodic scheduler once, drain due jobs, and exit",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Idle poll interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Max background jobs claimed per poll batch (default: 10)",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=120,
        help="Row lease timeout in seconds for claimed jobs (default: 120)",
    )
    return parser.parse_args(argv)


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(
        json_logs=settings.environment != "dev",
        level=logging.DEBUG if settings.debug else logging.INFO,
    )
    container = AppContainer.build(settings)
    try:
        runner = build_default_worker_runner(
            container,
            batch_size=args.batch_size,
            lease_seconds=args.lease_seconds,
            enable_scheduler=True,
        )
        if args.once:
            scheduler = PeriodicScheduler(
                container.db,
                PostgresJobQueue(clock=container.clock),
                clock=container.clock,
            )
            await scheduler.tick()
            processed = await runner.drain()
            logger.info("worker.once_completed", processed_jobs=processed)
            return 0

        await runner.run_forever(poll_interval=args.poll_interval)
        return 0
    finally:
        await container.db.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(asyncio.run(async_main(argv)))


if __name__ == "__main__":
    main()
