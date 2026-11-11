"""FastAPI router for Materials (`RecordRow`), Facts (`FactRow`), and Pending Inbox (`InboxItemRow`)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    get_media_user_scope,
    parse_if_match_version,
    parse_uuid_param,
    require_idempotency_key,
)
from mindweave.core.dto import CursorPage
from mindweave.core.errors import ValidationFailedError
from mindweave.modules.memory.application.dto import (
    FactCreateRequest,
    FactDto,
    FactPatchRequest,
    InboxItemDto,
    InboxResolveRequest,
    InboxResolveResponse,
    MaterialCreateRequest,
    MaterialDto,
    MaterialPatchRequest,
)
from mindweave.modules.memory.application.service import MemoryService
from mindweave.modules.memory.infra.repository import expire_stale_venting_items
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemStatus,
    RecordKind,
)
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["memory"])

__all__ = ["expire_stale_venting_items", "router"]


def _parse_bool_flag(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


async def _parse_material_create_request(
    request: Request,
) -> tuple[MaterialCreateRequest, bytes | None]:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        photo_bytes: bytes | None = None
        for field_name in ("photo", "image", "file"):
            upload = form.get(field_name)
            if isinstance(upload, UploadFile):
                photo_bytes = await upload.read()
                break

        raw_id = form.get("id")
        raw_kind = form.get("kind") or ("photo" if photo_bytes is not None else "note")
        raw_title = form.get("title")
        raw_body = form.get("body")
        raw_snippet = form.get("snippet")
        raw_stamp = form.get("stampCode") or form.get("stamp_code")
        raw_happened = form.get("happenedOn") or form.get("happened_on")
        raw_pinned = form.get("pinned")

        try:
            dto = MaterialCreateRequest(
                id=UUID(str(raw_id)) if raw_id else None,
                kind=RecordKind(str(raw_kind)),
                title=str(raw_title or ""),
                body=str(raw_body or ""),
                snippet=str(raw_snippet) if raw_snippet is not None else None,
                stamp_code=str(raw_stamp) if raw_stamp is not None else None,
                happened_on=date.fromisoformat(str(raw_happened)) if raw_happened else None,
                pinned=_parse_bool_flag(raw_pinned, default=True),
            )
        except (ValueError, ValidationError) as exc:
            raise ValidationFailedError("Invalid material form parameters") from exc
        return dto, photo_bytes

    json_bytes = await request.body()
    try:
        dto = MaterialCreateRequest.model_validate_json(json_bytes)
    except ValidationError as exc:
        raise ValidationFailedError("Invalid material request body") from exc
    return dto, None


# ============================================================================
# Materials Endpoints
# ============================================================================


@router.get("/persons/{person_id}/materials", response_model=CursorPage[MaterialDto])
async def list_person_materials(
    person_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    include_held: bool = Query(default=False, alias="includeHeld"),
    kind: RecordKind | None = Query(default=None),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[MaterialDto]:
    pid = parse_uuid_param(person_id, field_name="person_id")
    svc = MemoryService(container)
    return await svc.list_person_materials(
        scope=scope,
        person_id=pid,
        cursor=cursor,
        limit=limit,
        include_held=include_held,
        kind=kind,
    )


def _resign_material_hit_body(
    container: AppContainer,
    scope: UserScope,
    response_body: dict[str, object],
) -> dict[str, object]:
    body = dict(response_body)
    mat_id = body.get("id")
    if body.get("hasMedia") and mat_id:
        body["mediaUrl"] = container.tokens.sign_media_path(
            f"/v1/materials/{mat_id}/media",
            user_id=scope.user_id,
            now=container.clock.now(),
        )
    return body


@router.post(
    "/persons/{person_id}/materials",
    response_model=MaterialDto,
    status_code=201,
)
async def create_person_material(
    person_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    pid = parse_uuid_param(person_id, field_name="person_id")
    req_dto, photo_bytes = await _parse_material_create_request(request)
    svc = MemoryService(container)
    created, hit = await svc.create_material(
        scope=scope,
        person_id=pid,
        req=req_dto,
        photo_bytes=photo_bytes,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        body = _resign_material_hit_body(container, scope, hit.response_body)
        headers = {"ETag": f'"{body["version"]}"'} if "version" in body else {}
        return JSONResponse(status_code=hit.status_code, content=body, headers=headers)
    assert created is not None
    return JSONResponse(
        status_code=201,
        content=created.model_dump(mode="json", by_alias=True),
        headers={"ETag": f'"{created.version}"'},
    )


@router.get("/materials/{material_id}", response_model=MaterialDto)
async def get_material(
    material_id: str,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> MaterialDto:
    mid = parse_uuid_param(material_id, field_name="material_id")
    svc = MemoryService(container)
    dto = await svc.get_material(scope=scope, material_id=mid)
    response.headers["ETag"] = f'"{dto.version}"'
    return dto


@router.patch("/materials/{material_id}", response_model=MaterialDto)
async def patch_material(
    material_id: str,
    req: MaterialPatchRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    expected_version: int | None = Depends(parse_if_match_version),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    mid = parse_uuid_param(material_id, field_name="material_id")
    svc = MemoryService(container)
    dto, hit = await svc.patch_material(
        scope=scope,
        material_id=mid,
        req=req,
        expected_version=expected_version,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        body = _resign_material_hit_body(container, scope, hit.response_body)
        headers = {"ETag": f'"{body["version"]}"'} if "version" in body else {}
        return JSONResponse(status_code=hit.status_code, content=body, headers=headers)
    assert dto is not None
    return JSONResponse(
        status_code=200,
        content=dto.model_dump(mode="json", by_alias=True),
        headers={"ETag": f'"{dto.version}"'},
    )


@router.delete("/materials/{material_id}", status_code=204)
async def delete_material(
    material_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    mid = parse_uuid_param(material_id, field_name="material_id")
    svc = MemoryService(container)
    hit = await svc.delete_material(
        scope=scope,
        material_id=mid,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        return Response(status_code=204)
    return Response(status_code=204)


@router.get("/materials/{material_id}/media")
async def stream_material_media(
    material_id: str,
    scope: UserScope = Depends(get_media_user_scope),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    mid = parse_uuid_param(material_id, field_name="material_id")
    svc = MemoryService(container)
    plaintext, content_type = await svc.get_material_media(scope=scope, material_id=mid)
    return StreamingResponse(
        iter([plaintext]),
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(plaintext)),
        },
    )


# ============================================================================
# Facts Endpoints
# ============================================================================


@router.get("/persons/{person_id}/facts", response_model=CursorPage[FactDto])
async def list_person_facts(
    person_id: str,
    status: FactStatus | None = Query(default=None),
    fact_type: FactType | None = Query(default=None, alias="type"),
    include_invalidated: bool = Query(default=False, alias="includeInvalidated"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[FactDto]:
    pid = parse_uuid_param(person_id, field_name="person_id")
    svc = MemoryService(container)
    return await svc.list_person_facts(
        scope=scope,
        person_id=pid,
        status=status,
        fact_type=fact_type,
        include_invalidated=include_invalidated,
        cursor=cursor,
        limit=limit,
    )


@router.post("/persons/{person_id}/facts", response_model=FactDto, status_code=201)
async def create_person_fact(
    person_id: str,
    req: FactCreateRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    pid = parse_uuid_param(person_id, field_name="person_id")
    svc = MemoryService(container)
    created, hit = await svc.create_fact(
        scope=scope,
        person_id=pid,
        req=req,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        headers = (
            {"ETag": f'"{hit.response_body["version"]}"'} if "version" in hit.response_body else {}
        )
        return JSONResponse(status_code=hit.status_code, content=hit.response_body, headers=headers)
    assert created is not None
    return JSONResponse(
        status_code=201,
        content=created.model_dump(mode="json", by_alias=True),
        headers={"ETag": f'"{created.version}"'},
    )


@router.patch("/facts/{fact_id}", response_model=FactDto)
async def patch_fact(
    fact_id: str,
    req: FactPatchRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    expected_version: int | None = Depends(parse_if_match_version),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    fid = parse_uuid_param(fact_id, field_name="fact_id")
    svc = MemoryService(container)
    dto, hit = await svc.patch_fact(
        scope=scope,
        fact_id=fid,
        req=req,
        expected_version=expected_version,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        headers = (
            {"ETag": f'"{hit.response_body["version"]}"'} if "version" in hit.response_body else {}
        )
        return JSONResponse(status_code=hit.status_code, content=hit.response_body, headers=headers)
    assert dto is not None
    return JSONResponse(
        status_code=200,
        content=dto.model_dump(mode="json", by_alias=True),
        headers={"ETag": f'"{dto.version}"'},
    )


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    fid = parse_uuid_param(fact_id, field_name="fact_id")
    svc = MemoryService(container)
    hit = await svc.delete_fact(
        scope=scope,
        fact_id=fid,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        return Response(status_code=204)
    return Response(status_code=204)


# ============================================================================
# Pending Inbox Endpoints
# ============================================================================


@router.get("/inbox", response_model=CursorPage[InboxItemDto])
async def list_inbox_items(
    status: InboxItemStatus = Query(default=InboxItemStatus.OPEN),
    person_id: str | None = Query(default=None, alias="personId"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[InboxItemDto]:
    pid = parse_uuid_param(person_id, field_name="personId") if person_id else None
    svc = MemoryService(container)
    return await svc.list_inbox(
        scope=scope,
        status=status,
        person_id=pid,
        cursor=cursor,
        limit=limit,
    )


@router.post("/inbox/{item_id}:resolve", response_model=InboxResolveResponse)
async def resolve_inbox_item(
    item_id: str,
    req: InboxResolveRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    iid = parse_uuid_param(item_id, field_name="item_id")
    svc = MemoryService(container)
    resolved, hit = await svc.resolve_inbox_item(
        scope=scope,
        item_id=iid,
        req=req,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        return JSONResponse(status_code=hit.status_code, content=hit.response_body)
    assert resolved is not None
    return JSONResponse(
        status_code=200,
        content=resolved.model_dump(mode="json", by_alias=True),
    )
