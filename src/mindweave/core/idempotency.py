"""Database-backed Idempotency-Key deduplication service (Invariant I11)."""

from __future__ import annotations

import base64
import hashlib
import json
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
        ttl_hours: int = 24,
        stale_in_progress_seconds: int = 120,
    ) -> None:
        self._clock = clock
        self._cipher = cipher
        self._ttl_hours = ttl_hours
        self._stale_seconds = stale_in_progress_seconds

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
        if not cleaned_key or len(cleaned_key) > 128 or "\x00" in cleaned_key:
            raise ValidationFailedError("Idempotency-Key must be 1..128 valid characters")

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
            )

        stored_body = existing.response_body
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

        return None, IdempotencyHit(
            status_code=existing.response_status or 200,
            response_body=replayed_body,
        )

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
