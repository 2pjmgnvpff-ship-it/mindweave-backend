"""FastAPI router for Cupid Companion Chat (REST & SSE, mounted without `/v1` prefix)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    parse_uuid_param,
    require_idempotency_key,
)
from mindweave.modules.companion.api.dto import (
    ChatMessagePage,
    ChatTurnResponse,
    ClearChatHistoryResponse,
    SendChatMessageRequest,
    StreamChatRequest,
)
from mindweave.modules.companion.application.service import CompanionService
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["companion"])

_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _should_stream(stream_param: bool | None, accept_header: str | None) -> bool:
    if stream_param is not None:
        return stream_param
    accept = (accept_header or "").lower()
    if "text/event-stream" in accept:
        return True
    if "application/json" in accept:
        return False
    return True


@router.get(
    "/persons/{person_id}/chat/messages",
    response_model=ChatMessagePage,
    summary="List cursor-paginated Cupid companion chat messages for a person",
)
async def list_person_chat_messages(
    person_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    order: Literal["asc", "desc"] = Query(default="asc"),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> ChatMessagePage:
    pid = parse_uuid_param(person_id, field_name="personId")
    svc = CompanionService(container)
    return await svc.list_messages(
        scope=scope.person(pid),
        cursor=cursor,
        limit=limit,
        order=order,
    )


@router.post(
    "/persons/{person_id}/chat/messages",
    response_model=ChatTurnResponse,
    summary="Send a Cupid companion message (SSE stream or JSON when stream=false)",
)
async def send_person_chat_message(
    person_id: str,
    body: SendChatMessageRequest,
    request: Request,
    stream: bool | None = Query(default=None),
    accept: str | None = Header(default=None, alias="Accept"),
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response | ChatTurnResponse:
    pid = parse_uuid_param(person_id, field_name="personId")
    person_scope = scope.person(pid)
    svc = CompanionService(container)

    if _should_stream(stream, accept):
        sse_iter = await svc.prepare_sse_stream(
            scope=person_scope,
            message=body.message,
            referenced_material_ids=body.referenced_material_ids,
            client_message_id=body.client_message_id,
            idem_key=idem_key,
            idem_path=str(request.url.path),
        )
        return StreamingResponse(
            sse_iter,
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return await svc.send_message_json(
        scope=person_scope,
        message=body.message,
        referenced_material_ids=body.referenced_material_ids,
        client_message_id=body.client_message_id,
        idem_key=idem_key,
        idem_path=str(request.url.path),
    )


@router.post(
    "/persons/{person_id}/chat/stream",
    summary="V10 frontend SSE compatibility endpoint for Cupid companion chat",
)
async def stream_person_chat_v10(
    person_id: str,
    body: StreamChatRequest,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    pid = parse_uuid_param(person_id, field_name="personId")
    svc = CompanionService(container)
    sse_iter = await svc.prepare_sse_stream(
        scope=scope.person(pid),
        message=body.message,
        referenced_material_ids=body.referenced_material_ids,
        client_message_id=body.client_message_id,
        idem_key=idem_key,
        idem_path=str(request.url.path),
    )
    return StreamingResponse(
        sse_iter,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.delete(
    "/persons/{person_id}/chat/messages",
    response_model=ClearChatHistoryResponse,
    summary="Clear conversation history and rolling summary for a person",
)
async def clear_person_chat_messages(
    person_id: str,
    request: Request,
    idem_key: str = Depends(require_idempotency_key),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> ClearChatHistoryResponse:
    pid = parse_uuid_param(person_id, field_name="personId")
    svc = CompanionService(container)
    return await svc.clear_history(
        scope=scope.person(pid),
        idem_key=idem_key,
        idem_path=str(request.url.path),
    )
