"""SQLAlchemy repository helpers for the Capture pipeline."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
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
    decrypt_fact_content,
    decrypt_person_display_name,
    decrypt_person_role_tag,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    CaptureStatus,
    FactStatus,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PersonState,
    RecordReviewState,
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
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
            )
            .order_by(PersonRow.created_at.asc())
        )
    ).all()

    real_rows = [p for p in person_rows if p.slug != UNASSIGNED_PERSON_SLUG]
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


async def load_recent_person_context(
    session: AsyncSession,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
    person_ids: Sequence[UUID],
    max_facts_per_person: int = 5,
    max_snippets_total: int = 12,
) -> tuple[dict[UUID, list[str]], list[str]]:
    """Load encrypted confirmed recent facts per person and recent context snippets.

    Strictly scoped to `UserScope` (RLS + explicit `user_id == scope.user_id`), excluding:
    - deleted or archived persons (`PersonRow.deleted_at`, `state == 'archived'`)
    - non-confirmed facts (`status != 'confirmed'`)
    - bi-temporally superseded/invalidated facts (`invalid_at IS NOT NULL`)
    - facts backed by soft-deleted or held records (`RecordRow.deleted_at`, `review_state == 'held'`)
    """
    unique_person_ids = list(dict.fromkeys(person_ids))
    if not unique_person_ids or (max_facts_per_person <= 0 and max_snippets_total <= 0):
        return {}, []

    stmt = (
        select(FactRow)
        .join(
            PersonRow,
            and_(
                FactRow.person_id == PersonRow.id,
                PersonRow.user_id == scope.user_id,
            ),
        )
        .outerjoin(
            RecordRow,
            and_(
                FactRow.record_id == RecordRow.id,
                RecordRow.user_id == scope.user_id,
            ),
        )
        .where(
            FactRow.user_id == scope.user_id,
            FactRow.person_id.in_(unique_person_ids),
            FactRow.status == FactStatus.CONFIRMED.value,
            FactRow.invalid_at.is_(None),
            PersonRow.deleted_at.is_(None),
            PersonRow.state != PersonState.ARCHIVED.value,
            or_(
                FactRow.record_id.is_(None),
                and_(
                    RecordRow.id.is_not(None),
                    RecordRow.deleted_at.is_(None),
                    RecordRow.review_state != RecordReviewState.HELD.value,
                ),
            ),
        )
        .order_by(FactRow.created_at.desc(), FactRow.id.desc())
        .limit(max(len(unique_person_ids) * max(max_facts_per_person, 1) * 4, 200))
    )
    rows = (await session.scalars(stmt)).all()

    facts_by_person: dict[UUID, list[str]] = defaultdict(list)
    seen_per_person: dict[UUID, set[str]] = defaultdict(set)
    recent_snippets: list[str] = []
    seen_snippets: set[str] = set()

    for row in rows:
        if row.person_id is None:
            continue
        if (
            row.user_id != scope.user_id
            or row.status != FactStatus.CONFIRMED.value
            or row.invalid_at is not None
        ):
            continue
        content = decrypt_fact_content(cipher, dek, row).strip()
        if not content:
            continue
        compact = content[:120]
        if (
            max_facts_per_person > 0
            and len(facts_by_person[row.person_id]) < max_facts_per_person
            and compact not in seen_per_person[row.person_id]
        ):
            seen_per_person[row.person_id].add(compact)
            facts_by_person[row.person_id].append(compact)
        if (
            max_snippets_total > 0
            and len(recent_snippets) < max_snippets_total
            and compact not in seen_snippets
        ):
            seen_snippets.add(compact)
            recent_snippets.append(compact)

    return dict(facts_by_person), recent_snippets


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
            PersonRow.slug == UNASSIGNED_PERSON_SLUG,
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
        slug=UNASSIGNED_PERSON_SLUG,
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
    for fact in facts:
        if fact.person_id is not None:
            provisional_person_ids_to_check.add(fact.person_id)

    # Flush mutations first because session uses autoflush=False!
    await session.flush()

    # Archive & soft-delete any provisional / placeholder person created solely by this reverted capture
    cleaned_provisional = False
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
            other_facts = (
                await session.scalars(
                    select(FactRow.id).where(
                        FactRow.user_id == scope.user_id,
                        FactRow.person_id == pid,
                        FactRow.status != FactStatus.REJECTED.value,
                        FactRow.invalid_at.is_(None),
                    )
                )
            ).all()
            if not other_recs and not other_facts:
                person.state = PersonState.ARCHIVED.value
                person.archived_at = now
                person.deleted_at = now
                person.updated_at = now
                cleaned_provisional = True

    if cleaned_provisional:
        await session.flush()

    cancelled_reminders = await cancel_reminders_for_facts(
        session,
        user_id=scope.user_id,
        fact_ids=fact_ids,
        now=now,
    )
    await session.flush()
    return len(records), rejected_fact_count, cancelled_reminders
