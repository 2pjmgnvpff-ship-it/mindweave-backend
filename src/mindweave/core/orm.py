"""Canonical SQLAlchemy 2.0 ORM row definitions for all MindWeave tables.

Domain layers (`mindweave.modules.*.domain`) MUST NOT import this module (enforced by
`.importlinter`); only `infra` repositories, migrations, and architecture tests may import it.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mindweave.core.db import Base
from mindweave.shared.data_rights_registry import DataTableRegistration, data_rights_registry

# ============================================================================
# System / Auth Bootstrap Tables (queried via system_session)
# ============================================================================


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthIdentityRow(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject_hmac", name="uq_auth_identities_provider_subject"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False)  # apple | email
    subject_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    contact_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailOtpChallengeRow(Base):
    __tablename__ = "email_otp_challenges"
    __table_args__ = (Index("ix_email_otp_lookup", "email_hmac", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    email_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    otp_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False, default="login")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RefreshTokenRow(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    replaced_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BackgroundJobRow(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (Index("ix_background_jobs_claim", "status", "run_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    task_name: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_actor_time", "actor_user_hash", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    actor_user_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ============================================================================
# User-Scoped Tables (ALL protected by PostgreSQL Row-Level Security!)
# ============================================================================


class UserKeyRow(Base):
    __tablename__ = "user_keys"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kek_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPreferenceRow(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    preferred_edition: Mapped[str] = mapped_column(String(24), nullable=False, default="cotton")
    cupid_layout: Mapped[str] = mapped_column(String(8), nullable=False, default="a")
    wallpaper_preset: Mapped[str] = mapped_column(String(48), nullable=False, default="warm_paper")
    wallpaper_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    wallpaper_blur: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    wallpaper_vignette: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    app_lock_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    late_night_care_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConsentRow(Base):
    __tablename__ = "consents"
    __table_args__ = (Index("ix_consents_user_kind", "user_id", "consent_kind"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    consent_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceRow(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("user_id", "platform", "token_hmac", name="uq_devices_user_token"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(24), nullable=False)
    push_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    device_label_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonRow(Base):
    __tablename__ = "persons"
    __table_args__ = (Index("ix_persons_user_tier_rank", "user_id", "tier", "rank"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    rank: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    display_name_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    monogram: Mapped[str] = mapped_column(String(8), nullable=False)
    role_tag_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    context_sub_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    short_memo_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    traits_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    avatar_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_photo_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sub_photo_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    visual_tokens: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonAliasRow(Base):
    __tablename__ = "person_aliases"
    __table_args__ = (
        UniqueConstraint("user_id", "person_id", "alias_hmac", name="uq_person_alias_per_person"),
        Index("ix_person_aliases_lookup", "user_id", "alias_hmac"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    alias_hmac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    alias_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="nickname")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CaptureRow(Base):
    __tablename__ = "captures"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_captures_user_idem"),
        Index("ix_captures_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    route_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    scoped_person_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="received")
    audio_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    keep_original_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_text_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    transcript_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    transcript_marked_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    splits_summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    safety_category: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    model_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    undo_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecordRow(Base):
    __tablename__ = "records"
    __table_args__ = (
        Index("ix_records_user_person_created", "user_id", "person_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    capture_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("captures.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # photo | note | voice
    title_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    snippet_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    media_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_offset_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stamp_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    happened_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    review_state: Mapped[str] = mapped_column(String(24), nullable=False, default="normal")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FactRow(Base):
    __tablename__ = "facts"
    __table_args__ = (Index("ix_facts_ctx", "user_id", "person_id", "status"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=True
    )
    record_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=True
    )
    capture_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("captures.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    time_status: Mapped[str] = mapped_column(String(16), nullable=False, default="current")
    source_speaker: Mapped[str] = mapped_column(String(24), nullable=False, default="narrator")
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    trigger_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    trigger_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    is_lunar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lunar_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lunar_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lunar_is_leap_month: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    pending_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # D1 pencil draft: confirmed date/promise the user should glance at once
    needs_glance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False, default="ai")
    valid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InboxItemRow(Base):
    __tablename__ = "inbox_items"
    __table_args__ = (
        Index("ix_inbox_user_status", "user_id", "status", "created_at"),
        Index(
            "ix_inbox_auto_ink_due",
            "status",
            "auto_ink_at",
            postgresql_where=text("auto_ink_at IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=True
    )
    capture_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("captures.id", ondelete="CASCADE"), nullable=True
    )
    record_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    fact_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    candidate_person_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    payload_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    resolution_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # D1 pencil draft: pending item auto-promotes to ink at this instant (never for venting)
    auto_ink_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReminderRow(Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_due", "status", "next_fire_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    person_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    fact_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )
    title_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    rule: Mapped[str] = mapped_column(String(16), nullable=False, default="once")
    is_lunar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_leap_month: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    once_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fire_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    fire_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lead_days: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False, default=list)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_lead_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReminderDeliveryRow(Base):
    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "reminder_id",
            "scheduled_for",
            "lead_day",
            name="uq_reminder_delivery_fire_slot",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reminder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lead_day: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatThreadRow(Base):
    __tablename__ = "chat_threads"
    __table_args__ = (UniqueConstraint("user_id", "person_id", name="uq_chat_thread_person"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    rolling_summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_user_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_person_time", "user_id", "person_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    whisper_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    perspectives_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    suggestions_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    referenced_record_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    tier_snapshot: Mapped[str] = mapped_column(String(16), nullable=False)
    safety_category: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    has_unrecorded_gap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    client_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatCitationRow(Base):
    __tablename__ = "chat_citations"
    __table_args__ = (Index("ix_chat_citations_msg", "message_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    record_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    citation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AiCallRow(Base):
    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    task: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_micros_cny: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExportJobRow(Base):
    __tablename__ = "export_jobs"
    __table_args__ = (Index("ix_export_jobs_user_created", "user_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    archive_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelemetryEventRow(Base):
    __tablename__ = "telemetry_events"
    __table_args__ = (Index("ix_telemetry_user_time", "user_id", "occurred_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_name: Mapped[str] = mapped_column(String(48), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idem_key", "method", "path", name="uq_idempotency_user_key_route"
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idem_key: Mapped[str] = mapped_column(String(128), nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="in_progress")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Canonical list of RLS-protected user-scoped tables
RLS_PROTECTED_TABLES: tuple[str, ...] = (
    "user_keys",
    "user_preferences",
    "consents",
    "devices",
    "persons",
    "person_aliases",
    "captures",
    "records",
    "facts",
    "inbox_items",
    "reminders",
    "reminder_deliveries",
    "chat_threads",
    "chat_messages",
    "chat_citations",
    "ai_calls",
    "export_jobs",
    "telemetry_events",
    "idempotency_keys",
)

SYSTEM_SCOPED_TABLES: tuple[str, ...] = (
    "users",
    "auth_identities",
    "email_otp_challenges",
    "refresh_tokens",
    "background_jobs",
    "audit_log",
)


def _register_all_user_tables() -> None:
    entries = [
        DataTableRegistration("auth_identities", "identity", "identities", "cascade_from_users"),
        DataTableRegistration(
            "refresh_tokens", "identity", None, "cascade_from_users", "Session credentials only"
        ),
        DataTableRegistration(
            "user_keys",
            "identity",
            None,
            "crypto_shred",
            "Per-user wrapped DEK destroyed on deletion",
        ),
        DataTableRegistration(
            "user_preferences", "preferences", "preferences", "cascade_from_users"
        ),
        DataTableRegistration("consents", "identity", "consents", "cascade_from_users"),
        DataTableRegistration("devices", "reminders", "devices", "cascade_from_users"),
        DataTableRegistration("persons", "people", "persons", "cascade_from_users"),
        DataTableRegistration("person_aliases", "people", "personAliases", "cascade_from_users"),
        DataTableRegistration("captures", "capture", "captures", "cascade_from_users"),
        DataTableRegistration("records", "memory", "materials", "cascade_from_users"),
        DataTableRegistration("facts", "memory", "facts", "cascade_from_users"),
        DataTableRegistration("inbox_items", "memory", "inboxItems", "cascade_from_users"),
        DataTableRegistration("reminders", "reminders", "reminders", "cascade_from_users"),
        DataTableRegistration(
            "reminder_deliveries", "reminders", "reminderDeliveries", "cascade_from_users"
        ),
        DataTableRegistration("chat_threads", "companion", "chatThreads", "cascade_from_users"),
        DataTableRegistration("chat_messages", "companion", "chatMessages", "cascade_from_users"),
        DataTableRegistration("chat_citations", "companion", "chatCitations", "cascade_from_users"),
        DataTableRegistration("ai_calls", "ai", "aiUsageSummary", "cascade_from_users"),
        DataTableRegistration(
            "export_jobs",
            "data_rights",
            None,
            "cascade_from_users",
            "Export job metadata & zip blobs",
        ),
        DataTableRegistration(
            "telemetry_events", "telemetry", "telemetryEvents", "cascade_from_users"
        ),
        DataTableRegistration(
            "idempotency_keys",
            "core",
            None,
            "cascade_from_users",
            "24h request deduplication cache",
        ),
    ]
    for entry in entries:
        data_rights_registry.register(entry)


_register_all_user_tables()
