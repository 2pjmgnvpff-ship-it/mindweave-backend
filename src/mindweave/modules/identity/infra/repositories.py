"""SQLAlchemy repositories for Identity system-scoped and RLS user-scoped tables."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.ids import new_id
from mindweave.core.orm import (
    AuthIdentityRow,
    ConsentRow,
    EmailOtpChallengeRow,
    RefreshTokenRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.identity.domain.models import AuthProvider, ConsentRecord, OtpPurpose
from mindweave.shared.enums import ConsentKind
from mindweave.shared.scopes import UserScope


class IdentitySystemRepository:
    """Repository operating strictly on auth bootstrap tables inside `system_session`."""

    async def get_auth_identity(
        self,
        session: AsyncSession,
        *,
        provider: AuthProvider,
        subject_hmac: bytes,
        for_update: bool = False,
    ) -> AuthIdentityRow | None:
        stmt = select(AuthIdentityRow).where(
            AuthIdentityRow.provider == provider.value,
            AuthIdentityRow.subject_hmac == subject_hmac,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    async def list_user_identities(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        provider: AuthProvider | None = None,
    ) -> list[AuthIdentityRow]:
        stmt = select(AuthIdentityRow).where(AuthIdentityRow.user_id == user_id)
        if provider is not None:
            stmt = stmt.where(AuthIdentityRow.provider == provider.value)
        rows = (await session.scalars(stmt)).all()
        return list(rows)

    async def get_user(self, session: AsyncSession, user_id: UUID) -> UserRow | None:
        return await session.get(UserRow, user_id)

    async def create_user_and_identity(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        identity_id: UUID,
        provider: AuthProvider,
        subject_hmac: bytes,
        contact_enc: bytes | None,
        now: datetime,
    ) -> tuple[UserRow, AuthIdentityRow]:
        user_row = UserRow(
            id=user_id,
            status="active",
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        identity_row = AuthIdentityRow(
            id=identity_id,
            user_id=user_id,
            provider=provider.value,
            subject_hmac=subject_hmac,
            contact_enc=contact_enc,
            created_at=now,
            last_login_at=now,
        )
        session.add(user_row)
        await session.flush()
        session.add(identity_row)
        await session.flush()
        return user_row, identity_row

    async def acquire_email_otp_advisory_lock(
        self,
        session: AsyncSession,
        *,
        email_hmac: bytes,
    ) -> None:
        """Acquire a transaction-scoped advisory lock keyed on email_hmac[:8] to serialize OTP issuance."""
        lock_key = int.from_bytes(email_hmac[:8], "big", signed=True)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": lock_key},
        )

    async def count_recent_otp_challenges(
        self,
        session: AsyncSession,
        *,
        email_hmac: bytes,
        window_start: datetime,
        exclude_failed: bool = False,
    ) -> int:
        """Count OTP challenges issued in the rate-limit window."""
        stmt = select(func.count(EmailOtpChallengeRow.id)).where(
            EmailOtpChallengeRow.email_hmac == email_hmac,
            EmailOtpChallengeRow.created_at >= window_start,
        )
        if exclude_failed:
            stmt = stmt.where(EmailOtpChallengeRow.delivery_status != "failed")
        count = await session.scalar(stmt)
        return int(count or 0)

    async def get_latest_otp_challenge_by_statuses(
        self,
        session: AsyncSession,
        *,
        email_hmac: bytes,
        statuses: tuple[str, ...],
        window_start: datetime,
        purpose: OtpPurpose | None = None,
    ) -> EmailOtpChallengeRow | None:
        """Return the most recent OTP challenge matching statuses (and optional purpose) in window."""
        conditions = [
            EmailOtpChallengeRow.email_hmac == email_hmac,
            EmailOtpChallengeRow.delivery_status.in_(statuses),
            EmailOtpChallengeRow.created_at >= window_start,
        ]
        if purpose is not None:
            conditions.append(EmailOtpChallengeRow.purpose == purpose.value)
        stmt = (
            select(EmailOtpChallengeRow)
            .where(*conditions)
            .order_by(EmailOtpChallengeRow.created_at.desc())
            .limit(1)
        )
        return await session.scalar(stmt)

    async def invalidate_unconsumed_otp_challenges(
        self,
        session: AsyncSession,
        *,
        email_hmac: bytes,
        purpose: OtpPurpose,
        now: datetime,
        exclude_challenge_id: UUID | None = None,
    ) -> int:
        """Mark prior unconsumed non-failed challenges for (email_hmac, purpose) as consumed."""
        conditions = [
            EmailOtpChallengeRow.email_hmac == email_hmac,
            EmailOtpChallengeRow.purpose == purpose.value,
            EmailOtpChallengeRow.consumed_at.is_(None),
            EmailOtpChallengeRow.delivery_status != "failed",
        ]
        if exclude_challenge_id is not None:
            conditions.append(EmailOtpChallengeRow.id != exclude_challenge_id)
        result = await session.execute(
            update(EmailOtpChallengeRow).where(*conditions).values(consumed_at=now)
        )
        await session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def create_otp_challenge(
        self,
        session: AsyncSession,
        *,
        challenge_id: UUID,
        email_hmac: bytes,
        otp_hash: bytes,
        purpose: OtpPurpose,
        max_attempts: int,
        expires_at: datetime,
        now: datetime,
        delivery_status: str = "pending",
        delivery_provider: str | None = None,
    ) -> EmailOtpChallengeRow:
        row = EmailOtpChallengeRow(
            id=challenge_id,
            email_hmac=email_hmac,
            otp_hash=otp_hash,
            purpose=purpose.value,
            attempts=0,
            max_attempts=max_attempts,
            expires_at=expires_at,
            consumed_at=None,
            delivery_provider=delivery_provider,
            delivery_status=delivery_status,
            provider_message_id=None,
            delivery_error_code=None,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def finalize_otp_challenge_delivered(
        self,
        session: AsyncSession,
        *,
        challenge_id: UUID,
        email_hmac: bytes,
        purpose: OtpPurpose,
        delivery_provider: str,
        delivery_status: str,
        provider_message_id: str | None,
        now: datetime,
    ) -> None:
        """Mark newly sent challenge delivered and invalidate prior unconsumed valid OTPs."""
        await session.execute(
            update(EmailOtpChallengeRow)
            .where(EmailOtpChallengeRow.id == challenge_id)
            .values(
                delivery_provider=delivery_provider,
                delivery_status=delivery_status,
                provider_message_id=provider_message_id,
                delivery_error_code=None,
            )
        )
        await self.invalidate_unconsumed_otp_challenges(
            session,
            email_hmac=email_hmac,
            purpose=purpose,
            now=now,
            exclude_challenge_id=challenge_id,
        )

    async def mark_otp_challenge_failed(
        self,
        session: AsyncSession,
        *,
        challenge_id: UUID,
        delivery_provider: str | None,
        error_code: str,
        now: datetime,
    ) -> None:
        """Mark only the failed attempt row as failed+consumed without touching prior valid OTPs."""
        await session.execute(
            update(EmailOtpChallengeRow)
            .where(EmailOtpChallengeRow.id == challenge_id)
            .values(
                delivery_provider=delivery_provider,
                delivery_status="failed",
                consumed_at=now,
                delivery_error_code=error_code[:64],
            )
        )
        await session.flush()

    async def get_otp_challenge_for_update(
        self,
        session: AsyncSession,
        challenge_id: UUID,
    ) -> EmailOtpChallengeRow | None:
        stmt = (
            select(EmailOtpChallengeRow)
            .where(EmailOtpChallengeRow.id == challenge_id)
            .with_for_update()
        )
        return await session.scalar(stmt)

    async def increment_otp_attempts_atomic(
        self,
        session: AsyncSession,
        *,
        challenge_id: UUID,
        now: datetime,
    ) -> int | None:
        """Atomically increment attempts and lock on max_attempts (prevents CWE-362 brute-force)."""
        row = (
            await session.execute(
                text(
                    """
                    UPDATE email_otp_challenges
                    SET attempts = attempts + 1,
                        consumed_at = CASE
                            WHEN attempts + 1 >= max_attempts THEN :now
                            ELSE consumed_at
                        END
                    WHERE id = :id AND consumed_at IS NULL
                    RETURNING attempts
                    """
                ),
                {"id": challenge_id, "now": now},
            )
        ).one_or_none()
        return int(row[0]) if row is not None else None

    async def consume_otp_challenge_atomic(
        self,
        session: AsyncSession,
        *,
        challenge_id: UUID,
        now: datetime,
    ) -> bool:
        """Atomically mark a valid challenge consumed on first match (zero double-spend)."""
        row = (
            await session.execute(
                text(
                    """
                    UPDATE email_otp_challenges
                    SET attempts = attempts + 1,
                        consumed_at = :now
                    WHERE id = :id
                      AND consumed_at IS NULL
                      AND attempts < max_attempts
                    RETURNING id
                    """
                ),
                {"id": challenge_id, "now": now},
            )
        ).one_or_none()
        return row is not None

    async def create_refresh_token(
        self,
        session: AsyncSession,
        *,
        token_id: UUID,
        user_id: UUID,
        family_id: UUID,
        token_hash: bytes,
        expires_at: datetime,
        now: datetime,
    ) -> RefreshTokenRow:
        row = RefreshTokenRow(
            id=token_id,
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            replaced_by_id=None,
            revoked_at=None,
            expires_at=expires_at,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_refresh_token_for_update(
        self,
        session: AsyncSession,
        token_hash: bytes,
    ) -> RefreshTokenRow | None:
        stmt = (
            select(RefreshTokenRow)
            .where(RefreshTokenRow.token_hash == token_hash)
            .with_for_update()
        )
        return await session.scalar(stmt)

    async def get_user_refresh_token(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        token_hash: bytes,
    ) -> RefreshTokenRow | None:
        stmt = (
            select(RefreshTokenRow)
            .where(
                RefreshTokenRow.user_id == user_id,
                RefreshTokenRow.token_hash == token_hash,
            )
            .with_for_update()
        )
        return await session.scalar(stmt)

    async def revoke_refresh_family(
        self,
        session: AsyncSession,
        *,
        family_id: UUID,
        now: datetime,
    ) -> int:
        result = await session.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.family_id == family_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def revoke_all_user_refresh_tokens(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        now: datetime,
    ) -> int:
        result = await session.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.user_id == user_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.flush()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class IdentityUserRepository:
    """Repository operating strictly inside RLS-enforced `user_session(scope)`."""

    async def provision_user_workspace(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        wrapped_dek: bytes,
        kek_id: str,
        default_timezone: str,
        now: datetime,
    ) -> None:
        """Provision per-user wrapped DEK and default preferences; zero fictional/demo people!"""
        key_row = UserKeyRow(
            user_id=scope.user_id,
            wrapped_dek=wrapped_dek,
            kek_id=kek_id,
            created_at=now,
            rotated_at=None,
            destroyed_at=None,
        )
        pref_row = UserPreferenceRow(
            user_id=scope.user_id,
            preferred_edition="cotton",
            cupid_layout="a",
            wallpaper_preset="warm_paper",
            wallpaper_object_key=None,
            wallpaper_blur=12,
            wallpaper_vignette=20,
            app_lock_enabled=False,
            late_night_care_enabled=True,
            timezone=default_timezone,
            version=1,
            updated_at=now,
        )
        session.add(key_row)
        session.add(pref_row)
        await session.flush()

    async def list_consents(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> list[ConsentRecord]:
        stmt = (
            select(ConsentRow)
            .where(ConsentRow.user_id == scope.user_id)
            .order_by(ConsentRow.granted_at.desc(), ConsentRow.id.desc())
        )
        rows = (await session.scalars(stmt)).all()
        return [self._to_domain_consent(row) for row in rows]

    async def record_consent(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        consent_kind: ConsentKind,
        policy_version: str,
        granted: bool,
        now: datetime,
    ) -> ConsentRecord:
        active_stmt = (
            select(ConsentRow)
            .where(
                ConsentRow.user_id == scope.user_id,
                ConsentRow.consent_kind == consent_kind.value,
                ConsentRow.revoked_at.is_(None),
            )
            .order_by(ConsentRow.granted_at.desc(), ConsentRow.id.desc())
            .with_for_update()
        )
        active_rows = list((await session.scalars(active_stmt)).all())

        if granted:
            if (
                len(active_rows) == 1
                and active_rows[0].policy_version == policy_version
                and active_rows[0].granted
            ):
                active_rows[0].granted_at = now
                await session.flush()
                return self._to_domain_consent(active_rows[0])

            for existing in active_rows:
                existing.revoked_at = now

            new_row = ConsentRow(
                id=new_id(),
                user_id=scope.user_id,
                consent_kind=consent_kind.value,
                policy_version=policy_version,
                granted=True,
                granted_at=now,
                revoked_at=None,
            )
            session.add(new_row)
            await session.flush()
            return self._to_domain_consent(new_row)

        if active_rows:
            latest = active_rows[0]
            latest.granted = False
            latest.policy_version = policy_version
            latest.revoked_at = now
            for extra in active_rows[1:]:
                extra.revoked_at = now
            await session.flush()
            return self._to_domain_consent(latest)

        revoked_row = ConsentRow(
            id=new_id(),
            user_id=scope.user_id,
            consent_kind=consent_kind.value,
            policy_version=policy_version,
            granted=False,
            granted_at=now,
            revoked_at=now,
        )
        session.add(revoked_row)
        await session.flush()
        return self._to_domain_consent(revoked_row)

    @staticmethod
    def _to_domain_consent(row: ConsentRow) -> ConsentRecord:
        return ConsentRecord(
            id=row.id,
            user_id=row.user_id,
            consent_kind=ConsentKind(row.consent_kind),
            policy_version=row.policy_version,
            granted=row.granted,
            granted_at=row.granted_at,
            revoked_at=row.revoked_at,
        )
