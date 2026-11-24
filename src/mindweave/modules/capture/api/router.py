"""FastAPI router for Multimodal Capture, Recent Captures Manager (C1), Range Audio, V10 `/voice/ingest`, SSE Events, and Undo."""

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
    get_media_user_scope,
    parse_uuid_param,
    require_idempotency_key,
)
from mindweave.core.dto import CursorPage
from mindweave.core.errors import PayloadTooLargeError, ValidationFailedError
from mindweave.modules.capture.api.schemas import VoiceTranscribeResponse
from mindweave.modules.capture.application.dto import (
    CaptureAddSlipRequest,
    CaptureAddSlipResponse,
    CaptureCreateJsonRequest,
    CaptureHistoryItemDto,
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


def _build_range_media_response(
    plaintext: bytes,
    content_type: str,
    range_header: str | None,
) -> Response:
    """RFC 9110 `Accept-Ranges: bytes` + `206 Partial Content` / `416` media response for iOS WKWebView `<audio>`."""
    total = len(plaintext)
    base_headers = {
        "Cache-Control": "private, no-store",
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
    }
    if not range_header or not range_header.strip().lower().startswith("bytes="):
        return Response(
            content=plaintext,
            status_code=200,
            media_type=content_type,
            headers={**base_headers, "Content-Length": str(total)},
        )
    spec = range_header.strip()[6:].split(",", 1)[0].strip()
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return Response(
            status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{total}"},
        )
    try:
        if not start_s and end_s:
            suffix = int(end_s)
            if suffix <= 0 or total == 0:
                return Response(
                    status_code=416,
                    headers={**base_headers, "Content-Range": f"bytes */{total}"},
                )
            start = max(0, total - min(total, suffix))
            end = total - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return Response(
            status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{total}"},
        )

    if total == 0 or start < 0 or start >= total or end < start:
        return Response(
            status_code=416,
            headers={**base_headers, "Content-Range": f"bytes */{total}"},
        )
    end = min(end, total - 1)
    chunk = plaintext[start : end + 1]
    return Response(
        content=chunk,
        status_code=206,
        media_type=content_type,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(chunk)),
        },
    )


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


def _parse_optional_int(raw: object, *, field_name: str, min_val: int = 0, max_val: int = 600_000) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return None
    try:
        val = int(s)
    except ValueError as exc:
        raise ValidationFailedError(f"Invalid integer value for {field_name}") from exc
    if val < min_val or val > max_val:
        raise ValidationFailedError(f"{field_name} out of allowed range [{min_val}, {max_val}]")
    return val


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
    if s.lower() == "scoped":  # PWA alias for the open person page
        return RouteMode.CURRENT
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
    max_audio_bytes: int,
    max_image_bytes: int,
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

        max_audio_read = max_audio_bytes + 1
        for audio_field in ("audio", "voice", "file"):
            up = form.get(audio_field)
            if isinstance(up, UploadFile):
                data = await up.read(max_audio_read)
                if len(data) > max_audio_bytes:
                    raise PayloadTooLargeError(
                        f"Audio file size exceeds maximum allowed ({max_audio_bytes} bytes)"
                    )
                if data:
                    audio_bytes = data
                    break

        max_image_read = max_image_bytes + 1
        for image_field in ("image", "screenshot", "photo"):
            up_img = form.get(image_field)
            if isinstance(up_img, UploadFile):
                data_img = await up_img.read(max_image_read)
                if len(data_img) > max_image_bytes:
                    raise PayloadTooLargeError(
                        f"Image file size exceeds maximum allowed ({max_image_bytes} bytes)"
                    )
                if data_img:
                    image_bytes = data_img
                    break

        raw_text_val = form.get("text") or form.get("transcript")
        text_str = str(raw_text_val) if raw_text_val is not None else None
        raw_client_tr = form.get("clientTranscript") or form.get("client_transcript")
        client_transcript_str = (
            str(raw_client_tr).strip() if isinstance(raw_client_tr, str) else None
        ) or None
        raw_engine = form.get("speechEngine") or form.get("speech_engine")
        speech_engine_str = (
            str(raw_engine).strip()[:64] if isinstance(raw_engine, str) else None
        ) or None
        audio_duration_ms = _parse_optional_int(
            form.get("audioDurationMs") or form.get("audio_duration_ms"),
            field_name="audioDurationMs",
        )

        inferred_source = (
            CaptureSource.VOICE
            if (audio_bytes is not None or client_transcript_str is not None or default_voice_endpoint)
            else (CaptureSource.IMAGE if image_bytes is not None else CaptureSource.TEXT)
        )
        # Preserve text-only submission on `/v1/voice/ingest` when explicitly sending `text` without audio/clientTranscript
        if (
            default_voice_endpoint
            and audio_bytes is None
            and client_transcript_str is None
            and text_str
            and text_str.strip()
        ):
            inferred_source = CaptureSource.TEXT
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
            client_transcript=client_transcript_str,
            speech_engine=speech_engine_str,
            audio_duration_ms=audio_duration_ms,
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
    client_tr = dto.client_transcript.strip() if dto.client_transcript else None

    source = dto.source
    if (audio_b is not None or client_tr is not None) and dto.source is CaptureSource.TEXT and not dto.text:
        source = CaptureSource.VOICE
    elif image_b is not None and dto.source is CaptureSource.TEXT and not dto.text:
        source = CaptureSource.IMAGE
    elif default_voice_endpoint and audio_b is None and (client_tr is not None or not (dto.text and dto.text.strip())):
        source = CaptureSource.VOICE

    effective_sync = sync_query if sync_query is not None else dto.sync
    return CaptureIngestCommand(
        scope=scope,
        source=source,
        route_mode=dto.route_mode,
        active_person_id=dto.active_person_id,
        text=dto.text,
        client_transcript=client_tr,
        speech_engine=dto.speech_engine,
        audio_duration_ms=dto.audio_duration_ms,
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


@router.get(
    "/captures",
    response_model=CursorPage[CaptureHistoryItemDto],
    response_model_by_alias=True,
    summary="List recent captures with live-hydrated slips and signed media URLs",
)
async def list_captures(
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None),
    source: CaptureSource | None = Query(default=None),
    include_reverted: bool = Query(default=False, alias="includeReverted"),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> CursorPage[CaptureHistoryItemDto]:
    svc = CapturePipelineService(container)
    return await svc.list_captures(
        scope=scope,
        limit=limit,
        cursor=cursor,
        source=source,
        include_reverted=include_reverted,
    )


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
        max_audio_bytes=container.settings.max_audio_bytes,
        max_image_bytes=container.settings.max_image_bytes,
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
        max_audio_bytes=container.settings.max_audio_bytes,
        max_image_bytes=container.settings.max_image_bytes,
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


@router.post(
    "/voice/transcribe",
    response_model=VoiceTranscribeResponse,
    response_model_by_alias=True,
    status_code=200,
    summary="Transcribe conversational voice audio without creating records or facts",
)
async def transcribe_voice(
    request: Request,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> VoiceTranscribeResponse:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise ValidationFailedError("Voice transcription requires multipart/form-data upload")

    form = await request.form()
    _reject_null_bytes_in_form(form)

    audio_bytes: bytes | None = None
    max_audio_bytes = container.settings.max_audio_bytes
    max_read = max_audio_bytes + 1
    for audio_field in ("audio", "voice", "file"):
        upload = form.get(audio_field)
        if isinstance(upload, UploadFile):
            data = await upload.read(max_read)
            if len(data) > max_audio_bytes:
                raise PayloadTooLargeError(
                    f"Audio file size exceeds maximum allowed ({max_audio_bytes} bytes)"
                )
            if data:
                audio_bytes = data
                break

    if not audio_bytes:
        raise ValidationFailedError("Voice transcription requires a non-empty audio file")

    active_person_id = _parse_optional_uuid_form(
        form.get("activePersonId") or form.get("active_person_id"),
        field_name="activePersonId",
    )

    svc = CapturePipelineService(container)
    result = await svc.transcribe_only(
        scope=scope,
        audio_bytes=audio_bytes,
        active_person_id=active_person_id,
    )
    return VoiceTranscribeResponse(
        transcript=result.transcript,
        corrected=result.corrected,
        raw_transcript=result.raw_transcript,
        duration_ms=result.duration_ms,
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


@router.get(
    "/captures/{capture_id}/audio",
    summary="Stream decrypted voice capture audio with RFC 9110 Range (206 Partial Content) support",
)
async def get_capture_audio(
    capture_id: str,
    request: Request,
    scope: UserScope = Depends(get_media_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    plaintext, content_type = await svc.get_capture_audio(scope=scope, capture_id=cid)
    return _build_range_media_response(
        plaintext,
        content_type,
        request.headers.get("range"),
    )


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


@router.post(
    "/captures/{capture_id}/slips",
    response_model=CaptureAddSlipResponse,
    status_code=201,
    summary="Add a missed slip to an existing capture batch",
)
async def add_slip_to_capture(
    capture_id: str,
    req: CaptureAddSlipRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    created, hit = await svc.add_slip_to_capture(
        scope=scope,
        capture_id=cid,
        req=req,
        idem_key=idem_key,
        route_path=request.url.path,
    )
    if hit is not None:
        return JSONResponse(status_code=hit.status_code, content=hit.response_body)
    assert created is not None
    return JSONResponse(
        status_code=201,
        content=created.model_dump(mode="json", by_alias=True),
        headers={"ETag": f'"{created.version}"'},
    )


@router.delete(
    "/captures/{capture_id}",
    response_model=CaptureUndoResponse,
    status_code=200,
    summary="Delete an entire capture batch from Recent Captures (no 5s/30s window restriction)",
)
async def delete_capture(
    capture_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    cid = parse_uuid_param(capture_id, field_name="capture_id")
    svc = CapturePipelineService(container)
    resp, hit = await svc.delete_capture(
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
