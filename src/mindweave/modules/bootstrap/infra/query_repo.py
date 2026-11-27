"""Single-round-trip SQLAlchemy query adapter for V10 Workspace Bootstrap."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.orm import (
    FactRow,
    InboxItemRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    UserPreferenceRow,
)
from mindweave.modules.bootstrap.domain.formatting import (
    FactPreview,
    MaterialPreview,
    ReminderPreview,
    format_duration_ms,
    sanitize_material_preview_text,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    CupidLayout,
    Edition,
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    PersonState,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderRule,
    ReminderStatus,
)
from mindweave.shared.scopes import UserScope

TOP_MATERIALS_PER_PERSON = 10
TOP_FACTS_PER_PERSON = 12
TOP_REMINDERS_PER_PERSON = 6


class BootstrapQueryRepository:
    """Fetches preferences, active/provisional persons, top-10 materials, facts, reminders & inbox."""

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
            raw_meta = (
                self._cipher.decrypt_json(
                    dek,
                    rec.card_meta_enc,
                    user_id=scope.user_id,
                    table="records",
                    column="card_meta_enc",
                    row_id=rec.id,
                )
                if rec.card_meta_enc is not None
                else {}
            )
            card_meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
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
                    is_favorite=rec.is_favorite,
                    card_meta=card_meta,
                    review_state=RecordReviewState(rec.review_state),
                    version=rec.version,
                )
            )
        return grouped

    async def load_facts_by_person(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        person_ids: Sequence[UUID],
        *,
        per_person_limit: int = TOP_FACTS_PER_PERSON,
    ) -> dict[UUID, list[FactPreview]]:
        if not person_ids:
            return {}

        stmt = (
            select(FactRow)
            .outerjoin(
                RecordRow,
                and_(
                    FactRow.record_id == RecordRow.id,
                    RecordRow.user_id == scope.user_id,
                ),
            )
            .where(
                FactRow.user_id == scope.user_id,
                FactRow.person_id.in_(person_ids),
                FactRow.invalid_at.is_(None),
                FactRow.status.in_((FactStatus.CONFIRMED.value, FactStatus.PENDING.value)),
                or_(
                    FactRow.record_id.is_(None),
                    and_(
                        RecordRow.id.is_not(None),
                        RecordRow.deleted_at.is_(None),
                        RecordRow.review_state != RecordReviewState.HELD.value,
                    ),
                ),
            )
            .order_by(FactRow.person_id.asc(), FactRow.created_at.desc(), FactRow.id.desc())
        )

        grouped: dict[UUID, list[FactPreview]] = {pid: [] for pid in person_ids}
        for row in (await session.scalars(stmt)).all():
            if row.person_id is None:
                continue
            bucket = grouped.setdefault(row.person_id, [])
            if len(bucket) >= per_person_limit:
                continue
            # Pending items only surface in bootstrap if they are date/promise drafts (non-venting)
            if row.status == FactStatus.PENDING.value and row.type not in (
                FactType.DATE.value,
                FactType.PROMISE.value,
            ):
                continue
            content = self._cipher.decrypt_str(
                dek,
                row.content_enc,
                user_id=scope.user_id,
                table="facts",
                column="content_enc",
                row_id=row.id,
            )
            bucket.append(
                FactPreview(
                    id=row.id,
                    record_id=row.record_id,
                    type=FactType(row.type),
                    polarity=Polarity(row.polarity),
                    content=sanitize_material_preview_text(content),
                    status=FactStatus(row.status),
                    trigger_date=row.trigger_date,
                    is_lunar=row.is_lunar,
                    lunar_month=row.lunar_month,
                    lunar_day=row.lunar_day,
                    lunar_is_leap_month=row.lunar_is_leap_month,
                    version=row.version,
                )
            )
        return grouped

    async def load_upcoming_reminders_by_person(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        person_ids: Sequence[UUID],
        *,
        per_person_limit: int = TOP_REMINDERS_PER_PERSON,
    ) -> dict[UUID, list[ReminderPreview]]:
        if not person_ids:
            return {}

        stmt = (
            select(ReminderRow)
            .where(
                ReminderRow.user_id == scope.user_id,
                ReminderRow.person_id.in_(person_ids),
                ReminderRow.status.in_(
                    (ReminderStatus.ACTIVE.value, ReminderStatus.PROPOSED.value)
                ),
            )
            .order_by(
                ReminderRow.person_id.asc(),
                ReminderRow.next_fire_at.asc().nulls_last(),
                ReminderRow.created_at.desc(),
            )
        )

        grouped: dict[UUID, list[ReminderPreview]] = {pid: [] for pid in person_ids}
        for row in (await session.scalars(stmt)).all():
            bucket = grouped.setdefault(row.person_id, [])
            if len(bucket) >= per_person_limit:
                continue
            title = self._cipher.decrypt_str(
                dek,
                row.title_enc,
                user_id=scope.user_id,
                table="reminders",
                column="title_enc",
                row_id=row.id,
            )
            bucket.append(
                ReminderPreview(
                    id=row.id,
                    fact_id=row.fact_id,
                    title=sanitize_material_preview_text(title),
                    rule=ReminderRule(row.rule),
                    is_lunar=row.is_lunar,
                    target_month=row.target_month,
                    target_day=row.target_day,
                    is_leap_month=row.is_leap_month,
                    once_date=row.once_date,
                    lead_days=list(row.lead_days or []),
                    next_fire_at=row.next_fire_at,
                    status=ReminderStatus(row.status),
                    version=row.version,
                )
            )
        return grouped

    async def count_cooling_items_by_person(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_ids: Sequence[UUID],
    ) -> dict[UUID, int]:
        if not person_ids:
            return {}
        stmt = (
            select(InboxItemRow.person_id, func.count(InboxItemRow.id))
            .where(
                InboxItemRow.user_id == scope.user_id,
                InboxItemRow.person_id.in_(person_ids),
                InboxItemRow.status == InboxItemStatus.OPEN.value,
                InboxItemRow.kind == InboxItemKind.VENTING_HOLD.value,
            )
            .group_by(InboxItemRow.person_id)
        )
        rows = (await session.execute(stmt)).all()
        counts: dict[UUID, int] = {pid: 0 for pid in person_ids}
        for pid, cnt in rows:
            if pid is not None:
                counts[pid] = int(cnt or 0)
        return counts

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
