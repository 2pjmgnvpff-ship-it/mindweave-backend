"""FastAPI router for privacy-safe telemetry (`/telemetry/events`, `/telemetry/summary`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    require_idempotency_key,
)
from mindweave.core.idempotency import hash_request_payload
from mindweave.modules.telemetry.api.schemas import (
    TelemetryBatchRequest,
    TelemetryEventItemRequest,
    TelemetryIngestResponse,
    TelemetrySummaryResponse,
)
from mindweave.modules.telemetry.application.service import TelemetryService
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["telemetry"])


@router.post(
    "/telemetry/events",
    response_model=TelemetryIngestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_telemetry_events(
    payload: TelemetryBatchRequest | TelemetryEventItemRequest,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> TelemetryIngestResponse | JSONResponse:
    items = payload.events if isinstance(payload, TelemetryBatchRequest) else [payload]
    raw_events = [(item.event_name, item.metadata, item.occurred_at) for item in items]
    serialized_req: dict[str, Any] = payload.model_dump(mode="json", by_alias=True)
    req_hash = hash_request_payload(serialized_req)

    service = TelemetryService(clock=container.clock)
    async with container.db.user_session(scope) as session:
        idem_row, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="POST",
            path="/telemetry/events",
            request_hash=req_hash,
        )
        if hit is not None:
            return JSONResponse(status_code=hit.status_code, content=hit.response_body)

        event_ids = await service.ingest_events(
            session,
            scope=scope,
            raw_events=raw_events,
        )
        response = TelemetryIngestResponse(
            accepted_count=len(event_ids),
            event_ids=event_ids,
        )
        if idem_row is not None:
            await container.idempotency.complete(
                session,
                idem_row,
                status_code=status.HTTP_201_CREATED,
                response_body=response.model_dump(mode="json", by_alias=True),
            )
        return response


@router.get(
    "/telemetry/summary",
    response_model=TelemetrySummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_telemetry_summary(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> TelemetrySummaryResponse:
    service = TelemetryService(clock=container.clock)
    async with container.db.user_session(scope) as session:
        summary = await service.get_summary(session, scope=scope)
        return TelemetrySummaryResponse(
            total_events=summary.total_events,
            counts_by_event=summary.counts_by_event,
            first_event_at=summary.first_event_at,
            last_event_at=summary.last_event_at,
        )
