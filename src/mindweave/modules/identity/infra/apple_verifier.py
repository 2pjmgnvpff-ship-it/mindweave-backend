"""Apple Sign-In PyJWT JWKS verifier & zero-PII OTP sender adapters."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt

from mindweave.core.errors import UnauthorizedError
from mindweave.modules.identity.domain.models import (
    InvalidEmailError,
    OtpPurpose,
    VerifiedAppleIdentity,
    normalize_email,
)

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"
DEV_APPLE_PREFIX = "dev_apple:"


class AppleJwksTokenVerifier:
    """Verifies Apple Sign-In JWTs via Apple's JWKS endpoint, with gated dev-token support."""

    def __init__(
        self,
        *,
        allow_dev_mock: bool = False,
        jwks_url: str = APPLE_JWKS_URL,
        expected_issuer: str = APPLE_ISSUER,
        allowed_audiences: tuple[str, ...] | None = None,
        jwks_cache_ttl_seconds: int = 3600,
        request_timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._allow_dev_mock = allow_dev_mock
        self._jwks_url = jwks_url
        self._expected_issuer = expected_issuer
        self._allowed_audiences = allowed_audiences
        self._cache_ttl = jwks_cache_ttl_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._http_client = http_client

        self._lock = asyncio.Lock()
        self._cached_keys: dict[str, dict[str, Any]] = {}
        self._cache_expires_at: float = 0.0

    async def verify_identity_token(
        self,
        identity_token: str,
        *,
        now: datetime | None = None,
    ) -> VerifiedAppleIdentity:
        cleaned = identity_token.strip()
        if not cleaned:
            raise UnauthorizedError("Missing Apple identity token")

        if cleaned.startswith(DEV_APPLE_PREFIX):
            if not self._allow_dev_mock:
                raise UnauthorizedError("Dev mock Apple authentication is disabled")
            apple_sub = cleaned.removeprefix(DEV_APPLE_PREFIX).strip()
            if not apple_sub or len(apple_sub) > 256:
                raise UnauthorizedError("Invalid dev Apple identity token subject")
            return VerifiedAppleIdentity(subject=apple_sub, email=None, email_verified=False)

        try:
            header = jwt.get_unverified_header(cleaned)
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Malformed Apple identity token header") from exc

        alg = str(header.get("alg", "")).strip()
        kid = str(header.get("kid", "")).strip()
        if alg not in ("RS256", "ES256") or not kid:
            raise UnauthorizedError("Unsupported Apple identity token signing header")

        jwk_dict = await self._get_jwk_for_kid(kid)
        try:
            signing_key = jwt.PyJWK.from_dict(jwk_dict).key
            verify_time = now is None
            if self._allowed_audiences:
                claims: dict[str, Any] = jwt.decode(
                    cleaned,
                    signing_key,
                    algorithms=["RS256", "ES256"],
                    issuer=self._expected_issuer,
                    audience=list(self._allowed_audiences),
                    options={
                        "require": ["sub", "iss", "aud", "exp", "iat"],
                        "verify_exp": verify_time,
                        "verify_iat": verify_time,
                    },
                )
            else:
                claims = jwt.decode(
                    cleaned,
                    signing_key,
                    algorithms=["RS256", "ES256"],
                    issuer=self._expected_issuer,
                    options={
                        "verify_aud": False,
                        "require": ["sub", "iss", "exp", "iat"],
                        "verify_exp": verify_time,
                        "verify_iat": verify_time,
                    },
                )
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Invalid or expired Apple identity token") from exc
        except Exception as exc:
            raise UnauthorizedError("Failed to verify Apple identity token") from exc

        if now is not None:
            exp_ts = int(claims["exp"])
            iat_ts = int(claims["iat"])
            exp_dt = datetime.fromtimestamp(exp_ts, tz=UTC)
            iat_dt = datetime.fromtimestamp(iat_ts, tz=UTC)
            ref_now = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
            if ref_now >= exp_dt:
                raise UnauthorizedError("Apple identity token has expired")
            if iat_dt > ref_now.replace(microsecond=0) and (iat_dt - ref_now).total_seconds() > 60:
                raise UnauthorizedError("Apple identity token issued in the future")

        subject = str(claims.get("sub", "")).strip()
        if not subject or len(subject) > 256:
            raise UnauthorizedError("Apple identity token missing valid subject")

        email_val: str | None = None
        raw_email = claims.get("email")
        if isinstance(raw_email, str) and raw_email.strip():
            try:
                email_val = normalize_email(raw_email)
            except InvalidEmailError:
                email_val = None

        raw_verified = claims.get("email_verified")
        email_verified = raw_verified in (True, "true", "True")

        return VerifiedAppleIdentity(
            subject=subject,
            email=email_val,
            email_verified=email_verified,
        )

    async def _get_jwk_for_kid(self, kid: str) -> dict[str, Any]:
        mono_now = time.monotonic()
        async with self._lock:
            cache_valid = mono_now < self._cache_expires_at and bool(self._cached_keys)
            if cache_valid and kid in self._cached_keys:
                return self._cached_keys[kid]

            await self._refresh_jwks_locked()
            jwk = self._cached_keys.get(kid)
            if jwk is None:
                raise UnauthorizedError("Signing key not found in Apple JWKS")
            return jwk

    async def _refresh_jwks_locked(self) -> None:
        try:
            if self._http_client is not None:
                response = await self._http_client.get(self._jwks_url)
            else:
                async with httpx.AsyncClient(timeout=self._request_timeout_seconds) as client:
                    response = await client.get(self._jwks_url)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise UnauthorizedError("Unable to fetch Apple JWKS signing keys") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise UnauthorizedError("Malformed Apple JWKS response")

        parsed: dict[str, dict[str, Any]] = {}
        for item in payload["keys"]:
            if isinstance(item, dict) and isinstance(item.get("kid"), str) and item["kid"].strip():
                parsed[item["kid"].strip()] = item

        if not parsed:
            raise UnauthorizedError("Apple JWKS contained no usable keys")

        self._cached_keys = parsed
        self._cache_expires_at = time.monotonic() + self._cache_ttl


class NoopEmailOtpSender:
    """Default dev/test OTP sender adapter that performs zero PII logging (Invariant I10)."""

    @property
    def provider_name(self) -> str:
        return "noop"

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: Any | None = None,
    ) -> Any:
        # Intentionally never logs email or otp_code (Invariant I10).
        from mindweave.modules.identity.domain.models import EmailDeliveryReceipt

        del email, otp_code, purpose, expires_in_seconds, challenge_id
        return EmailDeliveryReceipt(
            delivered=False,
            provider="noop",
            delivery_status="dev_bypass",
            provider_message_id=None,
        )

    async def aclose(self) -> None:
        return None
