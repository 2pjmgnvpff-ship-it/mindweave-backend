"""CamelCase API request and response DTOs for `/reminders` and `/devices`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.modules.reminders.domain.schedule import (
    DEFAULT_LEAD_DAYS,
    ReminderScheduleError,
    resolve_next_lunar_occurrence_date,
)
from mindweave.modules.reminders.infra.repositories import DecryptedDevice, DecryptedReminder
from mindweave.shared.enums import PushPlatform, ReminderRule, ReminderStatus


def _compute_next_occurrence_date(item: DecryptedReminder) -> date | None:
    """Compute the concrete solar occurrence date corresponding to a solar or lunar reminder."""
    if item.rule is ReminderRule.ONCE:
        return item.once_date
    if item.next_fire_at is not None:
        try:
            tz = ZoneInfo(item.timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            tz = ZoneInfo("Asia/Shanghai")
        fire_dt = (
            item.next_fire_at.replace(tzinfo=UTC)
            if item.next_fire_at.tzinfo is None
            else item.next_fire_at
        )
        local_fire_date = fire_dt.astimezone(tz).date()
        lead = int(item.next_lead_day or 0)
        return local_fire_date + timedelta(days=lead)
    if item.is_lunar and item.target_month is not None and item.target_day is not None:
        try:
            return resolve_next_lunar_occurrence_date(
                target_month=item.target_month,
                target_day=item.target_day,
                is_leap_month=item.is_leap_month,
                timezone=item.timezone,
                reference_utc=datetime.now(UTC),
                fire_hour=item.fire_hour,
                fire_minute=item.fire_minute,
                lead_days=item.lead_days,
            )
        except (ReminderScheduleError, ValueError):
            return None
    return item.once_date


class RegisterDeviceRequest(CamelModel):
    id: UUID | None = None
    platform: PushPlatform
    push_token: str = Field(min_length=1, max_length=4096)
    device_label: str | None = Field(default=None, max_length=120)
    enabled: bool = True


class DeviceResponse(CamelModel):
    id: UUID
    platform: PushPlatform
    device_label: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, item: DecryptedDevice) -> DeviceResponse:
        return cls(
            id=item.id,
            platform=item.platform,
            device_label=item.device_label,
            enabled=item.enabled,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class CreateReminderRequest(CamelModel):
    id: UUID | None = None
    person_id: UUID
    fact_id: UUID | None = None
    title: str = Field(min_length=1, max_length=240)
    rule: ReminderRule = ReminderRule.ONCE
    is_lunar: bool = False
    target_month: int | None = Field(default=None, ge=1, le=12)
    target_day: int | None = Field(default=None, ge=1, le=31)
    is_leap_month: bool = False
    once_date: date | None = None
    fire_hour: int = Field(default=9, ge=0, le=23)
    fire_minute: int = Field(default=0, ge=0, le=59)
    lead_days: list[int] = Field(default_factory=lambda: list(DEFAULT_LEAD_DAYS))
    timezone: str | None = Field(default=None, max_length=64)
    status: ReminderStatus = ReminderStatus.ACTIVE


class UpdateReminderRequest(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    rule: ReminderRule | None = None
    is_lunar: bool | None = None
    target_month: int | None = Field(default=None, ge=1, le=12)
    target_day: int | None = Field(default=None, ge=1, le=31)
    is_leap_month: bool | None = None
    once_date: date | None = None
    fire_hour: int | None = Field(default=None, ge=0, le=23)
    fire_minute: int | None = Field(default=None, ge=0, le=59)
    lead_days: list[int] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    status: ReminderStatus | None = None


class ReminderResponse(CamelModel):
    id: UUID
    person_id: UUID
    fact_id: UUID | None = None
    title: str
    rule: ReminderRule
    is_lunar: bool
    target_month: int | None = None
    target_day: int | None = None
    is_leap_month: bool
    once_date: date | None = None
    next_occurrence_date: date | None = None
    fire_hour: int
    fire_minute: int
    lead_days: list[int]
    timezone: str
    next_fire_at: datetime | None = None
    next_lead_day: int | None = None
    status: ReminderStatus
    last_fired_at: datetime | None = None
    action_done_at: datetime | None = None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, item: DecryptedReminder) -> ReminderResponse:
        return cls(
            id=item.id,
            person_id=item.person_id,
            fact_id=item.fact_id,
            title=item.title,
            rule=item.rule,
            is_lunar=item.is_lunar,
            target_month=item.target_month,
            target_day=item.target_day,
            is_leap_month=item.is_leap_month,
            once_date=item.once_date,
            next_occurrence_date=_compute_next_occurrence_date(item),
            fire_hour=item.fire_hour,
            fire_minute=item.fire_minute,
            lead_days=list(item.lead_days),
            timezone=item.timezone,
            next_fire_at=item.next_fire_at,
            next_lead_day=item.next_lead_day,
            status=item.status,
            last_fired_at=item.last_fired_at,
            action_done_at=item.action_done_at,
            version=item.version,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
