"""FastAPI router for `/reminders` and `/devices` (mounted under `/v1` by main app)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    parse_if_match_version,
    parse_uuid_param,
    require_idempotency_key,
)
from mindweave.core.dto import CursorPage
from mindweave.core.idempotency import hash_request_payload
from mindweave.modules.reminders.api.dto import (
    CreateReminderRequest,
    DeviceResponse,
    RegisterDeviceRequest,
    ReminderResponse,
    UpdateReminderRequest,
)
from mindweave.modules.reminders.application.service import (
    CreateReminderInput,
    ReminderService,
    UpdateReminderInput,
)
from mindweave.shared.enums import ReminderStatus
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["reminders"])


def _etag_headers(version: int) -> dict[str, str]:
    return {"ETag": f'"{version}"'}


@router.post(
    "/devices",
    response_model=DeviceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_device(
    request: Request,
    body: RegisterDeviceRequest,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    idem_key: str = Depends(require_idempotency_key),
) -> JSONResponse:
    req_hash = hash_request_payload(body.model_dump(mode="json", by_alias=True))
    service = ReminderService(container)

    async with container.db.user_session(scope) as session:
        idem_row, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="POST",
            path=str(request.url.path),
            request_hash=req_hash,
        )
        if hit is not None:
            return JSONResponse(status_code=hit.status_code, content=hit.response_body)

        device, created = await service.register_device(
            session,
            scope,
            platform=body.platform,
            push_token=body.push_token,
            device_label=body.device_label,
            enabled=body.enabled,
            requested_id=body.id,
        )
        dto = DeviceResponse.from_domain(device)
        payload = dto.model_dump(mode="json", by_alias=True)
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        assert idem_row is not None
        await container.idempotency.complete(
            session,
            idem_row,
            status_code=http_status,
            response_body=payload,
        )
    return JSONResponse(status_code=http_status, content=payload)


@router.get("/devices", response_model=CursorPage[DeviceResponse])
async def list_devices(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[DeviceResponse]:
    service = ReminderService(container)
    async with container.db.user_session(scope) as session:
        items, next_cursor, has_more = await service.list_devices(
            session,
            scope,
            cursor=cursor,
            limit=limit,
        )
    return CursorPage[DeviceResponse](
        items=[DeviceResponse.from_domain(item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    did = parse_uuid_param(device_id, field_name="deviceId")
    service = ReminderService(container)
    async with container.db.user_session(scope) as session:
        await service.delete_device(session, scope, did)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reminders", response_model=CursorPage[ReminderResponse])
async def list_reminders(
    reminder_status: ReminderStatus | None = Query(default=None, alias="status"),
    person_id: UUID | None = Query(default=None, alias="personId"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[ReminderResponse]:
    service = ReminderService(container)
    async with container.db.user_session(scope) as session:
        items, next_cursor, has_more = await service.list_reminders(
            session,
            scope,
            status=reminder_status,
            person_id=person_id,
            cursor=cursor,
            limit=limit,
        )
    return CursorPage[ReminderResponse](
        items=[ReminderResponse.from_domain(item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post(
    "/reminders",
    response_model=ReminderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_reminder(
    request: Request,
    body: CreateReminderRequest,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    idem_key: str = Depends(require_idempotency_key),
) -> JSONResponse:
    req_hash = hash_request_payload(body.model_dump(mode="json", by_alias=True))
    service = ReminderService(container)

    async with container.db.user_session(scope) as session:
        idem_row, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="POST",
            path=str(request.url.path),
            request_hash=req_hash,
        )
        if hit is not None:
            replay_ver = int(hit.response_body.get("version", 1))
            return JSONResponse(
                status_code=hit.status_code,
                content=hit.response_body,
                headers=_etag_headers(replay_ver),
            )

        created = await service.create_reminder(
            session,
            scope,
            CreateReminderInput(
                requested_id=body.id,
                person_id=body.person_id,
                fact_id=body.fact_id,
                title=body.title,
                rule=body.rule,
                is_lunar=body.is_lunar,
                target_month=body.target_month,
                target_day=body.target_day,
                is_leap_month=body.is_leap_month,
                once_date=body.once_date,
                fire_hour=body.fire_hour,
                fire_minute=body.fire_minute,
                lead_days=body.lead_days,
                timezone=body.timezone,
                status=body.status,
            ),
        )
        dto = ReminderResponse.from_domain(created)
        payload = dto.model_dump(mode="json", by_alias=True)
        assert idem_row is not None
        await container.idempotency.complete(
            session,
            idem_row,
            status_code=status.HTTP_201_CREATED,
            response_body=payload,
        )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload,
        headers=_etag_headers(dto.version),
    )


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
async def patch_reminder(
    request: Request,
    reminder_id: str,
    body: UpdateReminderRequest,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    expected_version: int | None = Depends(parse_if_match_version),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    rid = parse_uuid_param(reminder_id, field_name="reminderId")
    service = ReminderService(container)
    cleaned_idem = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None
    req_hash = hash_request_payload(
        {
            "body": body.model_dump(mode="json", by_alias=True, exclude_unset=True),
            "ifMatch": expected_version,
        }
    )

    async with container.db.user_session(scope) as session:
        idem_row = None
        if cleaned_idem is not None:
            idem_row, hit = await container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=cleaned_idem,
                method="PATCH",
                path=str(request.url.path),
                request_hash=req_hash,
            )
            if hit is not None:
                replay_ver = int(hit.response_body.get("version", 1))
                return JSONResponse(
                    status_code=hit.status_code,
                    content=hit.response_body,
                    headers=_etag_headers(replay_ver),
                )

        updated = await service.update_reminder(
            session,
            scope,
            rid,
            UpdateReminderInput(
                title=body.title,
                rule=body.rule,
                is_lunar=body.is_lunar,
                target_month=body.target_month,
                target_day=body.target_day,
                is_leap_month=body.is_leap_month,
                once_date=body.once_date,
                fire_hour=body.fire_hour,
                fire_minute=body.fire_minute,
                lead_days=body.lead_days,
                timezone=body.timezone,
                status=body.status,
            ),
            expected_version=expected_version,
        )
        dto = ReminderResponse.from_domain(updated)
        payload = dto.model_dump(mode="json", by_alias=True)
        if idem_row is not None:
            await container.idempotency.complete(
                session,
                idem_row,
                status_code=status.HTTP_200_OK,
                response_body=payload,
            )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=payload,
        headers=_etag_headers(dto.version),
    )


@router.delete("/reminders/{reminder_id}", response_model=ReminderResponse)
async def delete_reminder(
    reminder_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    expected_version: int | None = Depends(parse_if_match_version),
) -> JSONResponse:
    rid = parse_uuid_param(reminder_id, field_name="reminderId")
    service = ReminderService(container)
    async with container.db.user_session(scope) as session:
        cancelled = await service.cancel_reminder(
            session,
            scope,
            rid,
            expected_version=expected_version,
        )
    dto = ReminderResponse.from_domain(cancelled)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=dto.model_dump(mode="json", by_alias=True),
        headers=_etag_headers(dto.version),
    )


@router.post("/reminders/{reminder_id}:done", response_model=ReminderResponse)
async def mark_reminder_done(
    request: Request,
    reminder_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    idem_key: str = Depends(require_idempotency_key),
) -> JSONResponse:
    rid = parse_uuid_param(reminder_id, field_name="reminderId")
    req_hash = hash_request_payload({"action": "done", "reminderId": str(rid)})
    service = ReminderService(container)

    async with container.db.user_session(scope) as session:
        idem_row, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="POST",
            path=str(request.url.path),
            request_hash=req_hash,
        )
        if hit is not None:
            replay_ver = int(hit.response_body.get("version", 1))
            return JSONResponse(
                status_code=hit.status_code,
                content=hit.response_body,
                headers=_etag_headers(replay_ver),
            )

        done_item = await service.mark_reminder_done(session, scope, rid)
        dto = ReminderResponse.from_domain(done_item)
        payload = dto.model_dump(mode="json", by_alias=True)
        assert idem_row is not None
        await container.idempotency.complete(
            session,
            idem_row,
            status_code=status.HTTP_200_OK,
            response_body=payload,
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=payload,
        headers=_etag_headers(dto.version),
    )
