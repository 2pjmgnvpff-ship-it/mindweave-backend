"""SQLAlchemy repository and envelope-encryption helpers for Memory & Pending Inbox."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher, normalize_alias_for_index
from mindweave.core.dto import decode_cursor, encode_cursor, strip_forbidden_dev_jargon
from mindweave.core.errors import NotFoundError, ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    UserPreferenceRow,
)
from mindweave.modules.capture.domain.routing import derive_monogram
from mindweave.modules.memory.domain.bitemporal import (
    ExistingFactSnapshot,
    NewFactCandidate,
    detect_polarity_flip,
    find_facts_to_supersede,
)
from mindweave.modules.memory.infra.reminders_helper import cancel_reminders_for_facts
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PersonState,
    Polarity,
    RecordReviewState,
    Tier,
)
from mindweave.shared.lexorank import initial_rank, rank_after
from mindweave.shared.scopes import UserScope


def parse_created_cursor(cursor: str) -> tuple[datetime, UUID]:
    data = decode_cursor(cursor)
    try:
        ts_raw = str(data["ts"])
        id_raw = str(data["id"])
        dt = datetime.fromisoformat(ts_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        uid = UUID(id_raw)
        return dt, uid
    except Exception as exc:
        raise ValidationFailedError("Invalid pagination cursor") from exc


def build_created_cursor(created_at: datetime, row_id: UUID) -> str:
    return encode_cursor({"ts": created_at.isoformat(), "id": str(row_id)})


async def get_user_timezone(session: AsyncSession, scope: UserScope) -> str:
    tz = await session.scalar(
        select(UserPreferenceRow.timezone).where(UserPreferenceRow.user_id == scope.user_id)
    )
    return tz or "Asia/Shanghai"


async def require_person_row(
    session: AsyncSession,
    scope: UserScope,
    person_id: UUID,
) -> PersonRow:
    row = await session.scalar(
        select(PersonRow).where(
            PersonRow.id == person_id,
            PersonRow.user_id == scope.user_id,
            PersonRow.deleted_at.is_(None),
            PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
        )
    )
    if row is None:
        raise NotFoundError("Person not found")
    return row


def decrypt_person_display_name(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: PersonRow,
) -> str:
    return cipher.decrypt_str(
        dek,
        row.display_name_enc,
        user_id=row.user_id,
        table="persons",
        column="display_name_enc",
        row_id=row.id,
    )


def decrypt_person_role_tag(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: PersonRow,
) -> str:
    if row.role_tag_enc is None:
        return ""
    return cipher.decrypt_str(
        dek,
        row.role_tag_enc,
        user_id=row.user_id,
        table="persons",
        column="role_tag_enc",
        row_id=row.id,
    )


def decrypt_record_fields(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: RecordRow,
) -> tuple[str, str, str]:
    title = cipher.decrypt_str(
        dek,
        row.title_enc,
        user_id=row.user_id,
        table="records",
        column="title_enc",
        row_id=row.id,
    )
    body = cipher.decrypt_str(
        dek,
        row.body_enc,
        user_id=row.user_id,
        table="records",
        column="body_enc",
        row_id=row.id,
    )
    snippet = (
        cipher.decrypt_str(
            dek,
            row.snippet_enc,
            user_id=row.user_id,
            table="records",
            column="snippet_enc",
            row_id=row.id,
        )
        if row.snippet_enc is not None
        else body[:60]
    )
    return (
        strip_forbidden_dev_jargon(title),
        strip_forbidden_dev_jargon(body),
        strip_forbidden_dev_jargon(snippet),
    )


def _sanitize_meta_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        raise ValidationFailedError("cardMeta nesting depth exceeds limit")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise ValidationFailedError("Null bytes are not permitted in cardMeta")
        return strip_forbidden_dev_jargon(value[:2000])
    if isinstance(value, list):
        if len(value) > 64:
            raise ValidationFailedError("cardMeta array exceeds 64 items")
        return [_sanitize_meta_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 32:
            raise ValidationFailedError("cardMeta object exceeds 32 keys")
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k).strip()
            if not key or len(key) > 64 or "\x00" in key:
                raise ValidationFailedError("Invalid key in cardMeta")
            cleaned[key] = _sanitize_meta_value(v, depth=depth + 1)
        return cleaned
    raise ValidationFailedError("Unsupported value type in cardMeta")


def sanitize_card_meta(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and sanitize structured card metadata before AES-256-GCM encryption."""
    if not raw:
        return {}
    result = _sanitize_meta_value(raw, depth=0)
    assert isinstance(result, dict)
    return result


def decrypt_record_card_meta(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: RecordRow,
) -> dict[str, Any]:
    if row.card_meta_enc is None:
        return {}
    raw = cipher.decrypt_json(
        dek,
        row.card_meta_enc,
        user_id=row.user_id,
        table="records",
        column="card_meta_enc",
        row_id=row.id,
    )
    return raw if isinstance(raw, dict) else {}


def decrypt_fact_content(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: FactRow,
) -> str:
    raw = cipher.decrypt_str(
        dek,
        row.content_enc,
        user_id=row.user_id,
        table="facts",
        column="content_enc",
        row_id=row.id,
    )
    return strip_forbidden_dev_jargon(raw)


def decrypt_inbox_payload(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: InboxItemRow,
) -> dict[str, Any]:
    raw = cipher.decrypt_json(
        dek,
        row.payload_enc,
        user_id=row.user_id,
        table="inbox_items",
        column="payload_enc",
        row_id=row.id,
    )
    return raw if isinstance(raw, dict) else {}


async def next_rank_for_tier(
    session: AsyncSession,
    scope: UserScope,
    tier: Tier,
) -> str:
    ranks = (
        await session.scalars(
            select(PersonRow.rank)
            .where(
                PersonRow.user_id == scope.user_id,
                PersonRow.tier == tier.value,
                PersonRow.deleted_at.is_(None),
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
            )
            .order_by(PersonRow.rank.asc())
        )
    ).all()
    if not ranks:
        return initial_rank()
    return rank_after(ranks[-1])


async def create_person_with_primary_alias(
    session: AsyncSession,
    *,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
    display_name: str,
    tier: Tier,
    state: PersonState,
    now: datetime,
    slug: str | None = None,
) -> PersonRow:
    cleaned_name = strip_forbidden_dev_jargon(display_name.strip()) or "未命名角色"
    person_id = new_id()
    rank = await next_rank_for_tier(session, scope, tier)
    name_enc = cipher.encrypt_str(
        dek,
        cleaned_name,
        user_id=scope.user_id,
        table="persons",
        column="display_name_enc",
        row_id=person_id,
    )
    person_row = PersonRow(
        id=person_id,
        user_id=scope.user_id,
        slug=slug,
        tier=tier.value,
        rank=rank,
        state=state.value,
        display_name_enc=name_enc,
        monogram=derive_monogram(cleaned_name),
        has_photo=False,
        visual_tokens={},
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(person_row)
    await session.flush()

    norm = normalize_alias_for_index(cleaned_name)
    if norm and slug != UNASSIGNED_PERSON_SLUG:
        alias_id = new_id()
        alias_hmac = cipher.compute_blind_index(
            dek,
            user_id=scope.user_id,
            domain="alias",
            normalized_value=norm,
        )
        alias_enc = cipher.encrypt_str(
            dek,
            cleaned_name,
            user_id=scope.user_id,
            table="person_aliases",
            column="alias_enc",
            row_id=alias_id,
        )
        session.add(
            PersonAliasRow(
                id=alias_id,
                user_id=scope.user_id,
                person_id=person_id,
                alias_hmac=alias_hmac,
                alias_enc=alias_enc,
                kind="name",
                is_primary=True,
                created_at=now,
            )
        )
        await session.flush()

    return person_row


async def load_active_confirmed_fact_snapshots(
    session: AsyncSession,
    *,
    scope: UserScope,
    person_id: UUID,
    dek: bytes,
    cipher: EnvelopeCipher,
    exclude_fact_id: UUID | None = None,
) -> tuple[list[ExistingFactSnapshot], dict[UUID, FactRow]]:
    stmt = select(FactRow).where(
        FactRow.user_id == scope.user_id,
        FactRow.person_id == person_id,
        FactRow.status == FactStatus.CONFIRMED.value,
        FactRow.invalid_at.is_(None),
    )
    if exclude_fact_id is not None:
        stmt = stmt.where(FactRow.id != exclude_fact_id)

    rows = (await session.scalars(stmt)).all()
    snapshots: list[ExistingFactSnapshot] = []
    by_id: dict[UUID, FactRow] = {}
    for row in rows:
        content = decrypt_fact_content(cipher, dek, row)
        by_id[row.id] = row
        snapshots.append(
            ExistingFactSnapshot(
                id=row.id,
                user_id=row.user_id,
                person_id=person_id,
                fact_type=FactType(row.type),
                polarity=Polarity(row.polarity),
                content=content,
                status=FactStatus(row.status),
                invalid_at=row.invalid_at,
                trigger_date=row.trigger_date,
            )
        )
    return snapshots, by_id


async def check_polarity_flip_for_person(
    session: AsyncSession,
    *,
    scope: UserScope,
    person_id: UUID,
    fact_type: FactType,
    polarity: Polarity,
    content: str,
    dek: bytes,
    cipher: EnvelopeCipher,
) -> bool:
    snapshots, _ = await load_active_confirmed_fact_snapshots(
        session,
        scope=scope,
        person_id=person_id,
        dek=dek,
        cipher=cipher,
    )
    candidate = NewFactCandidate(
        user_id=scope.user_id,
        person_id=person_id,
        fact_type=fact_type,
        polarity=polarity,
        content=content,
    )
    return detect_polarity_flip(snapshots, candidate)


async def apply_bitemporal_supersession_for_fact(
    session: AsyncSession,
    *,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
    new_fact: FactRow,
    new_fact_content: str,
    now: datetime,
    explicit_supersedes_id: UUID | None = None,
) -> list[UUID]:
    """Invalidate superseded active confirmed facts without overwriting history."""
    if new_fact.person_id is None or new_fact.status != FactStatus.CONFIRMED.value:
        return []

    snapshots, rows_by_id = await load_active_confirmed_fact_snapshots(
        session,
        scope=scope,
        person_id=new_fact.person_id,
        dek=dek,
        cipher=cipher,
        exclude_fact_id=new_fact.id,
    )
    if not snapshots:
        return []

    candidate = NewFactCandidate(
        user_id=scope.user_id,
        person_id=new_fact.person_id,
        fact_type=FactType(new_fact.type),
        polarity=Polarity(new_fact.polarity),
        content=new_fact_content,
        new_fact_id=new_fact.id,
        trigger_date=new_fact.trigger_date,
        explicit_supersedes_id=explicit_supersedes_id,
    )
    patches = find_facts_to_supersede(snapshots, candidate, now=now)
    superseded_ids: list[UUID] = []
    for patch in patches:
        old_row = rows_by_id.get(patch.superseded_fact_id)
        if old_row is None:
            continue
        old_row.invalid_at = patch.invalid_at
        old_row.time_status = patch.time_status.value
        old_row.superseded_by_id = patch.superseded_by_id
        old_row.updated_at = now
        old_row.version += 1
        superseded_ids.append(old_row.id)

    if superseded_ids:
        await session.flush()
    return superseded_ids


async def sync_capture_status_after_inbox_resolution(
    session: AsyncSession,
    *,
    scope: UserScope,
    capture_id: UUID | None,
    now: datetime,
) -> None:
    if capture_id is None:
        return
    capture = await session.scalar(
        select(CaptureRow).where(
            CaptureRow.id == capture_id,
            CaptureRow.user_id == scope.user_id,
        )
    )
    if capture is None or capture.status != "needs_review":
        return
    open_items = (
        await session.scalars(
            select(InboxItemRow.id).where(
                InboxItemRow.user_id == scope.user_id,
                InboxItemRow.capture_id == capture_id,
                InboxItemRow.status == InboxItemStatus.OPEN.value,
            )
        )
    ).all()
    if not open_items:
        capture.status = "completed"
        if capture.completed_at is None:
            capture.completed_at = now
        await session.flush()


async def merge_provisional_person_into_target(
    session: AsyncSession,
    *,
    scope: UserScope,
    source_person: PersonRow,
    target_person: PersonRow,
    current_inbox_id: UUID,
    now: datetime,
) -> None:
    """Merge a provisional person's aliases, records, facts, reminders, and inbox items into target."""
    # 1. Migrate non-duplicate aliases
    existing_hmacs = set(
        (
            await session.scalars(
                select(PersonAliasRow.alias_hmac).where(
                    PersonAliasRow.user_id == scope.user_id,
                    PersonAliasRow.person_id == target_person.id,
                )
            )
        ).all()
    )
    source_aliases = (
        await session.scalars(
            select(PersonAliasRow).where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id == source_person.id,
            )
        )
    ).all()
    for alias in source_aliases:
        if alias.alias_hmac in existing_hmacs:
            await session.delete(alias)
        else:
            alias.person_id = target_person.id
            alias.is_primary = False
            alias.kind = "nickname"
            existing_hmacs.add(alias.alias_hmac)
    await session.flush()

    # 2. Reassign records
    source_records = (
        await session.scalars(
            select(RecordRow).where(
                RecordRow.user_id == scope.user_id,
                RecordRow.person_id == source_person.id,
            )
        )
    ).all()
    for rec in source_records:
        rec.person_id = target_person.id
        if rec.review_state == RecordReviewState.HELD.value:
            rec.review_state = RecordReviewState.NORMAL.value
        rec.updated_at = now
        rec.version += 1

    # 3. Reassign facts
    source_facts = (
        await session.scalars(
            select(FactRow).where(
                FactRow.user_id == scope.user_id,
                FactRow.person_id == source_person.id,
            )
        )
    ).all()
    for fact in source_facts:
        fact.person_id = target_person.id
        fact.updated_at = now
        fact.version += 1

    # 4. Reassign reminders
    source_reminders = (
        await session.scalars(
            select(ReminderRow).where(
                ReminderRow.user_id == scope.user_id,
                ReminderRow.person_id == source_person.id,
            )
        )
    ).all()
    for rem in source_reminders:
        rem.person_id = target_person.id
        rem.updated_at = now
        rem.version += 1

    # 5. Reassign other inbox items
    other_inbox = (
        await session.scalars(
            select(InboxItemRow).where(
                InboxItemRow.user_id == scope.user_id,
                InboxItemRow.person_id == source_person.id,
                InboxItemRow.id != current_inbox_id,
            )
        )
    ).all()
    for item in other_inbox:
        item.person_id = target_person.id

    # 6. Soft-delete / archive merged provisional person
    source_person.state = PersonState.ARCHIVED.value
    source_person.archived_at = now
    source_person.deleted_at = now
    source_person.updated_at = now
    source_person.version += 1
    await session.flush()


async def expire_stale_venting_items(
    session: AsyncSession,
    scope_or_now: UserScope | datetime | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Auto-demote expired 72-hour `venting_hold` inbox items to `note_only`.

    Supports both:
    - `expire_stale_venting_items(user_session, scope, now=...)` (tenant-scoped)
    - `expire_stale_venting_items(system_session, now_dt)` (cron worker across tenants)
    """
    scope: UserScope | None = None
    if isinstance(scope_or_now, UserScope):
        scope = scope_or_now
        effective_now = now or datetime.now(UTC)
    elif isinstance(scope_or_now, datetime):
        effective_now = scope_or_now
    else:
        effective_now = now or datetime.now(UTC)

    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=UTC)

    stmt = (
        select(InboxItemRow)
        .where(
            InboxItemRow.kind == InboxItemKind.VENTING_HOLD.value,
            InboxItemRow.status == InboxItemStatus.OPEN.value,
            InboxItemRow.expires_at.is_not(None),
            InboxItemRow.expires_at <= effective_now,
        )
        .with_for_update(skip_locked=True)
    )
    if scope is not None:
        stmt = stmt.where(InboxItemRow.user_id == scope.user_id)

    expired_items: Sequence[InboxItemRow] = (await session.scalars(stmt)).all()
    if not expired_items:
        return 0

    for item in expired_items:
        item.status = InboxItemStatus.EXPIRED.value
        item.resolution_action = InboxResolutionAction.KEEP_AS_NOTE.value
        item.resolved_at = effective_now

        if item.record_id is not None:
            record = await session.scalar(
                select(RecordRow).where(
                    RecordRow.id == item.record_id,
                    RecordRow.user_id == item.user_id,
                    RecordRow.deleted_at.is_(None),
                )
            )
            if record is not None:
                record.review_state = RecordReviewState.NOTE_ONLY.value
                record.updated_at = effective_now
                record.version += 1

        if item.fact_ids:
            facts = (
                await session.scalars(
                    select(FactRow).where(
                        FactRow.user_id == item.user_id,
                        FactRow.id.in_(list(item.fact_ids)),
                        FactRow.status == FactStatus.PENDING.value,
                    )
                )
            ).all()
            for fact in facts:
                fact.status = FactStatus.REJECTED.value
                fact.invalid_at = effective_now
                fact.updated_at = effective_now
                fact.version += 1

            await cancel_reminders_for_facts(
                session,
                user_id=item.user_id,
                fact_ids=item.fact_ids,
                now=effective_now,
            )

    await session.flush()
    return len(expired_items)


def apply_created_cursor_filter(
    stmt: Any,
    model: type[RecordRow] | type[FactRow] | type[InboxItemRow],
    cursor: str | None,
) -> Any:
    if not cursor:
        return stmt
    cursor_dt, cursor_id = parse_created_cursor(cursor)
    return stmt.where(tuple_(model.created_at, model.id) < (cursor_dt, cursor_id))
