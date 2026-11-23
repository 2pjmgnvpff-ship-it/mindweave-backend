"""CamelCase API schemas for person-scoped reminders endpoints (`/people/{person_id}/reminders`)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import AliasChoices, Field

from mindweave.core.dto import CamelModel
from mindweave.modules.reminders.api.dto import ReminderResponse, UpdateReminderRequest
from mindweave.modules.reminders.domain.schedule import DEFAULT_LEAD_DAYS
from mindweave.shared.enums import ReminderRule, ReminderStatus

__all__ = [
    "CreatePersonReminderRequest",
    "ReminderResponse",
    "UpdateReminderRequest",
]


class CreatePersonReminderRequest(CamelModel):
    """Request payload for `POST /v1/people/{person_id}/reminders` (accepts `triggerDate` or `onceDate`)."""

    id: UUID | None = None
    fact_id: UUID | None = None
    title: str = Field(min_length=1, max_length=240)
    rule: ReminderRule = ReminderRule.ONCE
    is_lunar: bool = False
    target_month: int | None = Field(default=None, ge=1, le=12)
    target_day: int | None = Field(default=None, ge=1, le=31)
    is_leap_month: bool = False
    once_date: date | None = Field(
        default=None,
        validation_alias=AliasChoices("onceDate", "triggerDate", "once_date", "trigger_date"),
    )
    fire_hour: int = Field(default=9, ge=0, le=23)
    fire_minute: int = Field(default=0, ge=0, le=59)
    lead_days: list[int] = Field(default_factory=lambda: list(DEFAULT_LEAD_DAYS))
    timezone: str | None = Field(default=None, max_length=64)
    status: ReminderStatus = ReminderStatus.ACTIVE
