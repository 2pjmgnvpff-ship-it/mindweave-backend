"""Tenant-isolated SQLAlchemy repositories for `devices`, `reminders`, and `reminder_deliveries`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.dto import decode_cursor, encode_cursor
from mindweave.core.errors import ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.core.orm import DeviceRow, PersonRow, ReminderDeliveryRow, ReminderRow
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    PersonState,
    PushPlatform,
    ReminderRule,
    ReminderStatus,
)
from mindweave.shared.scopes import UserScope


@dataclass(frozen=True, slots=True)
class DecryptedDevice:
    id: UUID
    user_id: UUID
    platform: PushPlatform
    device_label: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DecryptedDeviceTarget:
    id: UUID
    user_id: UUID
    platform: PushPlatform
    push_token: str


@dataclass(frozen=True, slots=True)
class DecryptedReminder:
    id: UUID
    user_id: UUID
    person_id: UUID
    fact_id: UUID | None
    title: str
    rule: ReminderRule
    is_lunar: bool
    target_month: int | None
    target_day: int | None
    is_leap_month: bool
    once_date: date | None
    fire_hour: int
    fire_minute: int
    lead_days: list[int]
    timezone: str
    next_fire_at: datetime | None
    next_lead_day: int | None
    status: ReminderStatus
    last_fired_at: datetime | None
    action_done_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


def _parse_cursor_pair(cursor: str) -> tuple[datetime, UUID]:
    raw = decode_cursor(cursor)
    ts_raw = raw.get("created_at")
    id_raw = raw.get("id")
    if not isinstance(ts_raw, str) or not isinstance(id_raw, str):
        raise ValidationFailedError("Malformed pagination cursor")
    try:
        return datetime.fromisoformat(ts_raw), UUID(id_raw)
    except ValueError as exc:
        raise ValidationFailedError("Malformed pagination cursor values") from exc


class DeviceRepository:
    """Encrypted device token storage using `push_token_enc` + `token_hmac` blind index."""

    def __init__(self, cipher: EnvelopeCipher) -> None:
        self._cipher = cipher

    async def upsert(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        dek: bytes,
        platform: PushPlatform,
        push_token: str,
        device_label: str | None,
        enabled: bool,
        now: datetime,
        requested_id: UUID | None = None,
    ) -> tuple[DecryptedDevice, bool]:
        normalized_token = push_token.strip()
        if not normalized_token:
            raise ValidationFailedError("pushToken must not be empty")

        cleaned_label = device_label.strip() if device_label and device_label.strip() else None
        token_hmac = self._cipher.compute_blind_index(
            dek,
            user_id=scope.user_id,
            domain="device_push_token",
            normalized_value=normalized_token,
        )

        existing = await session.scalar(
            select(DeviceRow)
            .where(
                DeviceRow.user_id == scope.user_id,
                DeviceRow.platform == platform.value,
                DeviceRow.token_hmac == token_hmac,
            )
            .with_for_update()
        )
        if existing is None and requested_id is not None:
            existing = await session.scalar(
                select(DeviceRow)
                .where(
                    DeviceRow.id == requested_id,
                    DeviceRow.user_id == scope.user_id,
                )
                .with_for_update()
            )

        if existing is not None:
            row_id = existing.id
            existing.platform = platform.value
            existing.token_hmac = token_hmac
            existing.push_token_enc = self._cipher.encrypt_str(
                dek,
                normalized_token,
                user_id=scope.user_id,
                table="devices",
                column="push_token_enc",
                row_id=row_id,
            )
            existing.device_label_enc = (
                self._cipher.encrypt_str(
                    dek,
                    cleaned_label,
                    user_id=scope.user_id,
                    table="devices",
                    column="device_label_enc",
                    row_id=row_id,
                )
                if cleaned_label is not None
                else None
            )
            existing.enabled = enabled
            existing.updated_at = now
            await session.flush()
            return self._to_decrypted(existing, scope, dek), False

        row_id = requested_id or new_id()
        row = DeviceRow(
            id=row_id,
            user_id=scope.user_id,
            platform=platform.value,
            push_token_enc=self._cipher.encrypt_str(
                dek,
                normalized_token,
                user_id=scope.user_id,
                table="devices",
                column="push_token_enc",
                row_id=row_id,
            ),
            token_hmac=token_hmac,
            device_label_enc=(
                self._cipher.encrypt_str(
                    dek,
                    cleaned_label,
                    user_id=scope.user_id,
                    table="devices",
                    column="device_label_enc",
                    row_id=row_id,
                )
                if cleaned_label is not None
                else None
            ),
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return self._to_decrypted(row, scope, dek), True

    async def list_for_user(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        dek: bytes,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[DecryptedDevice], str | None, bool]:
        stmt = select(DeviceRow).where(DeviceRow.user_id == scope.user_id)
        if cursor is not None:
            cursor_ts, cursor_id = _parse_cursor_pair(cursor)
            stmt = stmt.where(
                or_(
                    DeviceRow.created_at < cursor_ts,
                    and_(DeviceRow.created_at == cursor_ts, DeviceRow.id < cursor_id),
                )
            )
        stmt = stmt.order_by(DeviceRow.created_at.desc(), DeviceRow.id.desc()).limit(limit + 1)
        rows = list((await session.scalars(stmt)).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "created_at": page_rows[-1].created_at.isoformat(),
                    "id": str(page_rows[-1].id),
                }
            )
            if has_more and page_rows
            else None
        )
        return [self._to_decrypted(r, scope, dek) for r in page_rows], next_cursor, has_more

    async def list_enabled_targets(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        dek: bytes,
    ) -> list[DecryptedDeviceTarget]:
        stmt = (
            select(DeviceRow)
            .where(
                DeviceRow.user_id == scope.user_id,
                DeviceRow.enabled.is_(True),
            )
            .order_by(DeviceRow.created_at.asc(), DeviceRow.id.asc())
        )
        rows = (await session.scalars(stmt)).all()
        targets: list[DecryptedDeviceTarget] = []
        for row in rows:
            token = self._cipher.decrypt_str(
                dek,
                row.push_token_enc,
                user_id=scope.user_id,
                table="devices",
                column="push_token_enc",
                row_id=row.id,
            )
            targets.append(
                DecryptedDeviceTarget(
                    id=row.id,
                    user_id=row.user_id,
                    platform=PushPlatform(row.platform),
                    push_token=token,
                )
            )
        return targets

    async def delete_by_id(
        self,
        session: AsyncSession,
        scope: UserScope,
        device_id: UUID,
    ) -> bool:
        result = await session.execute(
            delete(DeviceRow)
            .where(
                DeviceRow.id == device_id,
                DeviceRow.user_id == scope.user_id,
            )
            .returning(DeviceRow.id)
        )
        return result.scalar_one_or_none() is not None

    def _to_decrypted(
        self,
        row: DeviceRow,
        scope: UserScope,
        dek: bytes,
    ) -> DecryptedDevice:
        label = (
            self._cipher.decrypt_str(
                dek,
                row.device_label_enc,
                user_id=scope.user_id,
                table="devices",
                column="device_label_enc",
                row_id=row.id,
            )
            if row.device_label_enc is not None
            else None
        )
        return DecryptedDevice(
            id=row.id,
            user_id=row.user_id,
            platform=PushPlatform(row.platform),
            device_label=label,
            enabled=row.enabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ReminderRepository:
    """Encrypted reminder persistence and atomic fire-slot delivery deduplication."""

    def __init__(self, cipher: EnvelopeCipher) -> None:
        self._cipher = cipher

    def encrypt_title(self, dek: bytes, scope: UserScope, reminder_id: UUID, title: str) -> bytes:
        return self._cipher.encrypt_str(
            dek,
            title,
            user_id=scope.user_id,
            table="reminders",
            column="title_enc",
            row_id=reminder_id,
        )

    def decrypt_row(self, row: ReminderRow, scope: UserScope, dek: bytes) -> DecryptedReminder:
        title = self._cipher.decrypt_str(
            dek,
            row.title_enc,
            user_id=scope.user_id,
            table="reminders",
            column="title_enc",
            row_id=row.id,
        )
        return DecryptedReminder(
            id=row.id,
            user_id=row.user_id,
            person_id=row.person_id,
            fact_id=row.fact_id,
            title=title,
            rule=ReminderRule(row.rule),
            is_lunar=row.is_lunar,
            target_month=row.target_month,
            target_day=row.target_day,
            is_leap_month=row.is_leap_month,
            once_date=row.once_date,
            fire_hour=row.fire_hour,
            fire_minute=row.fire_minute,
            lead_days=list(row.lead_days),
            timezone=row.timezone,
            next_fire_at=row.next_fire_at,
            next_lead_day=row.next_lead_day,
            status=ReminderStatus(row.status),
            last_fired_at=row.last_fired_at,
            action_done_at=row.action_done_at,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get_for_update(
        self,
        session: AsyncSession,
        scope: UserScope,
        reminder_id: UUID,
        *,
        skip_locked: bool = False,
    ) -> ReminderRow | None:
        stmt = (
            select(ReminderRow)
            .where(
                ReminderRow.id == reminder_id,
                ReminderRow.user_id == scope.user_id,
            )
            .with_for_update(skip_locked=skip_locked)
        )
        return await session.scalar(stmt)

    async def list_for_user(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        dek: bytes,
        status: ReminderStatus | None = None,
        person_id: UUID | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[DecryptedReminder], str | None, bool]:
        person_predicates = [
            PersonRow.id.is_not(None),
            PersonRow.deleted_at.is_(None),
            PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
        ]
        if not include_archived:
            person_predicates.extend(
                [
                    PersonRow.archived_at.is_(None),
                    PersonRow.state != PersonState.ARCHIVED.value,
                ]
            )

        stmt = (
            select(ReminderRow)
            .outerjoin(PersonRow, PersonRow.id == ReminderRow.person_id)
            .where(
                ReminderRow.user_id == scope.user_id,
                or_(
                    ReminderRow.person_id.is_(None),
                    and_(*person_predicates),
                ),
            )
        )
        if status is not None:
            stmt = stmt.where(ReminderRow.status == status.value)
        if person_id is not None:
            stmt = stmt.where(ReminderRow.person_id == person_id)
        if cursor is not None:
            cursor_ts, cursor_id = _parse_cursor_pair(cursor)
            stmt = stmt.where(
                or_(
                    ReminderRow.created_at < cursor_ts,
                    and_(ReminderRow.created_at == cursor_ts, ReminderRow.id < cursor_id),
                )
            )
        stmt = stmt.order_by(ReminderRow.created_at.desc(), ReminderRow.id.desc()).limit(limit + 1)
        rows = list((await session.scalars(stmt)).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            encode_cursor(
                {
                    "created_at": page_rows[-1].created_at.isoformat(),
                    "id": str(page_rows[-1].id),
                }
            )
            if has_more and page_rows
            else None
        )
        return [self.decrypt_row(r, scope, dek) for r in page_rows], next_cursor, has_more

    async def claim_delivery_fire_slot(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        reminder_id: UUID,
        scheduled_for: datetime,
        lead_day: int,
        channel: str,
        status: str,
        delivered_at: datetime,
    ) -> UUID | None:
        """Atomically insert `ReminderDeliveryRow` or return `None` if slot already delivered."""
        stmt = (
            pg_insert(ReminderDeliveryRow)
            .values(
                id=new_id(),
                user_id=scope.user_id,
                reminder_id=reminder_id,
                scheduled_for=scheduled_for,
                lead_day=lead_day,
                channel=channel,
                status=status,
                delivered_at=delivered_at,
            )
            .on_conflict_do_nothing(constraint="uq_reminder_delivery_fire_slot")
            .returning(ReminderDeliveryRow.id)
        )
        return await session.scalar(stmt)
