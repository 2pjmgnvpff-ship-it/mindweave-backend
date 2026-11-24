"""v2 email OTP delivery telemetry, non-null happened_on, covering journal indexes & optional encrypted embeddings.

Revision ID: 0003_v2_otp_happened_search
Revises: 0002_pencil_drafts
Create Date: 2026-11-23 14:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_v2_otp_happened_search"
down_revision: str | None = "0002_pencil_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Atomically relax FORCE RLS for table-owner backfill only inside Alembic's single tx.
    #    `mindweave_owner` owns `records` and `user_preferences` but has `NOBYPASSRLS`, so without
    #    toggling NO FORCE -> backfill -> FORCE, UPDATE would match 0 rows and crash SET NOT NULL.
    op.execute("ALTER TABLE records NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_preferences NO FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        UPDATE records r
        SET happened_on = (
            r.created_at AT TIME ZONE COALESCE(
                (
                    SELECT up.timezone
                    FROM user_preferences up
                    WHERE up.user_id = r.user_id
                      AND EXISTS (
                          SELECT 1 FROM pg_timezone_names ptn WHERE ptn.name = up.timezone
                      )
                ),
                'Asia/Shanghai'
            )
        )::date
        WHERE r.happened_on IS NULL
        """
    )

    # Restore FORCE ROW LEVEL SECURITY immediately inside the same transaction (Invariant I1).
    op.execute("ALTER TABLE user_preferences FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE records FORCE ROW LEVEL SECURITY")

    # 2. Enforce NOT NULL + safe SQL default so tuple keyset (<) never encounters SQL UNKNOWN.
    op.alter_column(
        "records",
        "happened_on",
        existing_type=sa.Date(),
        server_default=sa.text("CURRENT_DATE"),
        nullable=False,
    )

    # 3. Partial composite covering index matching /materials keyset, /timeline-buckets & 1ms search watermark.
    op.execute(
        """
        CREATE INDEX ix_records_user_person_happened_active
            ON records (user_id, person_id, happened_on DESC, created_at DESC, id DESC)
            INCLUDE (updated_at, review_state, kind, pinned)
            WHERE deleted_at IS NULL
        """
    )

    # 3b. Partial index for 1-query live splits[] hydration in GET /v1/captures.
    op.execute(
        """
        CREATE INDEX ix_records_user_capture_active
            ON records (user_id, capture_id)
            WHERE capture_id IS NOT NULL AND deleted_at IS NULL
        """
    )

    # 4. Optional Layer-2 DEK-encrypted float16 semantic vector columns on existing RLS table.
    op.add_column("records", sa.Column("embedding_enc", sa.LargeBinary(), nullable=True))
    op.add_column("records", sa.Column("embedding_model", sa.String(length=64), nullable=True))

    # 5. Email OTP delivery telemetry columns on non-RLS email_otp_challenges (zero PII).
    op.add_column(
        "email_otp_challenges",
        sa.Column("delivery_provider", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "email_otp_challenges",
        sa.Column(
            "delivery_status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'dev_bypass'"),
        ),
    )
    op.add_column(
        "email_otp_challenges",
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "email_otp_challenges",
        sa.Column("delivery_error_code", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        CREATE INDEX ix_email_otp_lookup_status
            ON email_otp_challenges (email_hmac, delivery_status, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_email_otp_lookup_status")
    op.drop_column("email_otp_challenges", "delivery_error_code")
    op.drop_column("email_otp_challenges", "provider_message_id")
    op.drop_column("email_otp_challenges", "delivery_status")
    op.drop_column("email_otp_challenges", "delivery_provider")

    op.drop_column("records", "embedding_model")
    op.drop_column("records", "embedding_enc")

    op.execute("DROP INDEX IF EXISTS ix_records_user_capture_active")
    op.execute("DROP INDEX IF EXISTS ix_records_user_person_happened_active")

    op.alter_column(
        "records",
        "happened_on",
        existing_type=sa.Date(),
        server_default=None,
        nullable=True,
    )
