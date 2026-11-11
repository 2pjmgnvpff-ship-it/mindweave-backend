"""Database-backed Idempotency-Key deduplication service (Invariant I11)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.errors import ConflictError, IdempotencyConflictError, ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.core.orm import IdempotencyKeyRow
from mindweave.shared.scopes import UserScope


def hash_request_payload(payload: Any) -> str:
    if isinstance(payload, bytes | bytearray):
        return hashlib.sha256(payload).hexdigest()
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotencyHit:
    status_code: int
    response_body: dict[str, Any]


class IdempotencyService:
    def __init__(self, clock: Clock, *, ttl_hours: int = 24) -> None:
        self._clock = clock
        self._ttl_hours = ttl_hours

    async def claim_or_replay(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        idem_key: str,
        method: str,
        path: str,
        request_hash: str,
    ) -> tuple[IdempotencyKeyRow | None, IdempotencyHit | None]:
        cleaned_key = idem_key.strip()
        if not cleaned_key or len(cleaned_key) > 128:
            raise ValidationFailedError("Idempotency-Key must be 1..128 non-empty characters")

        now = self._clock.now()
        row_id = new_id()
        stmt = (
            pg_insert(IdempotencyKeyRow)
            .values(
                id=row_id,
                user_id=scope.user_id,
                idem_key=cleaned_key,
                method=method.upper(),
                path=path,
                request_hash=request_hash,
                status="in_progress",
                created_at=now,
                expires_at=now + timedelta(hours=self._ttl_hours),
            )
            .on_conflict_do_nothing(index_elements=["user_id", "idem_key", "method", "path"])
            .returning(IdempotencyKeyRow.id)
        )
        inserted_id = await session.scalar(stmt)
        if inserted_id is not None:
            row = await session.get(IdempotencyKeyRow, inserted_id)
            return row, None

        existing = await session.scalar(
            select(IdempotencyKeyRow)
            .where(
                IdempotencyKeyRow.user_id == scope.user_id,
                IdempotencyKeyRow.idem_key == cleaned_key,
                IdempotencyKeyRow.method == method.upper(),
                IdempotencyKeyRow.path == path,
            )
            .with_for_update()
        )
        if existing is None:
            raise ConflictError("Concurrent idempotency conflict")

        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request payload."
            )

        if existing.status != "completed" or existing.response_body is None:
            raise ConflictError(
                "A request with this Idempotency-Key is currently in progress.",
                error_code="idempotency_in_progress",
            )

        return None, IdempotencyHit(
            status_code=existing.response_status or 200,
            response_body=existing.response_body,
        )

    @staticmethod
    async def complete(
        session: AsyncSession,
        row: IdempotencyKeyRow,
        *,
        status_code: int,
        response_body: dict[str, Any],
    ) -> None:
        row.status = "completed"
        row.response_status = status_code
        row.response_body = response_body
        await session.flush()
