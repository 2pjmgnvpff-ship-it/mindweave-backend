"""Application use-case service for User Preferences & Encrypted Custom Wallpapers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import contains_forbidden_dev_jargon
from mindweave.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    ValidationFailedError,
    VersionMismatchError,
)
from mindweave.core.idempotency import IdempotencyHit, hash_request_payload
from mindweave.core.ids import new_id
from mindweave.modules.preferences.domain.models import (
    PreferencesPatch,
    PreferencesValidationError,
    PreferencesVersionConflictError,
    UserPreferences,
    apply_preferences_patch,
)
from mindweave.modules.preferences.infra.image_sanitizer import sanitize_wallpaper_image
from mindweave.modules.preferences.infra.repositories import PreferencesRepository
from mindweave.shared.scopes import UserScope


class PreferencesService:
    """Manages user preferences with optimistic locking, idempotency, and EXIF-free encrypted wallpapers."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._repo = PreferencesRepository()

    async def get_or_create_in_session(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> UserPreferences:
        return await self._repo.get_or_create(
            session,
            scope,
            default_timezone=self._container.settings.default_timezone,
            now=self._container.clock.now(),
        )

    async def get_preferences(self, scope: UserScope) -> UserPreferences:
        async with self._container.db.user_session(scope) as session:
            return await self.get_or_create_in_session(session, scope)

    async def update_preferences(
        self,
        scope: UserScope,
        *,
        patch: PreferencesPatch,
        idem_key: str,
        expected_version: int | None,
        raw_patch_payload: dict[str, Any],
        serializer: Callable[[UserPreferences], dict[str, Any]],
    ) -> tuple[UserPreferences | None, IdempotencyHit | None]:
        if patch.wallpaper_preset is not None:
            jargon = contains_forbidden_dev_jargon(patch.wallpaper_preset)
            if jargon is not None:
                raise ValidationFailedError("wallpaperPreset contains disallowed copy")

        req_hash = hash_request_payload(
            {
                "patch": raw_patch_payload,
                "ifMatch": expected_version,
            }
        )
        now = self._container.clock.now()

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="PATCH",
                path="/me/preferences",
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            current = await self._repo.get_or_create(
                session,
                scope,
                default_timezone=self._container.settings.default_timezone,
                now=now,
                for_update=True,
            )
            try:
                updated = apply_preferences_patch(
                    current,
                    patch,
                    expected_version=expected_version,
                    now=now,
                )
            except PreferencesVersionConflictError as exc:
                raise VersionMismatchError(
                    str(exc),
                    extensions={
                        "expectedVersion": exc.expected_version,
                        "currentVersion": exc.current_version,
                    },
                ) from exc
            except PreferencesValidationError as exc:
                raise ValidationFailedError(str(exc)) from exc

            saved = await self._repo.save(session, scope, updated)
            assert idem_row is not None
            await self._container.idempotency.complete(
                session,
                idem_row,
                status_code=200,
                response_body=serializer(saved),
            )
            return saved, None

    async def upload_wallpaper(
        self,
        scope: UserScope,
        *,
        raw_bytes: bytes,
        idem_key: str | None = None,
        serializer: Callable[[UserPreferences], dict[str, Any]] | None = None,
    ) -> tuple[UserPreferences | None, IdempotencyHit | None]:
        max_bytes = self._container.settings.max_image_bytes
        if not raw_bytes:
            raise ValidationFailedError("Uploaded wallpaper image cannot be empty")
        if len(raw_bytes) > max_bytes:
            raise PayloadTooLargeError(
                f"Wallpaper image exceeds maximum allowed size ({max_bytes} bytes)"
            )

        cleaned_idem: str | None = None
        if idem_key is not None and idem_key.strip():
            cleaned_idem = idem_key.strip()
            if len(cleaned_idem) > 128:
                raise ValidationFailedError("Idempotency-Key exceeds 128 characters")

        sanitized = await asyncio.to_thread(
            sanitize_wallpaper_image,
            raw_bytes,
            max_bytes=max_bytes,
        )

        new_object_key = f"{scope.user_id}/wallpaper/{new_id()}"
        old_object_key: str | None = None
        blob_written = False
        now = self._container.clock.now()

        try:
            async with self._container.db.user_session(scope) as session:
                idem_row = None
                if cleaned_idem is not None:
                    idem_row, hit = await self._container.idempotency.claim_or_replay(
                        session,
                        scope=scope,
                        idem_key=cleaned_idem,
                        method="POST",
                        path="/me/wallpaper",
                        request_hash=hash_request_payload(raw_bytes),
                    )
                    if hit is not None:
                        return None, hit

                dek = await self._container.load_user_dek(session, scope)
                current = await self._repo.get_or_create(
                    session,
                    scope,
                    default_timezone=self._container.settings.default_timezone,
                    now=now,
                    for_update=True,
                )
                old_object_key = current.wallpaper_object_key

                await self._container.storage.put_encrypted(
                    user_id=scope.user_id,
                    dek=dek,
                    object_key=new_object_key,
                    plaintext=sanitized.data,
                    content_type=sanitized.content_type,
                )
                blob_written = True

                updated = replace(
                    current,
                    wallpaper_object_key=new_object_key,
                    version=current.version + 1,
                    updated_at=now,
                )
                saved = await self._repo.save(session, scope, updated)

                if idem_row is not None and serializer is not None:
                    await self._container.idempotency.complete(
                        session,
                        idem_row,
                        status_code=200,
                        response_body=serializer(saved),
                    )
        except Exception:
            if blob_written:
                await self._container.storage.delete_object(object_key=new_object_key)
            raise

        if old_object_key is not None and old_object_key != new_object_key:
            await self._container.storage.delete_object(object_key=old_object_key)

        return saved, None

    async def get_wallpaper(self, scope: UserScope) -> tuple[bytes, str]:
        async with self._container.db.user_session(scope) as session:
            prefs = await self._repo.get_optional(session, scope)
            if prefs is None or not prefs.wallpaper_object_key:
                raise NotFoundError("Custom wallpaper not found")
            dek = await self._container.load_user_dek(session, scope)
            object_key = prefs.wallpaper_object_key

        return await self._container.storage.get_decrypted(
            user_id=scope.user_id,
            dek=dek,
            object_key=object_key,
        )

    async def delete_wallpaper(self, scope: UserScope) -> None:
        now = self._container.clock.now()
        old_object_key: str | None = None

        async with self._container.db.user_session(scope) as session:
            current = await self._repo.get_optional(session, scope, for_update=True)
            if current is not None and current.wallpaper_object_key is not None:
                old_object_key = current.wallpaper_object_key
                updated = replace(
                    current,
                    wallpaper_object_key=None,
                    version=current.version + 1,
                    updated_at=now,
                )
                await self._repo.save(session, scope, updated)

        if old_object_key is not None:
            await self._container.storage.delete_object(object_key=old_object_key)
