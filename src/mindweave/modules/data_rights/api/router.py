"""FastAPI router for PIPL Art. 45/47 data export and account deletion (`/me/exports`, `/me`)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    parse_uuid_param,
    require_idempotency_key,
    require_step_up_reauth,
)
from mindweave.core.errors import ForbiddenError
from mindweave.core.idempotency import hash_request_payload
from mindweave.core.orm import ExportJobRow
from mindweave.modules.data_rights.api.schemas import (
    CreateExportRequest,
    DeleteAccountResponse,
    ExportJobListResponse,
    ExportJobResponse,
)
from mindweave.modules.data_rights.application.service import DataRightsService
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["data-rights"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _row_to_response(
    row: ExportJobRow,
    *,
    api_prefix: str = "/v1",
    download_token: str | None = None,
) -> ExportJobResponse:
    download_path = (
        f"{api_prefix}/me/exports/{row.id}/download?token={download_token}"
        if download_token is not None
        else None
    )
    return ExportJobResponse(
        export_id=row.id,
        status=row.status,
        byte_size=row.byte_size,
        sha256_hex=row.sha256_hex,
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        downloaded_at=row.downloaded_at,
        download_token=download_token,
        download_path=download_path,
    )


@router.post(
    "/me/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_export(
    request: Request,
    payload: CreateExportRequest | None = Body(default=None),
    scope: UserScope = Depends(require_step_up_reauth),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> ExportJobResponse | JSONResponse:
    effective_payload = payload or CreateExportRequest()
    req_hash = hash_request_payload(effective_payload.model_dump(mode="json", by_alias=True))
    service = DataRightsService(container)

    async with container.db.user_session(scope) as session:
        idem_row, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="POST",
            path="/me/exports",
            request_hash=req_hash,
        )

    if hit is not None:
        replay_body = dict(hit.response_body)
        raw_export_id = replay_body.get("exportId")
        if raw_export_id is not None:
            row, fresh_token = await service.get_export_job(
                scope=scope,
                export_id=UUID(str(raw_export_id)),
                issue_download_token=True,
            )
            replayed = _row_to_response(
                row,
                api_prefix=container.settings.api_prefix,
                download_token=fresh_token,
            )
            return JSONResponse(
                status_code=hit.status_code,
                content=replayed.model_dump(mode="json", by_alias=True),
            )
        return JSONResponse(status_code=hit.status_code, content=replay_body)

    built = await service.create_export(
        scope=scope,
        async_mode=effective_payload.async_mode,
        client_ip=_client_ip(request),
    )

    download_path = (
        f"{container.settings.api_prefix}/me/exports/{built.export_id}/download?token={built.download_token}"
        if built.download_token is not None
        else None
    )
    response = ExportJobResponse(
        export_id=built.export_id,
        status=built.status,
        byte_size=built.byte_size,
        sha256_hex=built.sha256_hex,
        created_at=built.created_at,
        completed_at=built.completed_at,
        expires_at=built.expires_at,
        downloaded_at=None,
        download_token=built.download_token,
        download_path=download_path,
    )

    stored_body = response.model_dump(mode="json", by_alias=True)
    stored_body["downloadToken"] = None
    stored_body["downloadPath"] = None

    async with container.db.user_session(scope) as session:
        if idem_row is not None:
            persisted = await session.merge(idem_row)
            await container.idempotency.complete(
                session,
                persisted,
                status_code=status.HTTP_201_CREATED,
                response_body=stored_body,
            )

    return response


@router.get(
    "/me/exports",
    response_model=ExportJobListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_data_exports(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> ExportJobListResponse:
    service = DataRightsService(container)
    rows = await service.list_export_jobs(scope=scope)
    return ExportJobListResponse(items=[_row_to_response(r) for r in rows])


@router.get(
    "/me/exports/{export_id}",
    response_model=ExportJobResponse,
    status_code=status.HTTP_200_OK,
)
async def get_data_export(
    export_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    x_reauth_token: str | None = Header(default=None, alias="X-Reauth-Token"),
) -> ExportJobResponse:
    export_uuid = parse_uuid_param(export_id, field_name="export_id")
    has_valid_reauth = False
    if x_reauth_token:
        container.tokens.verify_reauth_token(
            x_reauth_token,
            expected_user_id=scope.user_id,
            now=container.clock.now(),
        )
        has_valid_reauth = True
    service = DataRightsService(container)
    row, fresh_token = await service.get_export_job(
        scope=scope,
        export_id=export_uuid,
        issue_download_token=has_valid_reauth,
    )
    return _row_to_response(
        row,
        api_prefix=container.settings.api_prefix,
        download_token=fresh_token,
    )


@router.get(
    "/me/exports/{export_id}/download",
    status_code=status.HTTP_200_OK,
    responses={200: {"content": {"application/zip": {}}}},
)
async def download_data_export(
    export_id: str,
    request: Request,
    token: str | None = Query(default=None),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    export_uuid = parse_uuid_param(export_id, field_name="export_id")
    if not token or not token.strip():
        raise ForbiddenError(
            "One-time download token query parameter is required.",
            error_code="export_missing_token",
        )

    service = DataRightsService(container)
    zip_bytes, filename = await service.download_export(
        scope=scope,
        export_id=export_uuid,
        token=token,
        client_ip=_client_ip(request),
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.delete(
    "/me",
    response_model=DeleteAccountResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_my_account(
    request: Request,
    scope: UserScope = Depends(require_step_up_reauth),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> DeleteAccountResponse | JSONResponse:
    req_hash = hash_request_payload({"action": "delete_account", "user_id": str(scope.user_id)})
    async with container.db.user_session(scope) as session:
        _, hit = await container.idempotency.claim_or_replay(
            session,
            scope=scope,
            idem_key=idem_key,
            method="DELETE",
            path="/me",
            request_hash=req_hash,
        )
        if hit is not None:
            return JSONResponse(status_code=hit.status_code, content=hit.response_body)

    service = DataRightsService(container)
    result = await service.delete_account(
        scope=scope,
        client_ip=_client_ip(request),
    )
    return DeleteAccountResponse(
        deleted=result.deleted,
        crypto_shredded=result.crypto_shredded,
        deleted_blobs_count=result.deleted_blobs_count,
        cancelled_jobs_count=result.cancelled_jobs_count,
        deleted_at=result.deleted_at,
        audit_receipt_id=result.audit_receipt_id,
    )
