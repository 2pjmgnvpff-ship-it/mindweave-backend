"""CamelCase API DTOs for MindWeave privacy-safe telemetry."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel


class TelemetryEventItemRequest(CamelModel):
    event_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class TelemetryBatchRequest(CamelModel):
    events: list[TelemetryEventItemRequest]


class TelemetryIngestResponse(CamelModel):
    accepted_count: int
    event_ids: list[UUID]


class TelemetrySummaryResponse(CamelModel):
    total_events: int
    counts_by_event: dict[str, int]
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
