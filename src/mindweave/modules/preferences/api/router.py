"""FastAPI router for User Preferences and Encrypted Custom Wallpapers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    parse_if_match_version,
    require_idempotency_key,
)
from mindweave.modules.preferences.api.schemas import (
    PreferencesResponse,
    UpdatePreferencesRequest,
)
from mindweave.modules.preferences.application.service import PreferencesService
from mindweave.modules.preferences.domain.models import UserPreferences
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["preferences"])
_STREAM_CHUNK_BYTES = 64 * 1024


def _chunk_bytes(data: bytes) -> Iterator[bytes]:
    for offset in range(0, len(data), _STREAM_CHUNK_BYTES):
        yield data[offset : offset + _STREAM_CHUNK_BYTES]


@router.get("/me/preferences", response_model=PreferencesResponse)
async def get_my_preferences(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> PreferencesResponse:
    service = PreferencesService(container)
    prefs = await service.get_preferences(scope)
    return PreferencesResponse.from_domain(prefs)


@router.patch("/me/preferences", response_model=PreferencesResponse)
async def patch_my_preferences(
    payload: UpdatePreferencesRequest,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    expected_version: int | None = Depends(parse_if_match_version),
    container: AppContainer = Depends(get_container),
) -> PreferencesResponse:
    def _serialize(p: UserPreferences) -> dict[str, Any]:
        return PreferencesResponse.from_domain(p).model_dump(by_alias=True, mode="json")

    service = PreferencesService(container)
    saved, hit = await service.update_preferences(
        scope,
        patch=payload.to_domain_patch(),
        idem_key=idem_key,
        expected_version=expected_version,
        raw_patch_payload=payload.model_dump(by_alias=True, mode="json", exclude_unset=True),
        serializer=_serialize,
    )
    if hit is not None:
        return PreferencesResponse.model_validate(hit.response_body)
    assert saved is not None
    return PreferencesResponse.from_domain(saved)


@router.post("/me/wallpaper", response_model=PreferencesResponse)
async def upload_my_wallpaper(
    file: UploadFile = File(...),
    scope: UserScope = Depends(get_current_user_scope),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    container: AppContainer = Depends(get_container),
) -> PreferencesResponse:
    def _serialize(p: UserPreferences) -> dict[str, Any]:
        return PreferencesResponse.from_domain(p).model_dump(by_alias=True, mode="json")

    # Read at most max_image_bytes + 1 to prevent unbounded memory allocation on oversized uploads
    raw_bytes = await file.read(container.settings.max_image_bytes + 1)
    await file.close()

    service = PreferencesService(container)
    saved, hit = await service.upload_wallpaper(
        scope,
        raw_bytes=raw_bytes,
        idem_key=idempotency_key,
        serializer=_serialize,
    )
    if hit is not None:
        return PreferencesResponse.model_validate(hit.response_body)
    assert saved is not None
    return PreferencesResponse.from_domain(saved)


@router.get("/me/wallpaper")
async def get_my_wallpaper(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    service = PreferencesService(container)
    plaintext, content_type = await service.get_wallpaper(scope)
    return StreamingResponse(
        _chunk_bytes(plaintext),
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(plaintext)),
        },
    )


@router.delete(
    "/me/wallpaper",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_my_wallpaper(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> Response:
    service = PreferencesService(container)
    await service.delete_wallpaper(scope)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
