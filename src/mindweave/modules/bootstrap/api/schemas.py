"""CamelCase Pydantic v2 DTOs for `GET /v1/bootstrap` aligned with V10 `gleb-app.mjs`."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.shared.enums import CupidLayout, Edition, PersonState, RecordKind, Tier


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


class BootstrapPersonDTO(CamelModel):
    """V10 `PEOPLE_DB[id]` entry hydrated with encrypted profile & top-10 materials."""

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
    version: int


class BootstrapResponse(CamelModel):
    """Single round-trip workspace bootstrap payload for Web V10 & iOS."""

    preferences: BootstrapPreferencesDTO
    core_ids: list[str]
    casual_ids: list[str]
    people: dict[str, BootstrapPersonDTO]
    inbox_count: int
