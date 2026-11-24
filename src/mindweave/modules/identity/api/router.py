"""FastAPI router for Identity, Auth, Step-Up Reauth, and PIPL Consents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    require_idempotency_key,
)
from mindweave.modules.identity.api.schemas import (
    AppleAuthRequest,
    AppleReauthRequest,
    AuthTokensResponse,
    ConsentItemResponse,
    ConsentsListResponse,
    EmailOtpChallengeResponse,
    EmailOtpRequest,
    EmailOtpVerifyRequest,
    LogoutRequest,
    LogoutResponse,
    ReauthTokenResponse,
    RecordConsentRequest,
    RefreshTokenRequest,
    RefreshTokensResponse,
)
from mindweave.modules.identity.application.service import (
    AuthSessionResult,
    IdentityService,
)
from mindweave.modules.identity.domain.models import ConsentRecord
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["identity"])
_optional_bearer = HTTPBearer(auto_error=False)


def _trusted_client_ip(request: Request, *, trusted_proxy_hops: int = 0) -> str | None:
    """Extract client IP honoring configured trusted_proxy_hops (0=direct, 1=Cloud Run, 2=GCLB)."""
    if trusted_proxy_hops > 0:
        xff = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-trusted_proxy_hops] if len(parts) >= trusted_proxy_hops else parts[0]
    return request.client.host if request.client is not None else None


def _client_ip(request: Request, container: AppContainer | None = None) -> str | None:
    hops = container.settings.trusted_proxy_hops if container is not None else 0
    return _trusted_client_ip(request, trusted_proxy_hops=hops)


@router.post("/auth/apple", response_model=AuthTokensResponse)
async def auth_apple(
    payload: AppleAuthRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> AuthTokensResponse:
    service = IdentityService(container)
    result = await service.authenticate_apple(
        identity_token=payload.identity_token,
        authorization_code=payload.authorization_code,
        display_email=payload.display_email,
        client_ip=_client_ip(request, container),
    )
    return AuthTokensResponse(
        user_id=result.user_id,
        is_new_user=result.is_new_user,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


async def _handle_request_email_otp(
    payload: EmailOtpRequest,
    request: Request,
    container: AppContainer,
) -> EmailOtpChallengeResponse:
    service = IdentityService(container)
    result = await service.request_email_otp(
        email=payload.email,
        purpose=payload.purpose,
        client_ip=_client_ip(request, container),
    )
    return EmailOtpChallengeResponse(
        challenge_id=result.challenge_id,
        expires_in=result.expires_in,
        delivered=result.delivered,
        delivery_status=result.delivery_status,
        provider=result.provider,
        cooldown_seconds=result.cooldown_seconds,
        dev_otp_code=result.dev_otp_code,
    )


@router.post(
    "/auth/email/otp:request",
    response_model=EmailOtpChallengeResponse,
    response_model_exclude_none=True,
)
async def request_email_otp(
    payload: EmailOtpRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> EmailOtpChallengeResponse:
    return await _handle_request_email_otp(payload, request, container)


@router.post(
    "/auth/email/request-otp",
    response_model=EmailOtpChallengeResponse,
    response_model_exclude_none=True,
    include_in_schema=False,
)
async def request_email_otp_alias(
    payload: EmailOtpRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> EmailOtpChallengeResponse:
    return await _handle_request_email_otp(payload, request, container)


@router.post(
    "/auth/email/otp:verify",
    response_model=AuthTokensResponse | ReauthTokenResponse,
)
async def verify_email_otp(
    payload: EmailOtpVerifyRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> AuthTokensResponse | ReauthTokenResponse:
    authenticated_scope: UserScope | None = None
    if creds is not None:
        authenticated_scope = await get_current_user_scope(container=container, creds=creds)

    service = IdentityService(container)
    result = await service.verify_email_otp(
        challenge_id=payload.challenge_id,
        email=payload.email,
        code=payload.code,
        authenticated_scope=authenticated_scope,
        client_ip=_client_ip(request, container),
    )
    if isinstance(result, AuthSessionResult):
        return AuthTokensResponse(
            user_id=result.user_id,
            is_new_user=result.is_new_user,
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            expires_in=result.expires_in,
        )
    return ReauthTokenResponse(
        reauth_token=result.reauth_token,
        expires_in=result.expires_in,
    )


@router.post("/auth/reauth/apple", response_model=ReauthTokenResponse)
async def reauth_apple(
    payload: AppleReauthRequest,
    request: Request,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> ReauthTokenResponse:
    service = IdentityService(container)
    result = await service.reauth_apple(
        scope,
        identity_token=payload.identity_token,
        client_ip=_client_ip(request, container),
    )
    return ReauthTokenResponse(
        reauth_token=result.reauth_token,
        expires_in=result.expires_in,
    )


@router.post("/auth/refresh", response_model=RefreshTokensResponse)
async def refresh_tokens(
    payload: RefreshTokenRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> RefreshTokensResponse:
    service = IdentityService(container)
    result = await service.refresh_session(
        refresh_token=payload.refresh_token,
        client_ip=_client_ip(request, container),
    )
    return RefreshTokensResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    payload: LogoutRequest | None = Body(default=None),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> LogoutResponse:
    service = IdentityService(container)
    await service.logout(
        scope,
        refresh_token=payload.refresh_token if payload is not None else None,
        client_ip=_client_ip(request, container),
    )
    return LogoutResponse(ok=True)


@router.get("/me/consents", response_model=ConsentsListResponse)
async def list_my_consents(
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> ConsentsListResponse:
    service = IdentityService(container)
    records = await service.list_consents(scope)
    return ConsentsListResponse(items=[ConsentItemResponse.from_domain(r) for r in records])


@router.post("/me/consents", response_model=ConsentItemResponse)
async def record_my_consent(
    payload: RecordConsentRequest,
    request: Request,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> ConsentItemResponse:
    def _serialize(rec: ConsentRecord) -> dict[str, Any]:
        return ConsentItemResponse.from_domain(rec).model_dump(by_alias=True, mode="json")

    service = IdentityService(container)
    record, hit = await service.record_consent(
        scope,
        idem_key=idem_key,
        consent_kind=payload.consent_kind,
        policy_version=payload.policy_version,
        granted=payload.granted,
        serializer=_serialize,
        client_ip=_client_ip(request, container),
    )
    if hit is not None:
        return ConsentItemResponse.model_validate(hit.response_body)
    assert record is not None
    return ConsentItemResponse.from_domain(record)
