"""Pure domain port protocols for Identity external adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from mindweave.modules.identity.domain.models import (
    EmailDeliveryReceipt,
    OtpPurpose,
    VerifiedAppleIdentity,
)


class AppleTokenVerifierPort(Protocol):
    """Port for verifying Sign in with Apple identity JWTs."""

    async def verify_identity_token(
        self,
        identity_token: str,
        *,
        now: datetime | None = None,
    ) -> VerifiedAppleIdentity: ...


class EmailOtpSenderPort(Protocol):
    """Port for dispatching email OTP codes without logging PII."""

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: UUID | None = None,
    ) -> EmailDeliveryReceipt | None: ...
