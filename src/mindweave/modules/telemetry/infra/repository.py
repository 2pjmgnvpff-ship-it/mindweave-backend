"""SQLAlchemy repository for tenant-scoped privacy-safe telemetry events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.ids import new_id
from mindweave.core.orm import TelemetryEventRow
from mindweave.modules.telemetry.domain.allowlist import TelemetrySummary, ValidatedTelemetryEvent
from mindweave.shared.scopes import UserScope


class TelemetryRepository:
    """Tenant-isolated persistence adapter for `telemetry_events`."""

    async def insert_many(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        events: Sequence[ValidatedTelemetryEvent],
        created_at: datetime,
    ) -> list[UUID]:
        ids: list[UUID] = []
        rows: list[TelemetryEventRow] = []
        for event in events:
            event_id = new_id()
            ids.append(event_id)
            rows.append(
                TelemetryEventRow(
                    id=event_id,
                    user_id=scope.user_id,
                    event_name=event.event_name,
                    metadata_json=dict(event.metadata),
                    occurred_at=event.occurred_at,
                    created_at=created_at,
                )
            )
        session.add_all(rows)
        await session.flush()
        return ids

    async def get_summary(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
    ) -> TelemetrySummary:
        grouped_stmt = (
            select(
                TelemetryEventRow.event_name,
                func.count(TelemetryEventRow.id),
            )
            .where(TelemetryEventRow.user_id == scope.user_id)
            .group_by(TelemetryEventRow.event_name)
            .order_by(TelemetryEventRow.event_name.asc())
        )
        grouped_rows = (await session.execute(grouped_stmt)).all()
        counts_by_event = {str(name): int(cnt) for name, cnt in grouped_rows}
        total_events = sum(counts_by_event.values())

        bounds_stmt = select(
            func.min(TelemetryEventRow.occurred_at),
            func.max(TelemetryEventRow.occurred_at),
        ).where(TelemetryEventRow.user_id == scope.user_id)
        bounds_row = (await session.execute(bounds_stmt)).one()
        first_at: datetime | None = bounds_row[0]
        last_at: datetime | None = bounds_row[1]

        return TelemetrySummary(
            total_events=total_events,
            counts_by_event=counts_by_event,
            first_event_at=first_at,
            last_event_at=last_at,
        )
