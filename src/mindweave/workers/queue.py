"""PostgreSQL-backed background job queue implementing `JobQueuePort` (Invariants I10, I11)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock, SystemClock
from mindweave.core.ids import new_id
from mindweave.core.logging import SENSITIVE_FIELD_NAMES, sanitize_log_value
from mindweave.core.orm import BackgroundJobRow
from mindweave.shared.enums import JobStatus
from mindweave.shared.pii import mask_hard_pii

if TYPE_CHECKING:
    from mindweave.workers.ports import JobQueuePort

_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_EXTRA_FORBIDDEN_JOB_KEYS: frozenset[str] = frozenset(
    {
        "title",
        "note",
        "text",
        "name",
        "push_token",
        "device_label",
        "prompt",
        "user_query",
        "ocr_text",
        "summary",
        "rolling_summary",
        "audio_bytes",
        "image_bytes",
    }
)
_MAX_STRING_VALUE_LEN = 256


class JobPayloadPrivacyError(ValueError):
    """Raised when a background job payload violates Invariant I10 (UUIDs/scalars only, zero PII)."""


def _normalize_payload_key(key: str) -> str:
    return _CAMEL_BOUNDARY_RE.sub(r"\1_\2", key).lower().strip()


def sanitize_and_validate_job_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that a job payload carries opaque identifiers/scalars only and zero PII."""
    if not isinstance(payload, Mapping):
        raise JobPayloadPrivacyError("Background job payload must be a mapping")
    return _validate_mapping(payload)


def _validate_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for raw_key, raw_val in mapping.items():
        key = str(raw_key)
        norm_key = _normalize_payload_key(key)
        if norm_key in SENSITIVE_FIELD_NAMES or norm_key in _EXTRA_FORBIDDEN_JOB_KEYS:
            raise JobPayloadPrivacyError(
                f"Background job payload forbids sensitive field {key!r} (Invariant I10)"
            )
        cleaned[key] = _validate_value(raw_val, key_hint=key)
    return cleaned


def _validate_value(value: Any, *, key_hint: str) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return dt.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        raise JobPayloadPrivacyError(
            f"Binary value in field {key_hint!r} is forbidden in job payloads"
        )
    if isinstance(value, str):
        if len(value) > _MAX_STRING_VALUE_LEN:
            raise JobPayloadPrivacyError(
                f"String value in field {key_hint!r} exceeds {_MAX_STRING_VALUE_LEN} chars; "
                "pass entity UUIDs instead of raw text (Invariant I10)"
            )
        if mask_hard_pii(value).any_masked:
            raise JobPayloadPrivacyError(
                f"Hard PII detected in job payload field {key_hint!r} (Invariant I10)"
            )
        redacted = sanitize_log_value(value, key_hint=key_hint)
        if redacted != value:
            raise JobPayloadPrivacyError(
                f"Sensitive token or email detected in job payload field {key_hint!r} (Invariant I10)"
            )
        return value
    if isinstance(value, Mapping):
        return _validate_mapping(value)
    if isinstance(value, list | tuple):
        return [_validate_value(item, key_hint=key_hint) for item in value]
    raise JobPayloadPrivacyError(
        f"Unsupported value type {type(value).__name__} in job payload field {key_hint!r}"
    )


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class PostgresJobQueue:
    """Transactional PostgreSQL job queue (`background_jobs` table) with idempotent deduplication."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()

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
    ) -> UUID:
        cleaned_task = task_name.strip()
        if not cleaned_task or len(cleaned_task) > 80:
            raise ValueError("task_name must be 1..80 non-empty characters")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        cleaned_idem: str | None = None
        if idempotency_key is not None:
            cleaned_idem = idempotency_key.strip()
            if not cleaned_idem:
                cleaned_idem = None
            elif len(cleaned_idem) > 160:
                raise ValueError("idempotency_key must not exceed 160 characters")

        safe_payload = sanitize_and_validate_job_payload(payload)
        now = _ensure_utc(self._clock.now())
        scheduled_at = _ensure_utc(run_at) if run_at is not None else now
        job_id = new_id()

        values: dict[str, Any] = {
            "id": job_id,
            "user_id": user_id,
            "task_name": cleaned_task,
            "payload_json": safe_payload,
            "idempotency_key": cleaned_idem,
            "status": JobStatus.QUEUED.value,
            "attempts": 0,
            "max_attempts": max_attempts,
            "run_at": scheduled_at,
            "locked_until": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }

        if cleaned_idem is None:
            stmt = pg_insert(BackgroundJobRow).values(**values).returning(BackgroundJobRow.id)
            inserted = await session.scalar(stmt)
            assert inserted is not None
            return inserted

        dedup_stmt = (
            pg_insert(BackgroundJobRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(BackgroundJobRow.id)
        )
        inserted_id = await session.scalar(dedup_stmt)
        if inserted_id is not None:
            return inserted_id

        existing_id = await session.scalar(
            select(BackgroundJobRow.id).where(BackgroundJobRow.idempotency_key == cleaned_idem)
        )
        if existing_id is None:
            raise RuntimeError("Concurrent background_jobs idempotency lookup failed")
        return existing_id


if TYPE_CHECKING:
    _queue_port_check: JobQueuePort = PostgresJobQueue()
