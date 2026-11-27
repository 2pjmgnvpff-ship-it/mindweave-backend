"""CamelCase Pydantic v2 DTOs for `GET /v1/bootstrap` aligned with V10 `gleb-app.mjs`."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.shared.enums import (
    CupidLayout,
    Edition,
    FactStatus,
    FactType,
    PersonState,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderRule,
    ReminderStatus,
    Tier,
)


class BootstrapPreferencesDTO(CamelModel):
    """User aesthetic & care preferences embedded in `/bootstrap`."""

    edition: Edition
    preferred_edition: Edition
    cupid_layout: CupidLayout
    wallpaper_preset: str
    has_custom_wallpaper: bool
    wallpaper_url: str | None = None
    wallpaper_blur: int
    wallpaper_vignette: int
    app_lock_enabled: bool
    late_night_care_enabled: bool
    timezone: str
    version: int


class BootstrapMaterialPreviewDTO(CamelModel):
    """Scrapbook material preview inside `people[personId].materials[]`."""

    id: UUID
    type: RecordKind
    title: str
    body: str
    snippet: str
    dur: str | None = None
    stamp_code: str | None = None
    happened_on: date | None = None
    media_url: str | None = None
    thumb: str | None = None
    is_favorite: bool = False
    card_meta: dict[str, Any] = Field(default_factory=dict)
    review_state: RecordReviewState = RecordReviewState.NORMAL
    version: int = 1


class BootstrapFactPreviewDTO(CamelModel):
    """Confirmed fact / pending date draft preview for Jade Fact Pills & Dossier."""

    id: UUID
    record_id: UUID | None = None
    type: FactType
    polarity: Polarity = Polarity.NEUTRAL
    content: str
    status: FactStatus
    trigger_date: date | None = None
    is_lunar: bool = False
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_is_leap_month: bool = False
    version: int = 1


class BootstrapReminderPreviewDTO(CamelModel):
    """Upcoming care reminder preview for the Luminous Care Ribbon & Dossier."""

    id: UUID
    fact_id: UUID | None = None
    title: str
    rule: ReminderRule
    is_lunar: bool = False
    target_month: int | None = None
    target_day: int | None = None
    is_leap_month: bool = False
    once_date: date | None = None
    lead_days: list[int] = Field(default_factory=list)
    next_fire_at: datetime | None = None
    status: ReminderStatus
    version: int = 1


class BootstrapPersonDTO(CamelModel):
    """`PEOPLE_DB[id]` entry hydrated with encrypted profile, materials, facts & care reminders."""

    id: str
    name_cn: str
    monogram: str
    role_tag: str
    context_sub: str
    cupid_title: str
    tier: Tier
    state: PersonState
    has_photo: bool
    short_memo: str
    traits: list[str] = Field(default_factory=list)
    visual_tokens: dict[str, Any] = Field(default_factory=dict)
    seal_bg: str
    seal_color: str
    tape_color: str
    tab_color: str
    avatar: str | None = None
    main_photo: str | None = None
    sub_photo: str | None = None
    avatar_url: str | None = None
    main_photo_url: str | None = None
    sub_photo_url: str | None = None
    materials: list[BootstrapMaterialPreviewDTO] = Field(default_factory=list)
    facts: list[BootstrapFactPreviewDTO] = Field(default_factory=list)
    upcoming_reminders: list[BootstrapReminderPreviewDTO] = Field(default_factory=list)
    cooling_count: int = 0
    version: int


class BootstrapResponse(CamelModel):
    """Single round-trip workspace bootstrap payload for MindWeave Web & iOS."""

    preferences: BootstrapPreferencesDTO
    core_ids: list[str]
    casual_ids: list[str]
    people: dict[str, BootstrapPersonDTO]
    inbox_count: int
