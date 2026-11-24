"""CamelCase request and response DTOs for Identity & Consents endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.modules.identity.domain.models import ConsentRecord, OtpPurpose
from mindweave.shared.enums import ConsentKind


class AppleAuthRequest(CamelModel):
    identity_token: str = Field(min_length=1, max_length=8192)
    authorization_code: str | None = Field(default=None, max_length=4096)
    display_email: str | None = Field(default=None, max_length=320)


class AuthTokensResponse(CamelModel):
    user_id: UUID
    is_new_user: bool
    access_token: str
    refresh_token: str
    expires_in: int


class EmailOtpRequest(CamelModel):
    email: str = Field(min_length=3, max_length=254)
    purpose: OtpPurpose = OtpPurpose.LOGIN


class EmailOtpChallengeResponse(CamelModel):
    challenge_id: UUID
    expires_in: int
    delivered: bool = False
    delivery_status: str = "dev_bypass"
    provider: str = "noop"
    cooldown_seconds: int = 0
    dev_otp_code: str | None = None


class EmailOtpVerifyRequest(CamelModel):
    challenge_id: UUID
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=32)


class AppleReauthRequest(CamelModel):
    identity_token: str = Field(min_length=1, max_length=8192)


class ReauthTokenResponse(CamelModel):
    reauth_token: str
    expires_in: int


class RefreshTokenRequest(CamelModel):
    refresh_token: str = Field(min_length=1, max_length=1024)


class RefreshTokensResponse(CamelModel):
    access_token: str
    refresh_token: str
    expires_in: int


class LogoutRequest(CamelModel):
    refresh_token: str | None = Field(default=None, max_length=1024)


class LogoutResponse(CamelModel):
    ok: bool = True


class RecordConsentRequest(CamelModel):
    consent_kind: ConsentKind
    policy_version: str = Field(min_length=1, max_length=32)
    granted: bool = True


class ConsentItemResponse(CamelModel):
    id: UUID
    consent_kind: ConsentKind
    policy_version: str
    granted: bool
    granted_at: datetime
    revoked_at: datetime | None = None

    @classmethod
    def from_domain(cls, record: ConsentRecord) -> ConsentItemResponse:
        return cls(
            id=record.id,
            consent_kind=record.consent_kind,
            policy_version=record.policy_version,
            granted=record.granted,
            granted_at=record.granted_at,
            revoked_at=record.revoked_at,
        )


class ConsentsListResponse(CamelModel):
    items: list[ConsentItemResponse]
