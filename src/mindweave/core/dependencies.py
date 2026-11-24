"""FastAPI Dependency Injection container & common security/header extractors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock, SystemClock
from mindweave.core.config import Settings
from mindweave.core.crypto import EnvelopeCipher, EnvKekProvider
from mindweave.core.db import DatabaseManager
from mindweave.core.errors import (
    BadRequestError,
    ReauthRequiredError,
    UnauthorizedError,
    ValidationFailedError,
)
from mindweave.core.idempotency import IdempotencyService
from mindweave.core.orm import UserKeyRow, UserRow
from mindweave.core.ratelimit import QuotaService, TokenBucketRateLimiter, UserConcurrencyLimiter
from mindweave.core.security import TokenService
from mindweave.core.storage import LocalEncryptedBlobStore, ObjectStoragePort
from mindweave.shared.events import EventBus
from mindweave.shared.scopes import UserScope

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class AppContainer:
    """Application-wide service container stored on `app.state.container`."""

    settings: Settings
    db: DatabaseManager
    clock: Clock
    cipher: EnvelopeCipher
    tokens: TokenService
    storage: ObjectStoragePort
    idempotency: IdempotencyService
    rate_limiter: TokenBucketRateLimiter
    quotas: QuotaService
    events: EventBus
    # Optional lazy/extensible slots populated during app wiring (e.g. gateway, worker, push)
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, *, clock: Clock | None = None) -> AppContainer:
        clk = clock or SystemClock()
        db = DatabaseManager(
            app_url=settings.app_database_url,
            system_url=settings.system_database_url,
            echo=settings.debug,
        )
        kek = EnvKekProvider(kek_hex=settings.kek_hex, kek_id=settings.kek_id)
        cipher = EnvelopeCipher(
            kek,
            cache_ttl_seconds=settings.dek_cache_ttl_seconds,
            cache_max_entries=settings.dek_cache_max_entries,
        )
        tokens = TokenService(
            secret=settings.jwt_secret,
            issuer=settings.jwt_issuer,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            reauth_ttl_seconds=settings.reauth_token_ttl_seconds,
        )
        storage = LocalEncryptedBlobStore(settings.blob_storage_dir, cipher)
        idempotency = IdempotencyService(clk, cipher=cipher)
        rate_limiter = TokenBucketRateLimiter()
        quotas = QuotaService(
            clk,
            db=db,
            daily_chat_quota=settings.daily_chat_quota,
            daily_voice_minutes_quota=settings.daily_voice_minutes_quota,
            daily_capture_quota=settings.daily_capture_quota,
            daily_image_quota=settings.daily_image_quota,
            daily_ai_cost_micros_cny_per_user=settings.daily_ai_cost_micros_cny_per_user,
            daily_global_ai_cost_micros_cny=settings.daily_global_ai_cost_micros_cny,
            global_budget_cache_ttl_seconds=0.0 if settings.environment == "test" else 30.0,
        )
        events = EventBus()
        return cls(
            settings=settings,
            db=db,
            clock=clk,
            cipher=cipher,
            tokens=tokens,
            storage=storage,
            idempotency=idempotency,
            rate_limiter=rate_limiter,
            quotas=quotas,
            events=events,
            extras={"concurrency_limiter": UserConcurrencyLimiter(max_concurrent_per_user=2)},
        )

    async def load_user_dek(self, session: AsyncSession, scope: UserScope) -> bytes:
        key_row = await session.get(UserKeyRow, scope.user_id)
        if key_row is None:
            raise UnauthorizedError("User encryption key not provisioned")
        return self.cipher.load_dek(
            user_id=scope.user_id,
            wrapped_dek=key_row.wrapped_dek,
            kek_id=key_row.kek_id,
            destroyed_at=key_row.destroyed_at,
        )


def get_container(request: Request) -> AppContainer:
    container: AppContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("AppContainer not initialized on app.state")
    return container


async def get_current_user_scope(
    container: AppContainer = Depends(get_container),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> UserScope:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise UnauthorizedError("Missing Bearer authentication token")
    claims = container.tokens.verify_access_token(creds.credentials, now=container.clock.now())
    async with container.db.system_session() as sys_sess:
        status = await sys_sess.scalar(
            select(UserRow.status).where(
                UserRow.id == claims.user_id,
                UserRow.deleted_at.is_(None),
            )
        )
        if status != "active":
            raise UnauthorizedError("Account is inactive or has been deleted")
    return UserScope(user_id=claims.user_id)


async def get_media_user_scope(
    request: Request,
    container: AppContainer = Depends(get_container),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    uid: str | None = Query(default=None),
    exp: int | None = Query(default=None),
    sig: str | None = Query(default=None),
) -> UserScope:
    """Authenticate media reads via Bearer JWT OR short-lived HMAC-signed URL (`uid`, `exp`, `sig`)."""
    if creds is not None and creds.scheme.lower() == "bearer" and creds.credentials:
        return await get_current_user_scope(container=container, creds=creds)
    if uid is not None and exp is not None and sig is not None:
        user_id = container.tokens.verify_media_signature(
            path=request.url.path,
            uid=uid,
            exp=exp,
            sig=sig,
            now=container.clock.now(),
        )
        async with container.db.system_session() as sys_sess:
            status = await sys_sess.scalar(
                select(UserRow.status).where(
                    UserRow.id == user_id,
                    UserRow.deleted_at.is_(None),
                )
            )
            if status != "active":
                raise UnauthorizedError("Account is inactive or has been deleted")
        return UserScope(user_id=user_id)
    raise UnauthorizedError("Missing Bearer authentication token or signed media ticket")


async def require_step_up_reauth(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
    x_reauth_token: str | None = Header(default=None, alias="X-Reauth-Token"),
) -> UserScope:
    if not x_reauth_token:
        raise ReauthRequiredError("X-Reauth-Token header is required for this sensitive operation")
    container.tokens.verify_reauth_token(
        x_reauth_token,
        expected_user_id=scope.user_id,
        now=container.clock.now(),
    )
    return scope


def require_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise BadRequestError("Idempotency-Key header is required for mutating requests")
    cleaned = idempotency_key.strip()
    if len(cleaned) > 128 or "\x00" in cleaned:
        raise ValidationFailedError("Idempotency-Key must be 1..128 valid characters")
    return cleaned


def parse_if_match_version(
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> int | None:
    if if_match is None or not if_match.strip():
        return None
    raw = if_match.strip().removeprefix("W/").strip('"').strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValidationFailedError("If-Match header must be a positive integer version")
    return int(raw)


def parse_uuid_param(value: str, *, field_name: str = "id") -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationFailedError(f"Invalid UUID for {field_name}") from exc
