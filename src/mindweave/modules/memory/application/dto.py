"""CamelCase DTOs for Materials (`RecordRow`), Facts (`FactRow`), and Pending Inbox (`InboxItemRow`)."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.shared.enums import (
    CreatedBy,
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PendingReason,
    PersonState,
    Polarity,
    RecordKind,
    RecordReviewState,
    SourceSpeaker,
    Tier,
    TimeStatus,
)


class MaterialDto(CamelModel):
    id: UUID
    person_id: UUID
    capture_id: UUID | None = None
    kind: RecordKind
    type: RecordKind
    title: str
    body: str
    snippet: str
    has_media: bool = False
    media_url: str | None = None
    dur: str | None = None
    audio_duration_ms: int | None = None
    stamp_code: str | None = None
    happened_on: date | None = None
    pinned: bool = True
    review_state: RecordReviewState = RecordReviewState.NORMAL
    version: int = 1
    created_at: datetime
    updated_at: datetime


class MaterialCreateRequest(CamelModel):
    id: UUID | None = None
    kind: RecordKind = RecordKind.NOTE
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=10000)
    snippet: str | None = Field(default=None, max_length=300)
    stamp_code: str | None = Field(default=None, max_length=64)
    happened_on: date | None = None
    pinned: bool = True
    photo_base64: str | None = None


class MaterialPatchRequest(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    body: str | None = Field(default=None, min_length=1, max_length=10000)
    snippet: str | None = Field(default=None, max_length=300)
    stamp_code: str | None = Field(default=None, max_length=64)
    happened_on: date | None = None
    pinned: bool | None = None
    review_state: RecordReviewState | None = None


class FactDto(CamelModel):
    id: UUID
    person_id: UUID | None = None
    record_id: UUID | None = None
    capture_id: UUID | None = None
    type: FactType
    polarity: Polarity = Polarity.NEUTRAL
    time_status: TimeStatus = TimeStatus.CURRENT
    source_speaker: SourceSpeaker = SourceSpeaker.NARRATOR
    content: str
    trigger_date: date | None = None
    is_lunar: bool = False
    lunar_month: int | None = None
    lunar_day: int | None = None
    lunar_is_leap_month: bool = False
    status: FactStatus
    pending_reason: PendingReason | None = None
    confidence: float | None = None
    created_by: CreatedBy = CreatedBy.AI
    valid_at: datetime
    invalid_at: datetime | None = None
    superseded_by_id: UUID | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class FactCreateRequest(CamelModel):
    id: UUID | None = None
    record_id: UUID | None = None
    type: FactType
    polarity: Polarity = Polarity.NEUTRAL
    content: str = Field(min_length=1, max_length=2000)
    trigger_date: date | None = None
    is_lunar: bool = False
    lunar_month: int | None = Field(default=None, ge=1, le=12)
    lunar_day: int | None = Field(default=None, ge=1, le=30)
    lunar_is_leap_month: bool = False
    supersedes_fact_id: UUID | None = None


class FactPatchRequest(CamelModel):
    type: FactType | None = None
    polarity: Polarity | None = None
    time_status: TimeStatus | None = None
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    trigger_date: date | None = None
    is_lunar: bool | None = None
    lunar_month: int | None = Field(default=None, ge=1, le=12)
    lunar_day: int | None = Field(default=None, ge=1, le=30)
    lunar_is_leap_month: bool | None = None
    status: FactStatus | None = None


class InboxCandidatePersonDto(CamelModel):
    person_id: UUID
    display_name: str
    monogram: str
    tier: Tier
    role_tag: str = ""
    state: PersonState = PersonState.ACTIVE


class InboxItemDto(CamelModel):
    id: UUID
    kind: InboxItemKind
    status: InboxItemStatus
    person_id: UUID | None = None
    person_name: str | None = None
    capture_id: UUID | None = None
    record_id: UUID | None = None
    fact_ids: list[UUID] = Field(default_factory=list)
    facts: list[FactDto] = Field(default_factory=list)
    candidate_persons: list[InboxCandidatePersonDto] = Field(default_factory=list)
    summary: str = ""
    mention: str | None = None
    resolution_action: InboxResolutionAction | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class InboxResolveRequest(CamelModel):
    action: InboxResolutionAction
    target_person_id: UUID | None = None
    display_name: str | None = Field(default=None, max_length=80)
    tier: Tier | None = None
    trigger_date: date | None = None


class InboxResolveResponse(CamelModel):
    item: InboxItemDto
    resolved_fact_ids: list[UUID] = Field(default_factory=list)
    superseded_fact_ids: list[UUID] = Field(default_factory=list)
    target_person_id: UUID | None = None
