"""Application service for privacy-safe telemetry ingestion and summary queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.errors import ValidationFailedError
from mindweave.modules.telemetry.domain.allowlist import (
    TelemetryPolicyViolationError,
    TelemetrySummary,
    validate_telemetry_batch,
    validate_telemetry_event,
)
from mindweave.modules.telemetry.infra.repository import TelemetryRepository
from mindweave.shared.scopes import UserScope


class TelemetryService:
    """Validates PRD §8 telemetry events against strict allowlist and persists without PII."""

    def __init__(self, *, clock: Clock, repo: TelemetryRepository | None = None) -> None:
        self._clock = clock
        self._repo = repo or TelemetryRepository()

    async def ingest_events(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        raw_events: Sequence[tuple[str, Mapping[str, Any] | None, datetime | None]],
    ) -> list[UUID]:
        now = self._clock.now()
        normalized = [
            (name, meta, occurred_at if occurred_at is not None else now)
            for name, meta, occurred_at in raw_events
        ]
        try:
            validated = validate_telemetry_batch(normalized)
        except TelemetryPolicyViolationError as exc:
            raise ValidationFailedError(
                exc.detail,
                extensions={"reason": exc.reason_code},
            ) from exc

        return await self._repo.insert_many(
            session,
            scope=scope,
            events=validated,
            created_at=now,
        )

    async def record_event(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        event_name: str,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> UUID:
        now = self._clock.now()
        try:
            validated = validate_telemetry_event(
                event_name,
                metadata,
                occurred_at=occurred_at or now,
            )
        except TelemetryPolicyViolationError as exc:
            raise ValidationFailedError(
                exc.detail,
                extensions={"reason": exc.reason_code},
            ) from exc

        ids = await self._repo.insert_many(
            session,
            scope=scope,
            events=[validated],
            created_at=now,
        )
        return ids[0]

    async def get_summary(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
    ) -> TelemetrySummary:
        return await self._repo.get_summary(session, scope=scope)
