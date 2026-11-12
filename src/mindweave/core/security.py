"""JWT token issuance/verification, refresh token hashing, and OTP security."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt

from mindweave.core.errors import ReauthRequiredError, UnauthorizedError
from mindweave.core.ids import new_id

TokenPurpose = Literal["access", "reauth"]


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: UUID
    jti: UUID
    purpose: TokenPurpose
    issued_at: datetime
    expires_at: datetime


class TokenService:
    def __init__(
        self,
        *,
        secret: str,
        issuer: str = "mindweave",
        access_ttl_seconds: int = 900,
        reauth_ttl_seconds: int = 300,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("JWT secret must be at least 32 characters")
        self._secret = secret
        self._issuer = issuer
        self._access_ttl = access_ttl_seconds
        self._reauth_ttl = reauth_ttl_seconds

    def create_access_token(
        self, user_id: UUID, *, now: datetime | None = None
    ) -> tuple[str, TokenClaims]:
        return self._issue(user_id=user_id, purpose="access", ttl=self._access_ttl, now=now)

    def create_reauth_token(
        self, user_id: UUID, *, now: datetime | None = None
    ) -> tuple[str, TokenClaims]:
        return self._issue(user_id=user_id, purpose="reauth", ttl=self._reauth_ttl, now=now)

    def _issue(
        self,
        *,
        user_id: UUID,
        purpose: TokenPurpose,
        ttl: int,
        now: datetime | None = None,
    ) -> tuple[str, TokenClaims]:
        iat = (now or datetime.now(UTC)).astimezone(UTC)
        exp = iat + timedelta(seconds=ttl)
        jti = new_id()
        payload = {
            "sub": str(user_id),
            "jti": str(jti),
            "iss": self._issuer,
            "typ": purpose,
            "iat": int(iat.timestamp()),
            "exp": int(exp.timestamp()),
        }
        encoded = jwt.encode(payload, self._secret, algorithm="HS256")
        return encoded, TokenClaims(
            user_id=user_id,
            jti=jti,
            purpose=purpose,
            issued_at=iat,
            expires_at=exp,
        )

    def verify_access_token(self, token: str, *, now: datetime | None = None) -> TokenClaims:
        claims = self._decode(token, now=now)
        if claims.purpose != "access":
            raise UnauthorizedError("Invalid token type; expected access token")
        return claims

    def verify_reauth_token(
        self, token: str, *, expected_user_id: UUID, now: datetime | None = None
    ) -> TokenClaims:
        try:
            claims = self._decode(token, now=now)
        except UnauthorizedError as exc:
            raise ReauthRequiredError(exc.detail) from exc
        if claims.purpose != "reauth" or claims.user_id != expected_user_id:
            raise ReauthRequiredError("Valid step-up re-authentication token is required")
        return claims

    def _decode(self, token: str, *, now: datetime | None = None) -> TokenClaims:
        try:
            verify_exp = now is None
            data = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                options={
                    "require": ["sub", "jti", "iss", "typ", "iat", "exp"],
                    "verify_exp": verify_exp,
                    "verify_iat": verify_exp,
                },
            )
            iat = datetime.fromtimestamp(int(data["iat"]), tz=UTC)
            exp = datetime.fromtimestamp(int(data["exp"]), tz=UTC)
            if now is not None and now.astimezone(UTC) >= exp:
                raise UnauthorizedError("Token has expired")
            purpose_raw = str(data["typ"])
            if purpose_raw not in ("access", "reauth"):
                raise UnauthorizedError("Unsupported token purpose")
            purpose: TokenPurpose = "access" if purpose_raw == "access" else "reauth"
            return TokenClaims(
                user_id=UUID(str(data["sub"])),
                jti=UUID(str(data["jti"])),
                purpose=purpose,
                issued_at=iat,
                expires_at=exp,
            )
        except UnauthorizedError:
            raise
        except Exception as exc:
            raise UnauthorizedError("Invalid or expired authentication token") from exc

    def sign_media_path(
        self,
        path: str,
        *,
        user_id: UUID,
        now: datetime | None = None,
        ttl_seconds: int = 900,
    ) -> str:
        """Append short-lived user-bound HMAC query parameters (`uid`, `exp`, `sig`) for `<img src>`/`<audio src>`."""
        clean_path = path.split("?", 1)[0]
        canonical = "/" + clean_path.lstrip("/").removeprefix("api/")
        iat = (now or datetime.now(UTC)).astimezone(UTC)
        exp = int((iat + timedelta(seconds=ttl_seconds)).timestamp())
        sig = hmac.new(
            self._secret.encode("utf-8"),
            f"media:{user_id}:{canonical}:{exp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}uid={user_id}&exp={exp}&sig={sig}"

    def verify_media_signature(
        self,
        *,
        path: str,
        uid: str,
        exp: int,
        sig: str,
        now: datetime | None = None,
    ) -> UUID:
        try:
            user_id = UUID(uid)
        except ValueError as exc:
            raise UnauthorizedError("Invalid media signature user ID") from exc
        current_ts = int((now or datetime.now(UTC)).astimezone(UTC).timestamp())
        if current_ts >= exp:
            raise UnauthorizedError("Signed media URL has expired")
        canonical = "/" + path.split("?", 1)[0].lstrip("/").removeprefix("api/")
        expected = hmac.new(
            self._secret.encode("utf-8"),
            f"media:{user_id}:{canonical}:{exp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig.strip().lower()):
            raise UnauthorizedError("Invalid media URL signature")
        return user_id


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_six_digit_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(*, challenge_id: UUID, otp_code: str, secret: str) -> bytes:
    key = secret.encode("utf-8")
    msg = f"otp:{challenge_id}:{otp_code.strip()}".encode()
    return hmac.new(key, msg, hashlib.sha256).digest()


def verify_otp_constant_time(
    *, challenge_id: UUID, otp_code: str, expected_hash: bytes, secret: str
) -> bool:
    computed = hash_otp(challenge_id=challenge_id, otp_code=otp_code, secret=secret)
    return hmac.compare_digest(computed, expected_hash)


def hash_contact_lookup(normalized_contact: str, *, secret: str) -> bytes:
    """Deterministic server-pepper HMAC for looking up email/Apple subject without plaintext index."""
    return hmac.new(
        secret.encode("utf-8"),
        f"identity:{normalized_contact.strip().lower()}".encode(),
        hashlib.sha256,
    ).digest()
