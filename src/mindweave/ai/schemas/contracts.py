"""Frozen Pydantic v2 contracts for AI Gateway tasks (shared across AI, Capture, Companion, Safety, and Evals)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mindweave.shared.enums import (
    FactType,
    Polarity,
    SafetyCategory,
    SourceSpeaker,
    Tier,
    TimeStatus,
)


class KnownPersonRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: UUID
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    tier: Tier = Tier.CRYSTAL
    role_tag: str = ""
    recent_facts: list[str] = Field(default_factory=list)


class TranscribeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio_bytes: bytes
    mime_type: str = "audio/webm"
    known_person_names: list[str] = Field(default_factory=list)
    language: str = "zh-CN"
    active_person_name: str | None = None
    recent_context_snippets: list[str] = Field(default_factory=list)


class TranscribeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_transcript: str
    corrected_transcript: str
    duration_ms: int | None = None


class ScreenshotBubble(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: SourceSpeaker  # target_person (left) | user_self (right) | unknown (group/system)
    speaker_label: str = ""
    text: str


class ScreenshotParseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_group_chat: bool = False
    bubbles: list[ScreenshotBubble] = Field(default_factory=list)
    formatted_transcript: str = ""


class ExtractedPersonCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mention: str
    matched_person_id: UUID | None = None
    candidate_person_ids: list[UUID] = Field(default_factory=list)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    is_new_person: bool = False


class ExtractedFactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_mention: str
    person_id: UUID | None = None
    fact_type: FactType
    polarity: Polarity = Polarity.NEUTRAL
    time_status: TimeStatus = TimeStatus.CURRENT
    source_speaker: SourceSpeaker = SourceSpeaker.NARRATOR
    title: str = ""
    content: str
    trigger_date: str | None = None  # YYYY-MM-DD when known
    is_lunar: bool = False
    lunar_month: int | None = None
    lunar_day: int | None = None
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    event_group: str | None = None


class ExtractRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    current_date: str  # YYYY-MM-DD
    route_mode: str = "global"  # global | current
    scoped_person_id: UUID | None = None
    scoped_person_tier: Tier | None = None
    input_source: str = "text"  # voice | text | image | chat
    known_persons: list[KnownPersonRef] = Field(default_factory=list)
    recent_history_hints: list[str] = Field(default_factory=list)


class ExtractRecordOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safety_flag: SafetyCategory = SafetyCategory.NORMAL
    crisis_flag: bool = False
    is_venting_or_irony: bool = False
    needs_disambiguation: bool = False
    scoped_person_locked: bool = False
    suggest_new_person: str | None = None
    persons: list[ExtractedPersonCandidate] = Field(default_factory=list)
    facts: list[ExtractedFactItem] = Field(default_factory=list)


class ContextFactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: UUID
    fact_id: UUID
    fact_type: FactType
    content: str
    happened_on: str | None = None


class ContextMaterialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: UUID
    kind: str
    title: str
    body: str
    happened_on: str | None = None


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str  # user | assistant
    content: str


class CompanionChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: UUID
    person_name: str
    tier: Tier
    role_tag: str = ""
    traits: list[str] = Field(default_factory=list)
    confirmed_facts: list[ContextFactItem] = Field(default_factory=list)
    referenced_materials: list[ContextMaterialItem] = Field(default_factory=list)
    rolling_summary: str | None = None
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    user_message: str
    safety_override: SafetyCategory = SafetyCategory.NORMAL


class CompanionChatOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    safety_flag: SafetyCategory = SafetyCategory.NORMAL
    reply_text: str
    whisper_note: str | None = None
    cited_record_ids: list[UUID] = Field(default_factory=list)
    has_unrecorded_gap: bool = False
    alternative_perspectives: list[str] = Field(default_factory=list)
    actionable_suggestions: list[str] = Field(default_factory=list)
    disclaimer: str | None = None


class SafetyClassifyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: SafetyCategory = SafetyCategory.NORMAL
    is_hyperbole_or_joke: bool = False
    rationale_code: str = "normal"


class CrisisCardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str  # crisis_self_harm_v1 | domestic_violence_v1
    title: str
    care_message: str
    hotlines: list[dict[str, str]]
    disclaimer: str


class SafetyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: SafetyCategory
    block_normal_reply: bool = False
    crisis_card: CrisisCardPayload | None = None
    trigger_source: str = "l1"  # l1 | l2 | merged
