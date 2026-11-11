"""Application service for devices, reminders, North-Star telemetry, and due-reminder delivery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import KeyShreddedError
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionMismatchError,
)
from mindweave.core.ids import new_id
from mindweave.core.logging import get_logger
from mindweave.core.orm import (
    FactRow,
    PersonRow,
    ReminderRow,
    TelemetryEventRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.reminders.domain.notification_copy import build_desensitized_push_copy
from mindweave.modules.reminders.domain.schedule import (
    DEFAULT_LEAD_DAYS,
    ReminderScheduleError,
    compute_next_fire_slot,
    normalize_lead_days,
    resolve_next_lunar_occurrence_date,
    validate_timezone_name,
)
from mindweave.modules.reminders.infra.push import (
    CompositePushDispatcher,
    PushMessage,
    PushProviderPort,
)
from mindweave.modules.reminders.infra.repositories import (
    DecryptedDevice,
    DecryptedReminder,
    DeviceRepository,
    ReminderRepository,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    PersonState,
    PushPlatform,
    ReminderRule,
    ReminderStatus,
)
from mindweave.shared.scopes import UserScope

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateReminderInput:
    person_id: UUID
    title: str
    rule: ReminderRule = ReminderRule.ONCE
    fact_id: UUID | None = None
    is_lunar: bool = False
    target_month: int | None = None
    target_day: int | None = None
    is_leap_month: bool = False
    once_date: date | None = None
    fire_hour: int = 9
    fire_minute: int = 0
    lead_days: Sequence[int] = DEFAULT_LEAD_DAYS
    timezone: str | None = None
    status: ReminderStatus = ReminderStatus.ACTIVE
    requested_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UpdateReminderInput:
    title: str | None = None
    rule: ReminderRule | None = None
    is_lunar: bool | None = None
    target_month: int | None = None
    target_day: int | None = None
    is_leap_month: bool | None = None
    once_date: date | None = None
    fire_hour: int | None = None
    fire_minute: int | None = None
    lead_days: Sequence[int] | None = None
    timezone: str | None = None
    status: ReminderStatus | None = None


@dataclass(frozen=True, slots=True)
class ScanDueRemindersResult:
    scanned_count: int
    delivered_slots: int
    pushed_messages: int
    completed_reminders: int


def resolve_push_provider(container: AppContainer) -> PushProviderPort:
    candidate = container.extras.get("push_provider") or container.extras.get("push")
    if candidate is not None:
        return cast(PushProviderPort, candidate)
    dispatcher = CompositePushDispatcher()
    container.extras["push_provider"] = dispatcher
    return dispatcher


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class ReminderService:
    """Use-case orchestrator for device tokens, solar/lunar reminders, and due push dispatch."""

    def __init__(
        self,
        container: AppContainer,
        *,
        push_provider: PushProviderPort | None = None,
    ) -> None:
        self._container = container
        self._push_provider: PushProviderPort = push_provider or resolve_push_provider(container)
        self._devices = DeviceRepository(container.cipher)
        self._reminders = ReminderRepository(container.cipher)

    async def register_device(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        platform: PushPlatform,
        push_token: str,
        device_label: str | None = None,
        enabled: bool = True,
        requested_id: UUID | None = None,
    ) -> tuple[DecryptedDevice, bool]:
        dek = await self._container.load_user_dek(session, scope)
        now = _ensure_utc(self._container.clock.now())
        return await self._devices.upsert(
            session,
            scope,
            dek=dek,
            platform=platform,
            push_token=push_token,
            device_label=device_label,
            enabled=enabled,
            now=now,
            requested_id=requested_id,
        )

    async def list_devices(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[DecryptedDevice], str | None, bool]:
        dek = await self._container.load_user_dek(session, scope)
        return await self._devices.list_for_user(
            session,
            scope,
            dek=dek,
            cursor=cursor,
            limit=limit,
        )

    async def delete_device(
        self,
        session: AsyncSession,
        scope: UserScope,
        device_id: UUID,
    ) -> None:
        deleted = await self._devices.delete_by_id(session, scope, device_id)
        if not deleted:
            raise NotFoundError("Device not found")

    async def list_reminders(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        status: ReminderStatus | None = None,
        person_id: UUID | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[DecryptedReminder], str | None, bool]:
        dek = await self._container.load_user_dek(session, scope)
        return await self._reminders.list_for_user(
            session,
            scope,
            dek=dek,
            status=status,
            person_id=person_id,
            include_archived=include_archived,
            cursor=cursor,
            limit=limit,
        )

    async def create_reminder(
        self,
        session: AsyncSession,
        scope: UserScope,
        inp: CreateReminderInput,
    ) -> DecryptedReminder:
        cleaned_title = inp.title.strip()
        if not cleaned_title:
            raise ValidationFailedError("Reminder title must not be empty")

        if inp.status not in (ReminderStatus.ACTIVE, ReminderStatus.PROPOSED):
            raise ValidationFailedError(
                "Initial reminder status must be either 'active' or 'proposed'"
            )

        person = await session.scalar(
            select(PersonRow).where(
                PersonRow.id == inp.person_id,
                PersonRow.user_id == scope.user_id,
            )
        )
        if person is None or person.deleted_at is not None or person.slug == UNASSIGNED_PERSON_SLUG:
            raise NotFoundError("Person not found")
        if person.archived_at is not None or person.state == PersonState.ARCHIVED.value:
            raise ValidationFailedError("Cannot create reminder for an archived person")

        if inp.fact_id is not None:
            fact_exists = await session.scalar(
                select(FactRow.id).where(
                    FactRow.id == inp.fact_id,
                    FactRow.user_id == scope.user_id,
                )
            )
            if fact_exists is None:
                raise NotFoundError("Fact not found")

        if inp.requested_id is not None:
            existing_id = await session.scalar(
                select(ReminderRow.id).where(ReminderRow.id == inp.requested_id)
            )
            if existing_id is not None:
                raise ConflictError("Reminder with this ID already exists")

        resolved_tz = inp.timezone
        if resolved_tz is None or not resolved_tz.strip():
            pref_tz = await session.scalar(
                select(UserPreferenceRow.timezone).where(UserPreferenceRow.user_id == scope.user_id)
            )
            resolved_tz = pref_tz or self._container.settings.default_timezone
        resolved_tz = resolved_tz.strip()

        now = _ensure_utc(self._container.clock.now())
        resolved_once_date = inp.once_date
        try:
            validate_timezone_name(resolved_tz)
            normalized_leads = list(normalize_lead_days(inp.lead_days))
            if (
                inp.rule is ReminderRule.ONCE
                and resolved_once_date is None
                and inp.is_lunar
                and inp.target_month is not None
                and inp.target_day is not None
            ):
                resolved_once_date = resolve_next_lunar_occurrence_date(
                    target_month=inp.target_month,
                    target_day=inp.target_day,
                    is_leap_month=inp.is_leap_month,
                    timezone=resolved_tz,
                    reference_utc=now,
                    fire_hour=inp.fire_hour,
                    fire_minute=inp.fire_minute,
                    lead_days=normalized_leads,
                )
            slot = compute_next_fire_slot(
                rule=inp.rule,
                timezone=resolved_tz,
                fire_hour=inp.fire_hour,
                fire_minute=inp.fire_minute,
                lead_days=normalized_leads,
                once_date=resolved_once_date,
                is_lunar=inp.is_lunar,
                target_month=inp.target_month,
                target_day=inp.target_day,
                is_leap_month=inp.is_leap_month,
                reference_utc=now,
            )
        except ReminderScheduleError as exc:
            raise ValidationFailedError(str(exc)) from exc

        if inp.status is ReminderStatus.ACTIVE:
            if slot is None:
                raise ValidationFailedError(
                    "All reminder fire slots for onceDate are already in the past"
                )
            next_fire_at: datetime | None = slot.scheduled_for_utc
            next_lead_day: int | None = slot.lead_day
        else:
            next_fire_at = None
            next_lead_day = None

        keep_lunar_meta = inp.rule is ReminderRule.YEARLY or inp.is_lunar
        dek = await self._container.load_user_dek(session, scope)
        row_id = inp.requested_id or new_id()
        row = ReminderRow(
            id=row_id,
            user_id=scope.user_id,
            person_id=inp.person_id,
            fact_id=inp.fact_id,
            title_enc=self._reminders.encrypt_title(dek, scope, row_id, cleaned_title),
            rule=inp.rule.value,
            is_lunar=inp.is_lunar if keep_lunar_meta else False,
            target_month=inp.target_month if keep_lunar_meta else None,
            target_day=inp.target_day if keep_lunar_meta else None,
            is_leap_month=inp.is_leap_month if (keep_lunar_meta and inp.is_lunar) else False,
            once_date=resolved_once_date if inp.rule is ReminderRule.ONCE else None,
            fire_hour=inp.fire_hour,
            fire_minute=inp.fire_minute,
            lead_days=normalized_leads,
            timezone=resolved_tz,
            next_fire_at=next_fire_at,
            next_lead_day=next_lead_day,
            status=inp.status.value,
            last_fired_at=None,
            action_done_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return self._reminders.decrypt_row(row, scope, dek)

    async def update_reminder(
        self,
        session: AsyncSession,
        scope: UserScope,
        reminder_id: UUID,
        inp: UpdateReminderInput,
        *,
        expected_version: int | None,
    ) -> DecryptedReminder:
        if expected_version is None:
            raise BadRequestError("If-Match header is required for optimistic locking")

        row = await self._reminders.get_for_update(session, scope, reminder_id)
        if row is None:
            raise NotFoundError("Reminder not found")

        if row.version != expected_version:
            raise VersionMismatchError(
                f"Optimistic lock failed: expected version {expected_version}, current is {row.version}"
            )

        dek = await self._container.load_user_dek(session, scope)
        now = _ensure_utc(self._container.clock.now())

        if inp.title is not None:
            cleaned_title = inp.title.strip()
            if not cleaned_title:
                raise ValidationFailedError("Reminder title must not be empty")
            row.title_enc = self._reminders.encrypt_title(dek, scope, row.id, cleaned_title)

        new_rule = inp.rule if inp.rule is not None else ReminderRule(row.rule)
        new_is_lunar = inp.is_lunar if inp.is_lunar is not None else row.is_lunar
        new_target_month = inp.target_month if inp.target_month is not None else row.target_month
        new_target_day = inp.target_day if inp.target_day is not None else row.target_day
        new_is_leap = inp.is_leap_month if inp.is_leap_month is not None else row.is_leap_month
        new_once_date = inp.once_date if inp.once_date is not None else row.once_date
        new_fire_hour = inp.fire_hour if inp.fire_hour is not None else row.fire_hour
        new_fire_minute = inp.fire_minute if inp.fire_minute is not None else row.fire_minute
        new_tz = inp.timezone.strip() if inp.timezone is not None else row.timezone
        new_status = inp.status if inp.status is not None else ReminderStatus(row.status)

        if (
            new_rule is ReminderRule.ONCE
            and new_once_date is None
            and row.fact_id is not None
            and inp.is_lunar is not False
            and (not new_is_lunar or new_target_month is None or new_target_day is None)
        ):
            linked_fact = await session.scalar(
                select(FactRow).where(
                    FactRow.id == row.fact_id,
                    FactRow.user_id == scope.user_id,
                )
            )
            if (
                linked_fact is not None
                and linked_fact.is_lunar
                and linked_fact.lunar_month is not None
                and linked_fact.lunar_day is not None
            ):
                new_is_lunar = True
                if new_target_month is None:
                    new_target_month = linked_fact.lunar_month
                if new_target_day is None:
                    new_target_day = linked_fact.lunar_day
                if inp.is_leap_month is None:
                    new_is_leap = linked_fact.lunar_is_leap_month

        try:
            validate_timezone_name(new_tz)
            new_leads = (
                list(normalize_lead_days(inp.lead_days))
                if inp.lead_days is not None
                else list(normalize_lead_days(row.lead_days))
            )
            if (
                new_rule is ReminderRule.ONCE
                and new_once_date is None
                and new_is_lunar
                and new_target_month is not None
                and new_target_day is not None
            ):
                new_once_date = resolve_next_lunar_occurrence_date(
                    target_month=new_target_month,
                    target_day=new_target_day,
                    is_leap_month=new_is_leap,
                    timezone=new_tz,
                    reference_utc=now,
                    fire_hour=new_fire_hour,
                    fire_minute=new_fire_minute,
                    lead_days=new_leads,
                )

            keep_lunar_meta = new_rule is ReminderRule.YEARLY or new_is_lunar
            slot = compute_next_fire_slot(
                rule=new_rule,
                timezone=new_tz,
                fire_hour=new_fire_hour,
                fire_minute=new_fire_minute,
                lead_days=new_leads,
                once_date=new_once_date if new_rule is ReminderRule.ONCE else None,
                is_lunar=new_is_lunar if keep_lunar_meta else False,
                target_month=new_target_month if keep_lunar_meta else None,
                target_day=new_target_day if keep_lunar_meta else None,
                is_leap_month=new_is_leap if (keep_lunar_meta and new_is_lunar) else False,
                reference_utc=now,
            )
        except ReminderScheduleError as exc:
            raise ValidationFailedError(str(exc)) from exc

        row.rule = new_rule.value
        row.is_lunar = new_is_lunar if keep_lunar_meta else False
        row.target_month = new_target_month if keep_lunar_meta else None
        row.target_day = new_target_day if keep_lunar_meta else None
        row.is_leap_month = new_is_leap if (keep_lunar_meta and new_is_lunar) else False
        row.once_date = new_once_date if new_rule is ReminderRule.ONCE else None
        row.fire_hour = new_fire_hour
        row.fire_minute = new_fire_minute
        row.lead_days = new_leads
        row.timezone = new_tz
        row.status = new_status.value

        if new_status is ReminderStatus.ACTIVE:
            if slot is None:
                raise ValidationFailedError(
                    "All reminder fire slots for onceDate are already in the past"
                )
            row.next_fire_at = slot.scheduled_for_utc
            row.next_lead_day = slot.lead_day
        else:
            row.next_fire_at = None
            row.next_lead_day = None

        row.version += 1
        row.updated_at = now
        await session.flush()
        return self._reminders.decrypt_row(row, scope, dek)

    async def cancel_reminder(
        self,
        session: AsyncSession,
        scope: UserScope,
        reminder_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> DecryptedReminder:
        row = await self._reminders.get_for_update(session, scope, reminder_id)
        if row is None:
            raise NotFoundError("Reminder not found")
        if expected_version is not None and row.version != expected_version:
            raise VersionMismatchError(
                f"Optimistic lock failed: expected version {expected_version}, current is {row.version}"
            )

        dek = await self._container.load_user_dek(session, scope)
        now = _ensure_utc(self._container.clock.now())
        row.status = ReminderStatus.CANCELLED.value
        row.next_fire_at = None
        row.next_lead_day = None
        row.version += 1
        row.updated_at = now
        await session.flush()
        return self._reminders.decrypt_row(row, scope, dek)

    async def mark_reminder_done(
        self,
        session: AsyncSession,
        scope: UserScope,
        reminder_id: UUID,
    ) -> DecryptedReminder:
        row = await self._reminders.get_for_update(session, scope, reminder_id)
        if row is None:
            raise NotFoundError("Reminder not found")
        if row.status == ReminderStatus.CANCELLED.value:
            raise ConflictError("Cancelled reminder cannot be marked done")

        dek = await self._container.load_user_dek(session, scope)
        now = _ensure_utc(self._container.clock.now())
        row.action_done_at = now
        if row.rule == ReminderRule.ONCE.value:
            row.status = ReminderStatus.COMPLETED.value
            row.next_fire_at = None
            row.next_lead_day = None
        row.version += 1
        row.updated_at = now

        # North-Star metric telemetry (PRD §3 & §8 — strictly zero PII / no content or names!)
        session.add(
            TelemetryEventRow(
                id=new_id(),
                user_id=scope.user_id,
                event_name="reminder_action_done",
                metadata_json={"rule": row.rule},
                occurred_at=now,
                created_at=now,
            )
        )
        await session.flush()
        return self._reminders.decrypt_row(row, scope, dek)

    async def scan_and_deliver_due_reminders(
        self,
        *,
        batch_limit: int = 100,
    ) -> ScanDueRemindersResult:
        """Idempotently scan due reminders across active users and dispatch desensitized pushes."""
        now = _ensure_utc(self._container.clock.now())
        async with self._container.db.system_session() as sys_sess:
            due_rows = (
                await sys_sess.execute(
                    select(ReminderRow.id, ReminderRow.user_id)
                    .join(UserRow, UserRow.id == ReminderRow.user_id)
                    .outerjoin(PersonRow, PersonRow.id == ReminderRow.person_id)
                    .where(
                        ReminderRow.status == ReminderStatus.ACTIVE.value,
                        ReminderRow.next_fire_at.is_not(None),
                        ReminderRow.next_fire_at <= now,
                        UserRow.status == "active",
                        UserRow.deleted_at.is_(None),
                        or_(
                            ReminderRow.person_id.is_(None),
                            and_(
                                PersonRow.id.is_not(None),
                                PersonRow.deleted_at.is_(None),
                                PersonRow.archived_at.is_(None),
                                PersonRow.state != PersonState.ARCHIVED.value,
                                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
                            ),
                        ),
                    )
                    .order_by(ReminderRow.next_fire_at.asc(), ReminderRow.id.asc())
                    .limit(batch_limit)
                )
            ).all()

        delivered_slots = 0
        pushed_messages = 0
        completed_reminders = 0

        for reminder_id, user_id in due_rows:
            scope = UserScope(user_id=user_id)
            try:
                slot_delivered, msg_count, completed = await self._deliver_single_due_reminder(
                    scope=scope,
                    reminder_id=reminder_id,
                    now=now,
                )
            except KeyShreddedError:
                logger.warning("reminders.skip_shredded_user", reminder_id=str(reminder_id))
                continue

            if slot_delivered:
                delivered_slots += 1
            pushed_messages += msg_count
            if completed:
                completed_reminders += 1

        return ScanDueRemindersResult(
            scanned_count=len(due_rows),
            delivered_slots=delivered_slots,
            pushed_messages=pushed_messages,
            completed_reminders=completed_reminders,
        )

    async def _deliver_single_due_reminder(
        self,
        *,
        scope: UserScope,
        reminder_id: UUID,
        now: datetime,
    ) -> tuple[bool, int, bool]:
        async with self._container.db.user_session(scope) as session:
            row = await session.scalar(
                select(ReminderRow)
                .where(
                    ReminderRow.id == reminder_id,
                    ReminderRow.user_id == scope.user_id,
                    ReminderRow.status == ReminderStatus.ACTIVE.value,
                    ReminderRow.next_fire_at.is_not(None),
                    ReminderRow.next_fire_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
            if row is None or row.next_fire_at is None:
                return False, 0, False

            if row.person_id is not None:
                person = await session.scalar(
                    select(PersonRow).where(
                        PersonRow.id == row.person_id,
                        PersonRow.user_id == scope.user_id,
                    )
                )
                if (
                    person is None
                    or person.deleted_at is not None
                    or person.slug == UNASSIGNED_PERSON_SLUG
                ):
                    row.status = ReminderStatus.CANCELLED.value
                    row.next_fire_at = None
                    row.next_lead_day = None
                    row.version += 1
                    row.updated_at = now
                    await session.flush()
                    return False, 0, False
                if person.archived_at is not None or person.state == PersonState.ARCHIVED.value:
                    row.status = ReminderStatus.PAUSED.value
                    row.next_fire_at = None
                    row.next_lead_day = None
                    row.version += 1
                    row.updated_at = now
                    await session.flush()
                    return False, 0, False

            scheduled_for = _ensure_utc(row.next_fire_at)
            lead_day = row.next_lead_day if row.next_lead_day is not None else 0

            dek = await self._container.load_user_dek(session, scope)
            targets = await self._devices.list_enabled_targets(session, scope, dek=dek)

            if not targets:
                channel = "in_app"
                delivery_status = "no_device"
            elif len({t.platform for t in targets}) == 1:
                channel = targets[0].platform.value
                delivery_status = "delivered"
            else:
                channel = "multi"
                delivery_status = "delivered"

            claimed_delivery_id = await self._reminders.claim_delivery_fire_slot(
                session,
                scope,
                reminder_id=row.id,
                scheduled_for=scheduled_for,
                lead_day=lead_day,
                channel=channel,
                status=delivery_status,
                delivered_at=now,
            )

            sent_count = 0
            if claimed_delivery_id is not None:
                if targets:
                    copy = build_desensitized_push_copy(
                        lead_day=lead_day,
                        rule=row.rule,
                        reminder_id=row.id,
                    )
                    for target in targets:
                        msg = PushMessage(
                            device_id=target.id,
                            user_id=scope.user_id,
                            reminder_id=row.id,
                            platform=target.platform,
                            push_token=target.push_token,
                            title=copy.title,
                            body=copy.body,
                            deeplink_path=copy.deeplink_path,
                            scheduled_for=scheduled_for,
                            lead_day=lead_day,
                        )
                        await self._push_provider.send(msg)
                        sent_count += 1
                row.last_fired_at = now

            effective_once_date = row.once_date
            if row.rule == ReminderRule.ONCE.value and effective_once_date is None:
                tz_obj = validate_timezone_name(row.timezone)
                effective_once_date = scheduled_for.astimezone(tz_obj).date() + timedelta(
                    days=lead_day
                )
                row.once_date = effective_once_date

            next_slot = compute_next_fire_slot(
                rule=row.rule,
                timezone=row.timezone,
                fire_hour=row.fire_hour,
                fire_minute=row.fire_minute,
                lead_days=row.lead_days,
                once_date=effective_once_date,
                is_lunar=row.is_lunar,
                target_month=row.target_month,
                target_day=row.target_day,
                is_leap_month=row.is_leap_month,
                after_utc=max(scheduled_for, now),
            )
            completed = False
            if next_slot is None:
                row.next_fire_at = None
                row.next_lead_day = None
                if row.rule == ReminderRule.ONCE.value:
                    row.status = ReminderStatus.COMPLETED.value
                    completed = True
            else:
                row.next_fire_at = next_slot.scheduled_for_utc
                row.next_lead_day = next_slot.lead_day

            row.version += 1
            row.updated_at = now
            await session.flush()
            return claimed_delivery_id is not None, sent_count, completed


async def scan_and_deliver_due_reminders(
    container: AppContainer,
    *,
    push_provider: PushProviderPort | None = None,
    batch_limit: int = 100,
) -> ScanDueRemindersResult:
    """Top-level worker helper for `TASK_SCAN_DUE_REMINDERS`."""
    service = ReminderService(container, push_provider=push_provider)
    return await service.scan_and_deliver_due_reminders(batch_limit=batch_limit)
