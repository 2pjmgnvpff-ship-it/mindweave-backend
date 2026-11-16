"""pencil drafts: facts.needs_glance + inbox_items.auto_ink_at (D1)

Revision ID: 0002_pencil_drafts
Revises: 0001_baseline
Create Date: 2026-11-16 19:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_pencil_drafts"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Confirmed date/promise facts the user should glance at once ("pencil" date capsule).
    op.add_column(
        "facts",
        sa.Column("needs_glance", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # Pending pencil drafts (#7 speaker unknown / #8 polarity flip / low confidence)
    # auto-promote to ink at this instant unless the user acted first. Never set for venting.
    op.add_column(
        "inbox_items",
        sa.Column("auto_ink_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_inbox_auto_ink_due",
        "inbox_items",
        ["status", "auto_ink_at"],
        unique=False,
        postgresql_where=sa.text("auto_ink_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_auto_ink_due", table_name="inbox_items")
    op.drop_column("inbox_items", "auto_ink_at")
    op.drop_column("facts", "needs_glance")
