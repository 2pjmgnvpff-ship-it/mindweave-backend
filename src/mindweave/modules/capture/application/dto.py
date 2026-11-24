"""CamelCase API DTOs for Multimodal Capture, Recent Captures Manager (C1), and V10 `/voice/ingest`."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import Field, field_validator

from mindweave.ai.schemas.contracts import CrisisCardPayload
from mindweave.core.dto import CamelModel
from mindweave.shared.enums import (
    CaptureSource,
    CaptureStatus,
    FactType,
    Presentation,
    RecordKind,
    RouteMode,
    SafetyCategory,
    Tier,
)


class CrisisCardDto(CamelModel):
    template_id: str
    title: str
    care_message: str
    hotlines: list[dict[str, str]]
    disclaimer: str

    @classmethod
    def from_contract(cls, payload: CrisisCardPayload) -> CrisisCardDto:
        return cls(
            template_id=payload.template_id,
            title=payload.title,
            care_message=payload.care_message,
            hotlines=payload.hotlines,
            disclaimer=payload.disclaimer,
        )


class CaptureSplitDto(CamelModel):
    person_id: UUID
    person_name: str
    tier: Tier
    is_new_person: bool = False
    material_id: UUID
    version: int = 1
    tag: str
    dur: str
    body: str
    snippet: str | None = None
    trigger_date: str | None = None
    self_corrected_from: str | None = None
    happened_on: str | None = None
    kind: RecordKind | None = None
    # D1 pencil drafts: how the journal draws this slip (ink / pencil / question).
    presentation: Presentation = Presentation.INK
    # Short natural-language hint shown under a pencil/question slip (e.g. "5月12日 · 点一下确认日期").
    glance_hint: str | None = None
    fact_ids: list[UUID] = Field(default_factory=list)
    # Review item to resolve this slip through (`POST /v1/inbox/{id}:resolve`), if any.
    inbox_item_id: UUID | None = None
    has_media: bool = False
    media_url: str | None = None


class CaptureCandidatePersonDto(CamelModel):
    person_id: UUID
    display_name: str
    tier: Tier = Tier.CRYSTAL
    distinguishing_cue: str | None = None


class CaptureCreateJsonRequest(CamelModel):
    id: UUID | None = None
    source: CaptureSource = CaptureSource.TEXT
    text: str | None = Field(default=None, max_length=20000)
    client_transcript: str | None = Field(default=None, max_length=20000)
    speech_engine: str | None = Field(default=None, max_length=64)
    audio_duration_ms: int | None = Field(default=None, ge=0, le=600_000)
    audio_base64: str | None = None
    image_base64: str | None = None
    route_mode: RouteMode = RouteMode.GLOBAL
    active_person_id: UUID | None = None
    keep_original_media: bool = False
    sync: bool | None = None

    @field_validator("route_mode", mode="before")
    @classmethod
    def _accept_scoped_alias(cls, value: object) -> object:
        # PWA alias: "scoped" == "current" (capture into the open person page)
        if isinstance(value, str) and value.strip().lower() == "scoped":
            return RouteMode.CURRENT
        return value


class CaptureResponse(CamelModel):
    capture_id: UUID
    source: CaptureSource
    route_mode: RouteMode
    scoped_person_id: UUID | None = None
    status: CaptureStatus
    transcript: str = ""
    transcript_html: str = ""
    splits: list[CaptureSplitDto] = Field(default_factory=list)
    candidate_persons: list[CaptureCandidatePersonDto] = Field(default_factory=list)
    pending_count: int = 0
    inbox_item_ids: list[UUID] = Field(default_factory=list)
    safety_category: SafetyCategory = SafetyCategory.NORMAL
    crisis_card: CrisisCardDto | None = None
    undo_deadline: datetime | None = None
    needs_cloud_refinement: bool = False
    extraction_mode: str = "cloud_llm"
    error_code: str | None = None
    has_audio: bool = False
    audio_url: str | None = None
    duration_ms: int | None = None
    created_at: datetime
    completed_at: datetime | None = None


class VoiceIngestResponse(CamelModel):
    """Exact V10 `/voice/ingest` compatibility contract + V2 Spatial Receipt extensions."""

    job_id: UUID
    route_mode: RouteMode
    transcript: str = ""
    transcript_html: str
    splits: list[CaptureSplitDto] = Field(default_factory=list)
    candidate_persons: list[CaptureCandidatePersonDto] = Field(default_factory=list)
    pending_count: int = 0
    undo_deadline: datetime | None = None
    crisis_card: CrisisCardDto | None = None
    inbox_item_ids: list[UUID] = Field(default_factory=list)
    needs_cloud_refinement: bool = False
    extraction_mode: str = "cloud_llm"
    error_code: str | None = None


class CaptureUndoResponse(CamelModel):
    capture_id: UUID
    status: CaptureStatus
    reverted_record_count: int
    rejected_fact_count: int
    cancelled_reminder_count: int
    affected_person_ids: list[UUID] = Field(default_factory=list)


# =============================================================================
# User C1「最近归档记录」Manager DTOs (`GET /v1/captures`, `POST /v1/captures/{id}/slips`)
# =============================================================================


class CaptureHistoryPersonSummaryDto(CamelModel):
    person_id: UUID
    person_name: str
    tier: Tier
    monogram: str
    slip_count: int


class CaptureHistorySplitDto(CamelModel):
    material_id: UUID
    version: int = 1
    person_id: UUID
    person_name: str
    tier: Tier
    kind: RecordKind
    title: str
    body: str
    snippet: str | None = None
    happened_on: str
    presentation: Presentation = Presentation.INK
    glance_hint: str | None = None
    inbox_item_id: UUID | None = None
    fact_ids: list[UUID] = Field(default_factory=list)
    has_media: bool = False
    media_url: str | None = None


class CaptureHistoryItemDto(CamelModel):
    capture_id: UUID
    source: CaptureSource
    route_mode: RouteMode
    status: CaptureStatus
    transcript: str = ""
    transcript_html: str = ""
    has_audio: bool = False
    audio_url: str | None = None
    duration_ms: int | None = None
    dur: str = ""
    has_image: bool = False
    image_url: str | None = None
    can_quick_undo: bool = False
    undo_deadline: datetime | None = None
    active_slip_count: int = 0
    pending_count: int = 0
    person_summaries: list[CaptureHistoryPersonSummaryDto] = Field(default_factory=list)
    splits: list[CaptureHistorySplitDto] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None


class CaptureAddSlipRequest(CamelModel):
    person_id: UUID | None = None
    new_person_name: str | None = Field(default=None, min_length=1, max_length=80)
    new_person_tier: Tier = Tier.CRYSTAL
    title: str | None = Field(default=None, max_length=160)
    body: str = Field(min_length=1, max_length=10000)
    happened_on: date | None = None
    fact_type: FactType = FactType.NOTE
    trigger_date: date | None = None
    inherit_capture_audio: bool = True


class CaptureAddSlipResponse(CamelModel):
    material_id: UUID
    capture_id: UUID
    person_id: UUID
    person_name: str
    tier: Tier
    kind: RecordKind
    title: str
    body: str
    snippet: str
    happened_on: str
    version: int = 1
    has_media: bool = False
    media_url: str | None = None
    affected_person_ids: list[UUID] = Field(default_factory=list)
