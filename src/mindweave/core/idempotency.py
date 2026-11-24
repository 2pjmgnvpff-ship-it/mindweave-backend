"""Database-backed Idempotency-Key deduplication service (Invariant I11)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.errors import (
    ConflictError,
    IdempotencyConflictError,
    UnauthorizedError,
    ValidationFailedError,
)
from mindweave.core.ids import new_id
from mindweave.core.orm import IdempotencyKeyRow, UserKeyRow
from mindweave.shared.scopes import UserScope

_ENC_ENVELOPE_KEY = "_enc_v1"


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
    def __init__(
        self,
        clock: Clock,
        *,
        cipher: EnvelopeCipher | None = None,
        ttl_hours: int = 72,
        stale_in_progress_seconds: int = 45,
        coalesce_wait_seconds: float = 3.0,
        coalesce_poll_interval_seconds: float = 0.4,
    ) -> None:
        self._clock = clock
        self._cipher = cipher
        self._ttl_hours = ttl_hours
        self._stale_seconds = stale_in_progress_seconds
        self._coalesce_wait_seconds = max(0.0, coalesce_wait_seconds)
        self._coalesce_poll_interval_seconds = max(0.05, coalesce_poll_interval_seconds)

    async def _load_user_dek(self, session: AsyncSession, user_id: UUID) -> bytes:
        assert self._cipher is not None
        key_row = await session.get(UserKeyRow, user_id)
        if key_row is None:
            raise UnauthorizedError("User encryption key not provisioned")
        return self._cipher.load_dek(
            user_id=user_id,
            wrapped_dek=key_row.wrapped_dek,
            kek_id=key_row.kek_id,
            destroyed_at=key_row.destroyed_at,
        )

    async def _decrypt_replay(
        self,
        session: AsyncSession,
        scope: UserScope,
        existing: IdempotencyKeyRow,
    ) -> IdempotencyHit:
        stored_body = existing.response_body or {}
        if _ENC_ENVELOPE_KEY in stored_body and self._cipher is not None:
            dek = await self._load_user_dek(session, scope.user_id)
            ciphertext = base64.b64decode(str(stored_body[_ENC_ENVELOPE_KEY]))
            decrypted = self._cipher.decrypt_json(
                dek,
                ciphertext,
                user_id=scope.user_id,
                table="idempotency_keys",
                column="response_body",
                row_id=existing.id,
            )
            replayed_body = dict(decrypted) if isinstance(decrypted, dict) else {}
        else:
            replayed_body = dict(stored_body)
        return IdempotencyHit(
            status_code=existing.response_status or 200,
            response_body=replayed_body,
        )

    async def claim_or_replay(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        idem_key: str,
        method: str,
        path: str,
        request_hash: str,
        coalesce_wait_seconds: float | None = None,
    ) -> tuple[IdempotencyKeyRow | None, IdempotencyHit | None]:
        cleaned_key = idem_key.strip()
        if not cleaned_key or len(cleaned_key) > 128 or "\x00" in cleaned_key:
            raise ValidationFailedError("Idempotency-Key must be 1..128 valid characters")

        method_upper = method.upper()
        now = self._clock.now()
        row_id = new_id()
        stmt = (
            pg_insert(IdempotencyKeyRow)
            .values(
                id=row_id,
                user_id=scope.user_id,
                idem_key=cleaned_key,
                method=method_upper,
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

        lookup_stmt = select(IdempotencyKeyRow).where(
            IdempotencyKeyRow.user_id == scope.user_id,
            IdempotencyKeyRow.idem_key == cleaned_key,
            IdempotencyKeyRow.method == method_upper,
            IdempotencyKeyRow.path == path,
        )

        # Check without FOR UPDATE first so coalescing polls do not block the in-flight worker's
        # completion UPDATE in PostgreSQL READ COMMITTED.
        peek = await session.scalar(lookup_stmt)
        if (
            peek is not None
            and peek.expires_at > now
            and peek.request_hash == request_hash
            and (peek.status != "completed" or peek.response_body is None)
            and peek.created_at > now - timedelta(seconds=self._stale_seconds)
        ):
            wait_budget = (
                self._coalesce_wait_seconds
                if coalesce_wait_seconds is None
                else max(0.0, coalesce_wait_seconds)
            )
            if wait_budget > 0.0:
                deadline = time.monotonic() + wait_budget
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    await asyncio.sleep(min(self._coalesce_poll_interval_seconds, max(0.01, remaining)))
                    session.expire(peek)
                    refreshed = await session.scalar(lookup_stmt)
                    if refreshed is None:
                        break
                    peek = refreshed
                    if peek.status == "completed" and peek.response_body is not None:
                        return None, await self._decrypt_replay(session, scope, peek)
                    if peek.created_at <= self._clock.now() - timedelta(seconds=self._stale_seconds):
                        break

        existing = await session.scalar(lookup_stmt.with_for_update())
        if existing is None:
            # Original in-flight request released its lock; retry insert cleanly.
            retry_id = await session.scalar(stmt)
            if retry_id is not None:
                return await session.get(IdempotencyKeyRow, retry_id), None
            raise ConflictError(
                "Concurrent idempotency conflict",
                error_code="idempotency_in_progress",
                extensions={"retryAfterSeconds": 2},
                headers={"Retry-After": "2"},
            )

        now = self._clock.now()
        if existing.expires_at <= now:
            existing.request_hash = request_hash
            existing.status = "in_progress"
            existing.response_status = None
            existing.response_body = None
            existing.created_at = now
            existing.expires_at = now + timedelta(hours=self._ttl_hours)
            await session.flush()
            return existing, None

        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key was already used with a different request payload."
            )

        if existing.status != "completed" or existing.response_body is None:
            if existing.created_at <= now - timedelta(seconds=self._stale_seconds):
                existing.status = "in_progress"
                existing.created_at = now
                existing.expires_at = now + timedelta(hours=self._ttl_hours)
                await session.flush()
                return existing, None
            raise ConflictError(
                "A request with this Idempotency-Key is currently in progress.",
                error_code="idempotency_in_progress",
                extensions={"retryAfterSeconds": 2},
                headers={"Retry-After": "2"},
            )

        return None, await self._decrypt_replay(session, scope, existing)

    async def complete(
        self,
        session: AsyncSession,
        row: IdempotencyKeyRow,
        *,
        status_code: int,
        response_body: dict[str, Any],
    ) -> None:
        row.status = "completed"
        row.response_status = status_code
        if self._cipher is not None:
            dek = await self._load_user_dek(session, row.user_id)
            ciphertext = self._cipher.encrypt_json(
                dek,
                response_body,
                user_id=row.user_id,
                table="idempotency_keys",
                column="response_body",
                row_id=row.id,
            )
            row.response_body = {_ENC_ENVELOPE_KEY: base64.b64encode(ciphertext).decode("ascii")}
        else:
            row.response_body = response_body
        await session.flush()

    @staticmethod
    async def release(
        session: AsyncSession,
        *,
        scope: UserScope,
        idem_key: str,
        method: str,
        path: str,
    ) -> None:
        await session.execute(
            delete(IdempotencyKeyRow).where(
                IdempotencyKeyRow.user_id == scope.user_id,
                IdempotencyKeyRow.idem_key == idem_key.strip(),
                IdempotencyKeyRow.method == method.upper(),
                IdempotencyKeyRow.path == path,
                IdempotencyKeyRow.status != "completed",
            )
        )
        await session.flush()
