"""SQLAlchemy repository helpers for the Capture pipeline."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.orm import (
    CaptureRow,
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
)
from mindweave.modules.capture.domain.routing import KnownPersonProfile
from mindweave.modules.memory.infra.reminders_helper import cancel_reminders_for_facts
from mindweave.modules.memory.infra.repository import (
    create_person_with_primary_alias,
    decrypt_person_display_name,
    decrypt_person_role_tag,
)
from mindweave.shared.enums import (
    CaptureStatus,
    FactStatus,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PersonState,
    Tier,
)
from mindweave.shared.scopes import UserScope


async def load_known_person_profiles(
    session: AsyncSession,
    *,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
) -> tuple[list[KnownPersonProfile], dict[UUID, PersonRow]]:
    """Load all non-deleted, non-placeholder persons and decrypted aliases for a user."""
    person_rows = (
        await session.scalars(
            select(PersonRow)
            .where(
                PersonRow.user_id == scope.user_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.state != PersonState.ARCHIVED.value,
            )
            .order_by(PersonRow.created_at.asc())
        )
    ).all()

    real_rows = [p for p in person_rows if p.slug != "__unassigned__"]
    if not real_rows:
        return [], {}

    alias_rows = (
        await session.scalars(
            select(PersonAliasRow).where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id.in_([p.id for p in real_rows]),
            )
        )
    ).all()

    aliases_by_person: dict[UUID, list[str]] = defaultdict(list)
    for alias in alias_rows:
        decrypted_alias = cipher.decrypt_str(
            dek,
            alias.alias_enc,
            user_id=scope.user_id,
            table="person_aliases",
            column="alias_enc",
            row_id=alias.id,
        )
        if decrypted_alias and decrypted_alias not in aliases_by_person[alias.person_id]:
            aliases_by_person[alias.person_id].append(decrypted_alias)

    profiles: list[KnownPersonProfile] = []
    rows_by_id: dict[UUID, PersonRow] = {}
    for row in real_rows:
        rows_by_id[row.id] = row
        display_name = decrypt_person_display_name(cipher, dek, row)
        role_tag = decrypt_person_role_tag(cipher, dek, row)
        profiles.append(
            KnownPersonProfile(
                person_id=row.id,
                display_name=display_name,
                aliases=tuple(aliases_by_person.get(row.id, ())),
                tier=Tier(row.tier),
                state=PersonState(row.state),
                role_tag=role_tag,
                monogram=row.monogram,
            )
        )
    return profiles, rows_by_id


async def get_or_create_unassigned_placeholder(
    session: AsyncSession,
    *,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
    now: datetime,
) -> PersonRow:
    existing = await session.scalar(
        select(PersonRow).where(
            PersonRow.user_id == scope.user_id,
            PersonRow.slug == "__unassigned__",
            PersonRow.deleted_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    return await create_person_with_primary_alias(
        session,
        scope=scope,
        dek=dek,
        cipher=cipher,
        display_name="待归档",
        tier=Tier.CRYSTAL,
        state=PersonState.PROVISIONAL,
        now=now,
        slug="__unassigned__",
    )


async def revert_capture_graph(
    session: AsyncSession,
    *,
    scope: UserScope,
    capture: CaptureRow,
    now: datetime,
) -> tuple[int, int, int]:
    """Atomically revert capture, reject facts, restore superseded facts, soft-delete records, resolve inbox, cancel reminders."""
    capture.status = CaptureStatus.REVERTED.value

    facts = list(
        (
            await session.scalars(
                select(FactRow)
                .where(
                    FactRow.user_id == scope.user_id,
                    FactRow.capture_id == capture.id,
                )
                .with_for_update()
            )
        ).all()
    )
    fact_ids = [f.id for f in facts]

    # Restore any older confirmed facts that were bi-temporally superseded by these facts
    if fact_ids:
        superseded_old = (
            await session.scalars(
                select(FactRow)
                .where(
                    FactRow.user_id == scope.user_id,
                    FactRow.superseded_by_id.in_(fact_ids),
                )
                .with_for_update()
            )
        ).all()
        for old_fact in superseded_old:
            old_fact.invalid_at = None
            old_fact.time_status = "current"
            old_fact.superseded_by_id = None
            old_fact.updated_at = now
            old_fact.version += 1

    rejected_fact_count = 0
    for fact in facts:
        if fact.status != FactStatus.REJECTED.value:
            fact.status = FactStatus.REJECTED.value
            fact.invalid_at = now
            fact.updated_at = now
            fact.version += 1
            rejected_fact_count += 1

    records = list(
        (
            await session.scalars(
                select(RecordRow)
                .where(
                    RecordRow.user_id == scope.user_id,
                    RecordRow.capture_id == capture.id,
                    RecordRow.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
    )
    for rec in records:
        rec.deleted_at = now
        rec.updated_at = now
        rec.version += 1

    inbox_items = list(
        (
            await session.scalars(
                select(InboxItemRow)
                .where(
                    InboxItemRow.user_id == scope.user_id,
                    InboxItemRow.capture_id == capture.id,
                    InboxItemRow.status == InboxItemStatus.OPEN.value,
                )
                .with_for_update()
            )
        ).all()
    )
    provisional_person_ids_to_check: set[UUID] = set()
    for item in inbox_items:
        item.status = InboxItemStatus.RESOLVED.value
        item.resolution_action = InboxResolutionAction.DISCARD.value
        item.resolved_at = now
        if item.kind == InboxItemKind.NEW_PERSON_CONFIRM.value and item.person_id is not None:
            provisional_person_ids_to_check.add(item.person_id)

    for rec in records:
        provisional_person_ids_to_check.add(rec.person_id)

    # Archive any provisional / placeholder person created solely by this reverted capture
    for pid in provisional_person_ids_to_check:
        person = await session.scalar(
            select(PersonRow).where(
                PersonRow.id == pid,
                PersonRow.user_id == scope.user_id,
                PersonRow.state == PersonState.PROVISIONAL.value,
                PersonRow.deleted_at.is_(None),
            )
        )
        if person is not None:
            other_recs = (
                await session.scalars(
                    select(RecordRow.id).where(
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == pid,
                        RecordRow.deleted_at.is_(None),
                    )
                )
            ).all()
            if not other_recs:
                person.state = PersonState.ARCHIVED.value
                person.archived_at = now
                person.deleted_at = now
                person.updated_at = now

    cancelled_reminders = await cancel_reminders_for_facts(
        session,
        user_id=scope.user_id,
        fact_ids=fact_ids,
        now=now,
    )
    await session.flush()
    return len(records), rejected_fact_count, cancelled_reminders
