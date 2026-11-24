"""Pure domain entities, value objects, and deterministic auth policies for Identity.

Strictly zero framework, database, HTTP, or ORM imports (enforced by `.importlinter`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from mindweave.shared.enums import ConsentKind

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_CODE_PATTERN = re.compile(r"^\d{6}$")


class IdentityDomainError(ValueError):
    """Base pure-domain validation/policy error for the identity module."""


class InvalidEmailError(IdentityDomainError):
    """Raised when an email address fails canonical validation."""


class InvalidOtpCodeError(IdentityDomainError):
    """Raised when an OTP code is not a 6-digit numeric code."""


class InvalidPolicyVersionError(IdentityDomainError):
    """Raised when a consent policy version string is empty or malformed."""


class AuthProvider(StrEnum):
    APPLE = "apple"
    EMAIL = "email"


class OtpPurpose(StrEnum):
    LOGIN = "login"
    REAUTH = "reauth"


class OtpChallengeState(StrEnum):
    READY = "ready"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    LOCKED_OUT = "locked_out"


class RefreshTokenState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    REPLAY_DETECTED = "replay_detected"


@dataclass(frozen=True, slots=True)
class EmailDeliveryReceipt:
    """Honest provider delivery receipt for an issued email OTP."""

    delivered: bool
    provider: str
    delivery_status: Literal["delivered", "dev_bypass"]
    provider_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAppleIdentity:
    """Verified claims extracted from an Apple Sign-In identity token."""

    subject: str
    email: str | None = None
    email_verified: bool = False


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """PIPL Art. 28/29 user consent domain record."""

    id: UUID
    user_id: UUID
    consent_kind: ConsentKind
    policy_version: str
    granted: bool
    granted_at: datetime
    revoked_at: datetime | None = None


def normalize_email(raw_email: str) -> str:
    """Validate and canonicalize an email address without echoing PII in errors."""
    cleaned = raw_email.strip().lower()
    if not cleaned or len(cleaned) > 254 or not _EMAIL_PATTERN.match(cleaned):
        raise InvalidEmailError("Invalid email address format")
    local_part, _, domain_part = cleaned.partition("@")
    if not local_part or len(local_part) > 64 or not domain_part or ".." in cleaned:
        raise InvalidEmailError("Invalid email address format")
    return cleaned


def normalize_otp_code(raw_code: str) -> str:
    """Validate and canonicalize a 6-digit numeric OTP code."""
    cleaned = raw_code.strip()
    if not _OTP_CODE_PATTERN.match(cleaned):
        raise InvalidOtpCodeError("Verification code must be exactly 6 digits")
    return cleaned


def normalize_policy_version(raw_version: str) -> str:
    """Validate and canonicalize a consent policy version identifier."""
    cleaned = raw_version.strip()
    if not cleaned or len(cleaned) > 32:
        raise InvalidPolicyVersionError("Policy version must be 1..32 non-empty characters")
    return cleaned


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def evaluate_otp_challenge_state(
    *,
    consumed_at: datetime | None,
    expires_at: datetime,
    attempts: int,
    max_attempts: int,
    now: datetime,
) -> OtpChallengeState:
    """Deterministic state evaluation for an email OTP challenge."""
    if consumed_at is not None:
        return OtpChallengeState.CONSUMED
    if _as_utc(expires_at) <= _as_utc(now):
        return OtpChallengeState.EXPIRED
    if attempts >= max_attempts:
        return OtpChallengeState.LOCKED_OUT
    return OtpChallengeState.READY


def evaluate_refresh_token_state(
    *,
    expires_at: datetime,
    revoked_at: datetime | None,
    replaced_by_id: UUID | None,
    now: datetime,
) -> RefreshTokenState:
    """Deterministic evaluation for refresh token rotation & replay detection."""
    if _as_utc(expires_at) <= _as_utc(now):
        return RefreshTokenState.EXPIRED
    if revoked_at is not None or replaced_by_id is not None:
        return RefreshTokenState.REPLAY_DETECTED
    return RefreshTokenState.VALID
