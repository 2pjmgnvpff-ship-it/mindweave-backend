"""FastAPI router for Multimodal Capture, V10 `/voice/ingest`, SSE Events, and 5s Undo."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import FormData, UploadFile

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    parse_uuid_param,
    require_idempotency_key,
)
from mindweave.core.errors import ValidationFailedError
from mindweave.modules.capture.application.dto import (
    CaptureCreateJsonRequest,
    CaptureResponse,
    CaptureUndoResponse,
    VoiceIngestResponse,
)
from mindweave.modules.capture.application.service import (
    CaptureIngestCommand,
    CapturePipelineService,
    decode_optional_base64,
)
from mindweave.shared.enums import CaptureSource, RouteMode
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["capture"])


def _reject_null_bytes_in_form(form: FormData) -> None:
    for key, value in form.multi_items():
        if "\x00" in key:
            raise ValidationFailedError("Null bytes are not permitted in form fields")
        if isinstance(value, str):
            if "\x00" in value:
                raise ValidationFailedError("Null bytes are not permitted in form fields")
        elif isinstance(value, UploadFile) and value.filename and "\x00" in value.filename:
            raise ValidationFailedError("Null bytes are not permitted in form fields")


def _parse_optional_bool(raw: object) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("", "null", "none", "undefined"):
        return None
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValidationFailedError(f"Invalid boolean flag value: {raw!r}")


def _parse_optional_uuid_form(raw: object, *, field_name: str) -> UUID | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return None
    return parse_uuid_param(s, field_name=field_name)


def _parse_route_mode(raw: object, *, default: RouteMode = RouteMode.GLOBAL) -> RouteMode:
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    try:
        return RouteMode(s)
    except ValueError as exc:
        raise ValidationFailedError(f"Unsupported routeMode: {s!r}") from exc


def _parse_capture_source(raw: object, *, fallback: CaptureSource) -> CaptureSource:
    if raw is None:
        return fallback
    s = str(raw).strip()
    if not s:
        return fallback
    try:
        return CaptureSource(s)
    except ValueError as exc:
        raise ValidationFailedError(f"Unsupported capture source: {s!r}") from exc


async def _parse_capture_ingest_request(
    request: Request,
    *,
    scope: UserScope,
    idem_key: str | None,
    sync_query: bool | None,
    default_voice_endpoint: bool = False,
) -> CaptureIngestCommand:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form()
        _reject_null_bytes_in_form(form)
        audio_bytes: bytes | None = None
        image_bytes: bytes | None = None

        for audio_field in ("audio", "voice", "file"):
            up = form.get(audio_field)
            if isinstance(up, UploadFile):
                data = await up.read()
                if data:
                    audio_bytes = data
                    break

        for image_field in ("image", "screenshot", "photo"):
            up_img = form.get(image_field)
            if isinstance(up_img, UploadFile):
                data_img = await up_img.read()
                if data_img:
                    image_bytes = data_img
                    break

        raw_text_val = form.get("text") or form.get("transcript")
        text_str = str(raw_text_val) if raw_text_val is not None else None

        inferred_source = (
            CaptureSource.VOICE
            if audio_bytes is not None
            else (
                CaptureSource.IMAGE
                if image_bytes is not None
                else (
                    CaptureSource.VOICE
                    if (default_voice_endpoint and not (text_str and text_str.strip()))
                    else CaptureSource.TEXT
                )
            )
        )
        source = _parse_capture_source(form.get("source"), fallback=inferred_source)
        route_mode = _parse_route_mode(
            form.get("routeMode") or form.get("route_mode"),
            default=RouteMode.GLOBAL,
        )
        active_person_id = _parse_optional_uuid_form(
            form.get("activePersonId") or form.get("active_person_id"),
            field_name="activePersonId",
        )
        client_id = _parse_optional_uuid_form(form.get("id"), field_name="id")
        keep_orig = _parse_optional_bool(
            form.get("keepOriginalMedia") or form.get("keep_original_media")
        )
        form_sync = _parse_optional_bool(form.get("sync"))
        effective_sync = sync_query if sync_query is not None else form_sync

        return CaptureIngestCommand(
            scope=scope,
            source=source,
            route_mode=route_mode,
            active_person_id=active_person_id,
            text=text_str,
            audio_bytes=audio_bytes,
            image_bytes=image_bytes,
            keep_original_media=bool(keep_orig),
            sync=effective_sync,
            client_capture_id=client_id,
            idem_key=idem_key,
            route_path=request.url.path,
        )

    raw_body = await request.body()
    if not raw_body or not raw_body.strip():
        raise ValidationFailedError("Request body must not be empty")
    try:
        dto = CaptureCreateJsonRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        raise ValidationFailedError("Invalid capture JSON payload") from exc

    audio_b = decode_optional_base64(dto.audio_base64, field_name="audioBase64")
    image_b = decode_optional_base64(dto.image_base64, field_name="imageBase64")

    source = dto.source
    if audio_b is not None and dto.source is CaptureSource.TEXT and not dto.text:
        source = CaptureSource.VOICE
    elif image_b is not None and dto.source is CaptureSource.TEXT and not dto.text:
        source = CaptureSource.IMAGE
    elif default_voice_endpoint and audio_b is None and not (dto.text and dto.text.strip()):
        source = CaptureSource.VOICE

    effective_sync = sync_query if sync_query is not None else dto.sync
    return CaptureIngestCommand(
        scope=scope,
        source=source,
        route_mode=dto.route_mode,
        active_person_id=dto.active_person_id,
        text=dto.text,
        audio_bytes=audio_b,
        image_bytes=image_b,
        keep_original_media=dto.keep_original_media,
        sync=effective_sync,
        client_capture_id=dto.id,
        idem_key=idem_key,
        route_path=request.url.path,
    )


def _optional_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str | None:
    if idempotency_key is None or not idempotency_key.strip():
        return None
    cleaned = idempotency_key.strip()
    if "\x00" in cleaned:
        raise ValidationFailedError("Null bytes are not permitted in Idempotency-Key")
    if len(cleaned) > 128:
        raise ValidationFailedError("Idempotency-Key exceeds 128 characters")
    return cleaned


@router.post("/captures", response_model=CaptureResponse, status_code=201)
async def create_capture(
    request: Request,
    sync: bool | None = Query(default=None),
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cmd = await _parse_capture_ingest_request(
        request,
        scope=scope,
        idem_key=idem_key,
        sync_query=sync,
        default_voice_endpoint=False,
    )
    svc = CapturePipelineService(container)
    created, hit, status_code = await svc.submit_capture(cmd)
    if hit is not None:
        return JSONResponse(status_code=hit.status_code, content=hit.response_body)
    assert created is not None
    return JSONResponse(
        status_code=status_code,
        content=created.model_dump(mode="json", by_alias=True),
    )


@router.post("/voice/ingest", response_model=VoiceIngestResponse, status_code=200)
async def ingest_voice(
    request: Request,
    idem_key: str | None = Depends(_optional_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cmd = await _parse_capture_ingest_request(
        request,
        scope=scope,
        idem_key=idem_key,
        sync_query=True,
        default_voice_endpoint=True,
    )
    svc = CapturePipelineService(container)
    resp, hit = await svc.ingest_voice_v10(cmd)
    if hit is not None:
        return JSONResponse(status_code=hit.status_code, content=hit.response_body)
    assert resp is not None
    return JSONResponse(
        status_code=200,
        content=resp.model_dump(mode="json", by_alias=True),
    )


@router.get("/captures/{capture_id}", response_model=CaptureResponse)
async def get_capture(
    capture_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CaptureResponse:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    return await svc.get_capture(scope=scope, capture_id=cid)


@router.get("/captures/{capture_id}/events")
async def stream_capture_events(
    capture_id: str,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> EventSourceResponse:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    # Pre-verify capture exists and belongs to tenant before opening SSE stream
    await svc.get_capture(scope=scope, capture_id=cid)
    return EventSourceResponse(svc.stream_capture_events(scope=scope, capture_id=cid))


@router.post("/captures/{capture_id}:undo", response_model=CaptureUndoResponse)
async def undo_capture(
    capture_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    resp, hit = await svc.undo_capture(
        scope=scope,
        capture_id=cid,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        return JSONResponse(status_code=hit.status_code, content=hit.response_body)
    assert resp is not None
    return JSONResponse(
        status_code=200,
        content=resp.model_dump(mode="json", by_alias=True),
    )
