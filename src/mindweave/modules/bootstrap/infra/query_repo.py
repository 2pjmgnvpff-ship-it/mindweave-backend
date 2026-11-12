"""Single-round-trip SQLAlchemy query adapter for V10 Workspace Bootstrap."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.orm import InboxItemRow, PersonRow, RecordRow, UserPreferenceRow
from mindweave.modules.bootstrap.domain.formatting import (
    MaterialPreview,
    format_duration_ms,
    sanitize_material_preview_text,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    CupidLayout,
    Edition,
    InboxItemStatus,
    PersonState,
    RecordKind,
    RecordReviewState,
)
from mindweave.shared.scopes import UserScope

TOP_MATERIALS_PER_PERSON = 10


class BootstrapQueryRepository:
    """Fetches preferences, active/provisional persons, top-10 materials, and open inbox count."""

    def __init__(self, cipher: EnvelopeCipher) -> None:
        self._cipher = cipher

    async def get_or_provision_preferences(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        default_timezone: str,
        now: datetime,
    ) -> UserPreferenceRow:
        row = await session.get(UserPreferenceRow, scope.user_id)
        if row is not None:
            return row

        stmt = (
            pg_insert(UserPreferenceRow)
            .values(
                user_id=scope.user_id,
                preferred_edition=Edition.COTTON.value,
                cupid_layout=CupidLayout.A.value,
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
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        await session.execute(stmt)
        await session.flush()
        provisioned = await session.get(UserPreferenceRow, scope.user_id)
        assert provisioned is not None
        return provisioned

    async def list_workspace_person_rows(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> list[PersonRow]:
        stmt = (
            select(PersonRow)
            .where(
                PersonRow.user_id == scope.user_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.archived_at.is_(None),
                PersonRow.state != PersonState.ARCHIVED.value,
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
            )
            .order_by(PersonRow.rank.asc(), PersonRow.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def load_top_materials_by_person(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        person_ids: Sequence[UUID],
        *,
        per_person_limit: int = TOP_MATERIALS_PER_PERSON,
    ) -> dict[UUID, list[MaterialPreview]]:
        if not person_ids:
            return {}

        rn_col = (
            func.row_number()
            .over(
                partition_by=RecordRow.person_id,
                order_by=(RecordRow.created_at.desc(), RecordRow.id.desc()),
            )
            .label("rn")
        )
        ranked_subq = (
            select(RecordRow.id.label("rec_id"), rn_col)
            .where(
                RecordRow.user_id == scope.user_id,
                RecordRow.person_id.in_(person_ids),
                RecordRow.deleted_at.is_(None),
                RecordRow.review_state != RecordReviewState.HELD.value,
            )
            .subquery()
        )
        stmt = (
            select(RecordRow)
            .join(ranked_subq, RecordRow.id == ranked_subq.c.rec_id)
            .where(ranked_subq.c.rn <= per_person_limit)
            .order_by(
                RecordRow.person_id.asc(),
                RecordRow.created_at.desc(),
                RecordRow.id.desc(),
            )
        )

        grouped: dict[UUID, list[MaterialPreview]] = {pid: [] for pid in person_ids}
        for rec in (await session.scalars(stmt)).all():
            title = self._cipher.decrypt_str(
                dek,
                rec.title_enc,
                user_id=scope.user_id,
                table="records",
                column="title_enc",
                row_id=rec.id,
            )
            body = self._cipher.decrypt_str(
                dek,
                rec.body_enc,
                user_id=scope.user_id,
                table="records",
                column="body_enc",
                row_id=rec.id,
            )
            snippet = (
                self._cipher.decrypt_str(
                    dek,
                    rec.snippet_enc,
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec.id,
                )
                if rec.snippet_enc is not None
                else ""
            )
            grouped.setdefault(rec.person_id, []).append(
                MaterialPreview(
                    id=rec.id,
                    kind=RecordKind(rec.kind),
                    title=sanitize_material_preview_text(title),
                    body=body,
                    snippet=sanitize_material_preview_text(snippet),
                    dur=format_duration_ms(rec.audio_duration_ms),
                    stamp_code=rec.stamp_code,
                    happened_on=rec.happened_on,
                    media_object_key=rec.media_object_key,
                )
            )
        return grouped

    async def count_open_inbox_items(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(InboxItemRow)
            .where(
                InboxItemRow.user_id == scope.user_id,
                InboxItemRow.status == InboxItemStatus.OPEN.value,
            )
        )
        count_val = await session.scalar(stmt)
        return int(count_val or 0)
