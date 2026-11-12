"""Application use-case service for Identity, Auth, Step-Up Reauth, and Consents."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import (
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
    ValidationFailedError,
)
from mindweave.core.idempotency import IdempotencyHit, hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.logging import get_logger
from mindweave.core.orm import UserRow
from mindweave.core.security import (
    generate_refresh_token,
    generate_six_digit_otp,
    hash_contact_lookup,
    hash_opaque_token,
    hash_otp,
    verify_otp_constant_time,
)
from mindweave.modules.audit.application.service import AuditService
from mindweave.modules.identity.domain.models import (
    AuthProvider,
    ConsentRecord,
    InvalidEmailError,
    InvalidPolicyVersionError,
    OtpChallengeState,
    OtpPurpose,
    RefreshTokenState,
    evaluate_otp_challenge_state,
    evaluate_refresh_token_state,
    normalize_email,
    normalize_policy_version,
)
from mindweave.modules.identity.domain.ports import AppleTokenVerifierPort, EmailOtpSenderPort
from mindweave.modules.identity.infra.apple_verifier import (
    AppleJwksTokenVerifier,
    NoopEmailOtpSender,
)
from mindweave.modules.identity.infra.repositories import (
    IdentitySystemRepository,
    IdentityUserRepository,
)
from mindweave.shared.enums import ConsentKind
from mindweave.shared.scopes import UserScope

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuthSessionResult:
    user_id: UUID
    is_new_user: bool
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class OtpChallengeIssuedResult:
    challenge_id: UUID
    expires_in: int
    dev_otp_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReauthIssuedResult:
    reauth_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class RefreshedSessionResult:
    access_token: str
    refresh_token: str
    expires_in: int


class IdentityService:
    """Orchestrates authentication, user provisioning, token family rotation, and PIPL consents."""

    OTP_RATE_LIMIT_CAPACITY: int = 5
    OTP_RATE_LIMIT_WINDOW_SECONDS: int = 60

    def __init__(
        self,
        container: AppContainer,
        *,
        apple_verifier: AppleTokenVerifierPort | None = None,
        otp_sender: EmailOtpSenderPort | None = None,
    ) -> None:
        self._container = container
        self._sys_repo = IdentitySystemRepository()
        self._user_repo = IdentityUserRepository()
        self._audit = AuditService(clock=container.clock, secret=container.settings.jwt_secret)

        if apple_verifier is not None:
            self._apple_verifier = apple_verifier
        else:
            cached_verifier = container.extras.get("apple_verifier")
            if cached_verifier is None:
                cached_verifier = AppleJwksTokenVerifier(
                    allow_dev_mock=container.settings.allow_dev_mock_apple_auth
                )
                container.extras["apple_verifier"] = cached_verifier
            self._apple_verifier = cached_verifier

        if otp_sender is not None:
            self._otp_sender = otp_sender
        else:
            cached_sender = container.extras.get("otp_sender")
            if cached_sender is None:
                cached_sender = NoopEmailOtpSender()
                container.extras["otp_sender"] = cached_sender
            self._otp_sender = cached_sender

    async def authenticate_apple(
        self,
        *,
        identity_token: str,
        authorization_code: str | None = None,
        display_email: str | None = None,
        client_ip: str | None = None,
    ) -> AuthSessionResult:
        del authorization_code  # Reserved for optional server-to-server Apple revocation exchange
        now = self._container.clock.now()
        verified = await self._apple_verifier.verify_identity_token(identity_token, now=now)

        contact_email: str | None = verified.email
        if contact_email is None and display_email is not None and display_email.strip():
            try:
                contact_email = normalize_email(display_email)
            except InvalidEmailError as exc:
                raise ValidationFailedError("Invalid displayEmail format") from exc

        subject_hmac = hash_contact_lookup(
            verified.subject, secret=self._container.settings.jwt_secret
        )
        return await self._login_or_provision(
            provider=AuthProvider.APPLE,
            subject_hmac=subject_hmac,
            contact_str=contact_email,
            client_ip=client_ip,
        )

    async def request_email_otp(
        self,
        *,
        email: str,
        purpose: OtpPurpose,
    ) -> OtpChallengeIssuedResult:
        try:
            normalized_email = normalize_email(email)
        except InvalidEmailError as exc:
            raise ValidationFailedError("Invalid email address format") from exc

        email_hmac = hash_contact_lookup(
            normalized_email, secret=self._container.settings.jwt_secret
        )

        ttl_seconds = self._container.settings.otp_ttl_seconds
        challenge_id = new_id()
        otp_code = generate_six_digit_otp()
        otp_digest = hash_otp(
            challenge_id=challenge_id,
            otp_code=otp_code,
            secret=self._container.settings.jwt_secret,
        )

        async with self._container.db.system_session() as sys_sess:
            await self._sys_repo.acquire_email_otp_advisory_lock(
                sys_sess,
                email_hmac=email_hmac,
            )
            now = self._container.clock.now()
            window_start = now - timedelta(seconds=self.OTP_RATE_LIMIT_WINDOW_SECONDS)
            recent_count = await self._sys_repo.count_recent_otp_challenges(
                sys_sess,
                email_hmac=email_hmac,
                window_start=window_start,
            )
            if recent_count >= self.OTP_RATE_LIMIT_CAPACITY:
                raise RateLimitedError(
                    "Rate limit exceeded. Please slow down and try again shortly.",
                    extensions={"retryAfterSeconds": self.OTP_RATE_LIMIT_WINDOW_SECONDS},
                )

            await self._sys_repo.invalidate_unconsumed_otp_challenges(
                sys_sess,
                email_hmac=email_hmac,
                purpose=purpose,
                now=now,
            )
            await self._sys_repo.create_otp_challenge(
                sys_sess,
                challenge_id=challenge_id,
                email_hmac=email_hmac,
                otp_hash=otp_digest,
                purpose=purpose,
                max_attempts=self._container.settings.otp_max_attempts,
                expires_at=now + timedelta(seconds=ttl_seconds),
                now=now,
            )

        await self._otp_sender.send_otp(
            email=normalized_email,
            otp_code=otp_code,
            purpose=purpose,
            expires_in_seconds=ttl_seconds,
        )
        # Never log email or OTP code (Invariant I10)!
        logger.info(
            "auth.otp_challenge_created",
            challenge_id=str(challenge_id),
            purpose=purpose.value,
            expires_in=ttl_seconds,
        )

        return OtpChallengeIssuedResult(
            challenge_id=challenge_id,
            expires_in=ttl_seconds,
            dev_otp_code=(
                otp_code if self._container.settings.expose_dev_otp_in_response else None
            ),
        )

    async def verify_email_otp(
        self,
        *,
        challenge_id: UUID,
        email: str,
        code: str,
        authenticated_scope: UserScope | None = None,
        client_ip: str | None = None,
    ) -> AuthSessionResult | ReauthIssuedResult:
        try:
            normalized_email = normalize_email(email)
        except InvalidEmailError as exc:
            raise ValidationFailedError("Invalid email address format") from exc

        cleaned_code = code.strip()
        if not cleaned_code or len(cleaned_code) > 32:
            raise ValidationFailedError("Verification code must be non-empty")

        email_hmac = hash_contact_lookup(
            normalized_email, secret=self._container.settings.jwt_secret
        )
        now = self._container.clock.now()

        pending_error: UnauthorizedError | None = None
        verified_purpose: OtpPurpose | None = None
        reauth_result: ReauthIssuedResult | None = None

        async with self._container.db.system_session() as sys_sess:
            challenge = await self._sys_repo.get_otp_challenge_for_update(sys_sess, challenge_id)
            if challenge is None:
                raise UnauthorizedError("Invalid or expired verification challenge")

            state = evaluate_otp_challenge_state(
                consumed_at=challenge.consumed_at,
                expires_at=challenge.expires_at,
                attempts=challenge.attempts,
                max_attempts=challenge.max_attempts,
                now=now,
            )
            if state is OtpChallengeState.CONSUMED:
                raise UnauthorizedError(
                    "Verification challenge has already been consumed",
                    error_code="otp_already_consumed",
                )
            if state is OtpChallengeState.EXPIRED:
                raise UnauthorizedError(
                    "Verification challenge has expired",
                    error_code="otp_expired",
                )
            if state is OtpChallengeState.LOCKED_OUT:
                raise UnauthorizedError(
                    "Maximum verification attempts exceeded",
                    error_code="otp_max_attempts_exceeded",
                )

            purpose = OtpPurpose(challenge.purpose)
            if purpose is OtpPurpose.REAUTH:
                if authenticated_scope is None:
                    raise UnauthorizedError(
                        "Bearer authentication is required for step-up re-authentication"
                    )
                belongs = await self._verify_email_belongs_to_user(
                    sys_sess_identities=await self._sys_repo.list_user_identities(
                        sys_sess, user_id=authenticated_scope.user_id
                    ),
                    scope=authenticated_scope,
                    normalized_email=normalized_email,
                    email_hmac=email_hmac,
                )
                if not belongs:
                    raise ForbiddenError("Email does not match the authenticated account")

            email_matches = hmac.compare_digest(challenge.email_hmac, email_hmac)
            code_matches = verify_otp_constant_time(
                challenge_id=challenge.id,
                otp_code=cleaned_code,
                expected_hash=challenge.otp_hash,
                secret=self._container.settings.jwt_secret,
            )

            challenge.attempts += 1
            if not (email_matches and code_matches):
                # Do NOT raise inside system_session context so attempts increment commits!
                if challenge.attempts >= challenge.max_attempts:
                    pending_error = UnauthorizedError(
                        "Maximum verification attempts exceeded",
                        error_code="otp_max_attempts_exceeded",
                    )
                else:
                    pending_error = UnauthorizedError(
                        "Invalid verification code",
                        error_code="invalid_otp",
                    )
            else:
                challenge.consumed_at = now
                verified_purpose = purpose
                if purpose is OtpPurpose.REAUTH:
                    assert authenticated_scope is not None
                    reauth_jwt, _ = self._container.tokens.create_reauth_token(
                        authenticated_scope.user_id, now=now
                    )
                    await self._audit.record(
                        sys_sess,
                        user_id=authenticated_scope.user_id,
                        action="auth.reauth",
                        target_type="user",
                        target_id=str(authenticated_scope.user_id),
                        metadata={"provider": AuthProvider.EMAIL.value},
                        client_ip=client_ip,
                    )
                    reauth_result = ReauthIssuedResult(
                        reauth_token=reauth_jwt,
                        expires_in=self._container.settings.reauth_token_ttl_seconds,
                    )

        if pending_error is not None:
            raise pending_error

        if verified_purpose is OtpPurpose.REAUTH:
            assert reauth_result is not None
            return reauth_result

        return await self._login_or_provision(
            provider=AuthProvider.EMAIL,
            subject_hmac=email_hmac,
            contact_str=normalized_email,
            client_ip=client_ip,
        )

    async def reauth_apple(
        self,
        scope: UserScope,
        *,
        identity_token: str,
        client_ip: str | None = None,
    ) -> ReauthIssuedResult:
        now = self._container.clock.now()
        verified = await self._apple_verifier.verify_identity_token(identity_token, now=now)
        subject_hmac = hash_contact_lookup(
            verified.subject, secret=self._container.settings.jwt_secret
        )

        async with self._container.db.system_session() as sys_sess:
            identities = await self._sys_repo.list_user_identities(
                sys_sess,
                user_id=scope.user_id,
                provider=AuthProvider.APPLE,
            )
            matched = next(
                (row for row in identities if hmac.compare_digest(row.subject_hmac, subject_hmac)),
                None,
            )
            if matched is None:
                raise ForbiddenError("Apple identity does not match the authenticated account")

            matched.last_login_at = now
            reauth_jwt, _ = self._container.tokens.create_reauth_token(scope.user_id, now=now)
            await self._audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="auth.reauth",
                target_type="user",
                target_id=str(scope.user_id),
                metadata={"provider": AuthProvider.APPLE.value},
                client_ip=client_ip,
            )

        return ReauthIssuedResult(
            reauth_token=reauth_jwt,
            expires_in=self._container.settings.reauth_token_ttl_seconds,
        )

    async def refresh_session(
        self,
        *,
        refresh_token: str,
        client_ip: str | None = None,
    ) -> RefreshedSessionResult:
        cleaned = refresh_token.strip()
        if not cleaned:
            raise UnauthorizedError("Missing refresh token")

        token_hash = hash_opaque_token(cleaned)
        now = self._container.clock.now()

        replay_error: UnauthorizedError | None = None
        result: RefreshedSessionResult | None = None

        async with self._container.db.system_session() as sys_sess:
            row = await self._sys_repo.get_refresh_token_for_update(sys_sess, token_hash)
            if row is None:
                raise UnauthorizedError("Invalid or expired refresh token")

            state = evaluate_refresh_token_state(
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                replaced_by_id=row.replaced_by_id,
                now=now,
            )
            if state is RefreshTokenState.EXPIRED:
                raise UnauthorizedError("Refresh token has expired")

            if state is RefreshTokenState.REPLAY_DETECTED:
                # Commit family-wide revocation & audit event BEFORE raising UnauthorizedError!
                revoked_count = await self._sys_repo.revoke_refresh_family(
                    sys_sess,
                    family_id=row.family_id,
                    now=now,
                )
                await self._audit.record(
                    sys_sess,
                    user_id=row.user_id,
                    action="auth.refresh_replay_revoked",
                    target_type="refresh_token_family",
                    target_id=str(row.family_id),
                    metadata={
                        "replayedTokenId": str(row.id),
                        "revokedFamilyCount": revoked_count,
                    },
                    client_ip=client_ip,
                )
                logger.warning(
                    "auth.refresh_replay_revoked",
                    family_id=str(row.family_id),
                    revoked_count=revoked_count,
                )
                replay_error = UnauthorizedError(
                    "Refresh token replay detected; token family revoked",
                    error_code="refresh_token_replayed",
                )
            else:
                user_row = await self._sys_repo.get_user(sys_sess, row.user_id)
                if (
                    user_row is None
                    or user_row.deleted_at is not None
                    or user_row.status != "active"
                ):
                    raise UnauthorizedError("Account is inactive or has been deleted")

                new_raw_refresh = generate_refresh_token()
                new_refresh_id = new_id()
                new_row = await self._sys_repo.create_refresh_token(
                    sys_sess,
                    token_id=new_refresh_id,
                    user_id=row.user_id,
                    family_id=row.family_id,
                    token_hash=hash_opaque_token(new_raw_refresh),
                    expires_at=now
                    + timedelta(days=self._container.settings.refresh_token_ttl_days),
                    now=now,
                )
                row.replaced_by_id = new_row.id
                row.revoked_at = now
                await sys_sess.flush()

                access_jwt, _ = self._container.tokens.create_access_token(row.user_id, now=now)
                result = RefreshedSessionResult(
                    access_token=access_jwt,
                    refresh_token=new_raw_refresh,
                    expires_in=self._container.settings.access_token_ttl_seconds,
                )

        if replay_error is not None:
            raise replay_error

        assert result is not None
        return result

    async def logout(
        self,
        scope: UserScope,
        *,
        refresh_token: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        now = self._container.clock.now()
        async with self._container.db.system_session() as sys_sess:
            if refresh_token is not None and refresh_token.strip():
                token_hash = hash_opaque_token(refresh_token.strip())
                row = await self._sys_repo.get_user_refresh_token(
                    sys_sess,
                    user_id=scope.user_id,
                    token_hash=token_hash,
                )
                if row is not None:
                    await self._sys_repo.revoke_refresh_family(
                        sys_sess,
                        family_id=row.family_id,
                        now=now,
                    )
                revocation_scope = "family"
            else:
                await self._sys_repo.revoke_all_user_refresh_tokens(
                    sys_sess,
                    user_id=scope.user_id,
                    now=now,
                )
                revocation_scope = "all"

            await self._audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="auth.logout",
                target_type="user",
                target_id=str(scope.user_id),
                metadata={"revocationScope": revocation_scope},
                client_ip=client_ip,
            )

    async def list_consents(self, scope: UserScope) -> list[ConsentRecord]:
        async with self._container.db.user_session(scope) as session:
            return await self._user_repo.list_consents(session, scope)

    async def record_consent(
        self,
        scope: UserScope,
        *,
        idem_key: str,
        consent_kind: ConsentKind,
        policy_version: str,
        granted: bool,
        serializer: Any,
        client_ip: str | None = None,
    ) -> tuple[ConsentRecord | None, IdempotencyHit | None]:
        try:
            normalized_version = normalize_policy_version(policy_version)
        except InvalidPolicyVersionError as exc:
            raise ValidationFailedError(str(exc)) from exc

        req_hash = hash_request_payload(
            {
                "consentKind": consent_kind.value,
                "policyVersion": normalized_version,
                "granted": granted,
            }
        )
        now = self._container.clock.now()

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path="/me/consents",
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            record = await self._user_repo.record_consent(
                session,
                scope=scope,
                consent_kind=consent_kind,
                policy_version=normalized_version,
                granted=granted,
                now=now,
            )
            assert idem_row is not None
            response_body: dict[str, Any] = serializer(record)
            await self._container.idempotency.complete(
                session,
                idem_row,
                status_code=200,
                response_body=response_body,
            )

        async with self._container.db.system_session() as sys_sess:
            await self._audit.record(
                sys_sess,
                user_id=scope.user_id,
                action="consent.updated",
                target_type="consent",
                target_id=str(record.id),
                metadata={
                    "consentKind": consent_kind.value,
                    "policyVersion": normalized_version,
                    "granted": granted,
                },
                client_ip=client_ip,
            )

        return record, None

    async def _login_or_provision(
        self,
        *,
        provider: AuthProvider,
        subject_hmac: bytes,
        contact_str: str | None,
        client_ip: str | None,
        _retried: bool = False,
    ) -> AuthSessionResult:
        now = self._container.clock.now()

        # 1. Fast path: check if identity already exists
        async with self._container.db.system_session() as sys_sess:
            existing = await self._sys_repo.get_auth_identity(
                sys_sess,
                provider=provider,
                subject_hmac=subject_hmac,
                for_update=True,
            )
            if existing is not None:
                user_row = await self._sys_repo.get_user(sys_sess, existing.user_id)
                if (
                    user_row is None
                    or user_row.deleted_at is not None
                    or user_row.status != "active"
                ):
                    raise UnauthorizedError("Account is inactive or has been deleted")

                existing.last_login_at = now
                user_row.updated_at = now

                raw_refresh = generate_refresh_token()
                await self._sys_repo.create_refresh_token(
                    sys_sess,
                    token_id=new_id(),
                    user_id=user_row.id,
                    family_id=new_id(),
                    token_hash=hash_opaque_token(raw_refresh),
                    expires_at=now
                    + timedelta(days=self._container.settings.refresh_token_ttl_days),
                    now=now,
                )
                await self._audit.record(
                    sys_sess,
                    user_id=user_row.id,
                    action="auth.login",
                    target_type="user",
                    target_id=str(user_row.id),
                    metadata={"provider": provider.value, "isNewUser": False},
                    client_ip=client_ip,
                )
                access_jwt, _ = self._container.tokens.create_access_token(user_row.id, now=now)
                existing_user_id = user_row.id
                existing_identity_id = existing.id
                needs_contact_backfill = existing.contact_enc is None and contact_str is not None

        if existing is not None:
            if needs_contact_backfill and contact_str is not None:
                scope = UserScope(existing_user_id)
                async with self._container.db.user_session(scope) as user_sess:
                    dek = await self._container.load_user_dek(user_sess, scope)
                contact_enc = self._container.cipher.encrypt_str(
                    dek,
                    contact_str,
                    user_id=existing_user_id,
                    table="auth_identities",
                    column="contact_enc",
                    row_id=existing_identity_id,
                )
                async with self._container.db.system_session() as sys_sess:
                    ident_row = await self._sys_repo.get_auth_identity(
                        sys_sess,
                        provider=provider,
                        subject_hmac=subject_hmac,
                        for_update=True,
                    )
                    if ident_row is not None and ident_row.contact_enc is None:
                        ident_row.contact_enc = contact_enc

            return AuthSessionResult(
                user_id=existing_user_id,
                is_new_user=False,
                access_token=access_jwt,
                refresh_token=raw_refresh,
                expires_in=self._container.settings.access_token_ttl_seconds,
            )

        # 2. New user provisioning:
        #    a) Generate DEK & encrypt contact_enc in memory with AAD (user_id, "auth_identities", "contact_enc", identity_id)
        #    b) Create UserRow + AuthIdentityRow in system_session
        #    c) Create UserKeyRow + default UserPreferenceRow inside user_session(UserScope(user_id)) so RLS WITH CHECK passes!
        #       (Strictly ZERO fictional/demo people!)
        #    d) Issue initial RefreshTokenRow & audit "auth.login"
        user_id = new_id()
        identity_id = new_id()
        wrapped_dek, kek_id = self._container.cipher.generate_wrapped_dek(user_id)
        dek = self._container.cipher.load_dek(
            user_id=user_id,
            wrapped_dek=wrapped_dek,
            kek_id=kek_id,
        )
        new_contact_enc: bytes | None = None
        if contact_str is not None:
            new_contact_enc = self._container.cipher.encrypt_str(
                dek,
                contact_str,
                user_id=user_id,
                table="auth_identities",
                column="contact_enc",
                row_id=identity_id,
            )

        try:
            async with self._container.db.system_session() as sys_sess:
                await self._sys_repo.create_user_and_identity(
                    sys_sess,
                    user_id=user_id,
                    identity_id=identity_id,
                    provider=provider,
                    subject_hmac=subject_hmac,
                    contact_enc=new_contact_enc,
                    now=now,
                )
        except IntegrityError:
            self._container.cipher.evict_user_dek(user_id)
            if not _retried:
                return await self._login_or_provision(
                    provider=provider,
                    subject_hmac=subject_hmac,
                    contact_str=contact_str,
                    client_ip=client_ip,
                    _retried=True,
                )
            raise

        scope = UserScope(user_id)
        try:
            async with self._container.db.user_session(scope) as user_sess:
                await self._user_repo.provision_user_workspace(
                    user_sess,
                    scope=scope,
                    wrapped_dek=wrapped_dek,
                    kek_id=kek_id,
                    default_timezone=self._container.settings.default_timezone,
                    now=now,
                )
        except Exception:
            self._container.cipher.evict_user_dek(user_id)
            async with self._container.db.system_session() as cleanup_sess:
                orphan = await cleanup_sess.get(UserRow, user_id)
                if orphan is not None:
                    await cleanup_sess.delete(orphan)
            raise

        raw_refresh = generate_refresh_token()
        async with self._container.db.system_session() as sys_sess:
            await self._sys_repo.create_refresh_token(
                sys_sess,
                token_id=new_id(),
                user_id=user_id,
                family_id=new_id(),
                token_hash=hash_opaque_token(raw_refresh),
                expires_at=now + timedelta(days=self._container.settings.refresh_token_ttl_days),
                now=now,
            )
            await self._audit.record(
                sys_sess,
                user_id=user_id,
                action="auth.login",
                target_type="user",
                target_id=str(user_id),
                metadata={"provider": provider.value, "isNewUser": True},
                client_ip=client_ip,
            )

        access_jwt, _ = self._container.tokens.create_access_token(user_id, now=now)
        return AuthSessionResult(
            user_id=user_id,
            is_new_user=True,
            access_token=access_jwt,
            refresh_token=raw_refresh,
            expires_in=self._container.settings.access_token_ttl_seconds,
        )

    async def _verify_email_belongs_to_user(
        self,
        *,
        sys_sess_identities: list[Any],
        scope: UserScope,
        normalized_email: str,
        email_hmac: bytes,
    ) -> bool:
        for row in sys_sess_identities:
            if row.provider == AuthProvider.EMAIL.value and hmac.compare_digest(
                row.subject_hmac, email_hmac
            ):
                return True

        encrypted_rows = [row for row in sys_sess_identities if row.contact_enc is not None]
        if not encrypted_rows:
            return False

        async with self._container.db.user_session(scope) as user_sess:
            dek = await self._container.load_user_dek(user_sess, scope)

        for row in encrypted_rows:
            decrypted = self._container.cipher.decrypt_str(
                dek,
                row.contact_enc,
                user_id=scope.user_id,
                table="auth_identities",
                column="contact_enc",
                row_id=row.id,
            )
            if hmac.compare_digest(
                decrypted.strip().lower().encode("utf-8"),
                normalized_email.encode("utf-8"),
            ):
                return True
        return False
