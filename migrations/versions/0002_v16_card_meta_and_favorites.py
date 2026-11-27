"""v16 record encrypted card metadata and favorite flag

Revision ID: 0002_v16_card_meta_favorites
Revises: 0001_baseline
Create Date: 2026-11-27 06:35:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_v16_card_meta_favorites"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("records", sa.Column("card_meta_enc", sa.LargeBinary(), nullable=True))
    op.add_column(
        "records",
        sa.Column(
            "is_favorite",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.drop_constraint("ck_records_kind", "records", type_="check")
    op.create_check_constraint(
        "ck_records_kind",
        "records",
        "kind IN ('photo', 'note', 'voice', 'chat', 'draft')",
    )


def downgrade() -> None:
    op.execute("UPDATE records SET kind = 'note' WHERE kind IN ('chat', 'draft')")
    op.drop_constraint("ck_records_kind", "records", type_="check")
    op.create_check_constraint(
        "ck_records_kind",
        "records",
        "kind IN ('photo', 'note', 'voice')",
    )
    op.drop_column("records", "is_favorite")
    op.drop_column("records", "card_meta_enc")
