"""CamelCase DTOs shared between Companion application & API layers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, Field

from mindweave.ai.schemas.contracts import CrisisCardPayload
from mindweave.core.dto import CamelModel, CursorPage
from mindweave.modules.companion.infra.repository import HydratedChatMessage, HydratedCitation
from mindweave.shared.enums import ChatRole, SafetyCategory, Tier


class SendChatMessageRequest(CamelModel):
    """Request payload for `POST /persons/{person_id}/chat/messages`."""

    message: str = Field(..., min_length=1, max_length=4000)
    referenced_material_ids: list[UUID] = Field(default_factory=list)
    client_message_id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("clientMessageId", "clientMsgId", "client_message_id"),
    )


class StreamChatRequest(CamelModel):
    """V10 frontend SSE compatibility payload for `POST /persons/{person_id}/chat/stream`."""

    message: str = Field(..., min_length=1, max_length=4000)
    referenced_material_ids: list[UUID] = Field(default_factory=list)
    client_message_id: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("clientMessageId", "clientMsgId", "client_message_id"),
    )


class ChatCitationDto(CamelModel):
    index: int
    material_id: UUID
    title: str
    snippet: str
    happened_on: str | None = None

    @classmethod
    def from_hydrated(cls, item: HydratedCitation) -> ChatCitationDto:
        return cls(
            index=item.index,
            material_id=item.material_id,
            title=item.title,
            snippet=item.snippet,
            happened_on=item.happened_on,
        )


class CrisisCardDto(CamelModel):
    template_id: str
    title: str
    care_message: str
    hotlines: list[dict[str, str]]
    disclaimer: str

    @classmethod
    def from_payload(cls, card: CrisisCardPayload) -> CrisisCardDto:
        return cls(
            template_id=card.template_id,
            title=card.title,
            care_message=card.care_message,
            hotlines=card.hotlines,
            disclaimer=card.disclaimer,
        )


class ChatMessageDto(CamelModel):
    id: UUID
    person_id: UUID
    thread_id: UUID
    role: ChatRole
    content: str
    whisper_note: str | None = None
    perspectives: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    citations: list[ChatCitationDto] = Field(default_factory=list)
    referenced_material_ids: list[UUID] = Field(default_factory=list)
    cited_material_ids: list[UUID] = Field(default_factory=list)
    tier_snapshot: Tier
    safety_category: SafetyCategory = SafetyCategory.NORMAL
    has_unrecorded_gap: bool = False
    client_message_id: str | None = None
    created_at: datetime

    @classmethod
    def from_hydrated(cls, msg: HydratedChatMessage) -> ChatMessageDto:
        citation_dtos = [ChatCitationDto.from_hydrated(c) for c in msg.citations]
        return cls(
            id=msg.id,
            person_id=msg.person_id,
            thread_id=msg.thread_id,
            role=msg.role,
            content=msg.content,
            whisper_note=msg.whisper_note,
            perspectives=msg.perspectives if msg.tier_snapshot is Tier.GOLD else [],
            suggestions=msg.suggestions,
            citations=citation_dtos,
            referenced_material_ids=msg.referenced_material_ids,
            cited_material_ids=[c.material_id for c in citation_dtos],
            tier_snapshot=msg.tier_snapshot,
            safety_category=msg.safety_category,
            has_unrecorded_gap=msg.has_unrecorded_gap,
            client_message_id=msg.client_message_id,
            created_at=msg.created_at,
        )


class ChatTurnResponse(CamelModel):
    """Non-streaming JSON response for `POST /persons/{person_id}/chat/messages?stream=false`."""

    message_id: UUID
    user_message: ChatMessageDto
    assistant_message: ChatMessageDto
    tier_snapshot: Tier
    has_unrecorded_gap: bool = False
    cited_material_ids: list[UUID] = Field(default_factory=list)
    crisis_card: CrisisCardDto | None = None

    def to_idempotency_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class ClearChatHistoryResponse(CamelModel):
    person_id: UUID
    deleted_count: int


ChatMessagePage = CursorPage[ChatMessageDto]
