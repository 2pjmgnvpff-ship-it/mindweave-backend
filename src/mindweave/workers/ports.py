"""Port protocol for PostgreSQL-backed background job queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class JobQueuePort(Protocol):
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        task_name: str,
        payload: dict[str, Any],
        user_id: UUID | None = None,
        run_at: datetime | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
    ) -> UUID: ...


# Canonical task names shared across modules and worker handlers
TASK_PROCESS_CAPTURE = "capture.process"
TASK_WRITEBACK_CHAT_FACTS = "companion.writeback_facts"
TASK_SUMMARIZE_THREAD = "companion.summarize_thread"
TASK_BUILD_EXPORT_ARCHIVE = "data_rights.build_export"
TASK_SCAN_DUE_REMINDERS = "reminders.scan_due"
TASK_EXPIRE_VENTING_INBOX = "memory.expire_venting_inbox"
TASK_AUTO_INK_PENCIL_DRAFTS = "memory.auto_ink_pencil_drafts"
TASK_PURGE_SOFT_DELETED = "maintenance.purge_soft_deleted"
