"""CamelCase API DTOs for Multimodal Capture and V10 `/voice/ingest` compatibility."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from mindweave.ai.schemas.contracts import CrisisCardPayload
from mindweave.core.dto import CamelModel
from mindweave.shared.enums import (
    CaptureSource,
    CaptureStatus,
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
    kind: RecordKind = RecordKind.NOTE
    tag: str
    dur: str
    body: str


class CaptureCreateJsonRequest(CamelModel):
    id: UUID | None = None
    source: CaptureSource = CaptureSource.TEXT
    text: str | None = Field(default=None, max_length=20000)
    audio_base64: str | None = None
    image_base64: str | None = None
    route_mode: RouteMode = RouteMode.GLOBAL
    active_person_id: UUID | None = None
    keep_original_media: bool = False
    sync: bool | None = None


class CaptureResponse(CamelModel):
    capture_id: UUID
    source: CaptureSource
    route_mode: RouteMode
    scoped_person_id: UUID | None = None
    status: CaptureStatus
    transcript: str = ""
    transcript_html: str = ""
    splits: list[CaptureSplitDto] = Field(default_factory=list)
    pending_count: int = 0
    inbox_item_ids: list[UUID] = Field(default_factory=list)
    safety_category: SafetyCategory = SafetyCategory.NORMAL
    crisis_card: CrisisCardDto | None = None
    undo_deadline: datetime | None = None
    created_at: datetime
    completed_at: datetime | None = None


class VoiceIngestResponse(CamelModel):
    """Exact V10 `/voice/ingest` compatibility contract + M2 pending/undo/crisis extensions."""

    job_id: UUID
    route_mode: RouteMode
    transcript_html: str
    splits: list[CaptureSplitDto] = Field(default_factory=list)
    pending_count: int = 0
    undo_deadline: datetime | None = None
    crisis_card: CrisisCardDto | None = None


class CaptureUndoResponse(CamelModel):
    capture_id: UUID
    status: CaptureStatus
    reverted_record_count: int
    rejected_fact_count: int
    cancelled_reminder_count: int
