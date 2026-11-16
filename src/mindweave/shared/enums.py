"""Canonical domain enumerations shared across MindWeave modules."""

from __future__ import annotations

from enum import StrEnum

UNASSIGNED_PERSON_SLUG = "__unassigned__"


class Tier(StrEnum):
    """Three-state Arrow Tier (canonical replacement for legacy love/matter/hate)."""

    GOLD = "gold"  # 金箭 · 记爱 (coreIds)
    SILVER = "silver"  # 银箭 · 记恨 (coreIds)
    CRYSTAL = "crystal"  # 水晶箭 · 记繁琐 (casualIds)

    @property
    def is_core(self) -> bool:
        return self in (Tier.GOLD, Tier.SILVER)

    @property
    def allows_perspectives(self) -> bool:
        return self is Tier.GOLD

    @classmethod
    def from_any(cls, value: str) -> Tier:
        normalized = value.strip().lower()
        legacy_map = {
            "love": cls.GOLD,
            "hate": cls.SILVER,
            "matter": cls.CRYSTAL,
            "casual": cls.CRYSTAL,
        }
        if normalized in legacy_map:
            return legacy_map[normalized]
        return cls(normalized)


class Edition(StrEnum):
    """Journal visual editions aligned with V10 frontend."""

    COTTON = "cotton"
    DOTGRID = "dotgrid"
    PARCHMENT = "parchment"
    TRAVELER = "traveler"
    HOBONICHI = "hobonichi"
    MUSEGLASS = "museglass"


class CupidLayout(StrEnum):
    """Muse top-bar Cupid medallion schemes aligned with V10 frontend."""

    A = "a"
    B = "b"
    C = "c"
    D = "d"


class PersonState(StrEnum):
    ACTIVE = "active"
    PROVISIONAL = "provisional"
    ARCHIVED = "archived"


class AliasKind(StrEnum):
    NAME = "name"
    NICKNAME = "nickname"
    TITLE = "title"


class CaptureSource(StrEnum):
    VOICE = "voice"
    TEXT = "text"
    IMAGE = "image"
    CHAT = "chat"


class RouteMode(StrEnum):
    GLOBAL = "global"
    CURRENT = "current"


class CaptureStatus(StrEnum):
    RECEIVED = "received"
    TRANSCRIBING = "transcribing"
    EXTRACTING = "extracting"
    ROUTED = "routed"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    SAVED_AS_NOTE = "saved_as_note"
    REVERTED = "reverted"


class RecordKind(StrEnum):
    """Scrapbook material type (exposed as `type` / `kind` in Materials API)."""

    PHOTO = "photo"
    NOTE = "note"
    VOICE = "voice"


class RecordReviewState(StrEnum):
    NORMAL = "normal"
    HELD = "held"
    NOTE_ONLY = "note_only"


class FactType(StrEnum):
    PREFERENCE = "preference"
    TABOO = "taboo"
    DATE = "date"
    WISH = "wish"
    PROMISE = "promise"
    EVENT = "event"
    NOTE = "note"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class TimeStatus(StrEnum):
    CURRENT = "current"
    PAST = "past"


class SourceSpeaker(StrEnum):
    TARGET_PERSON = "target_person"
    USER_SELF = "user_self"
    NARRATOR = "narrator"
    UNKNOWN = "unknown"


class FactStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CreatedBy(StrEnum):
    AI = "ai"
    USER = "user"


class PendingReason(StrEnum):
    AMBIGUOUS_PERSON = "ambiguous_person"
    MULTI_PERSON = "multi_person"
    DATE_OR_PROMISE = "date_or_promise"
    LOW_CONFIDENCE = "low_confidence"
    VENTING = "venting"
    NEW_PERSON = "new_person"
    SPEAKER_UNKNOWN = "speaker_unknown"
    POLARITY_FLIP = "polarity_flip"
    EXTRACTION_FAILED = "extraction_failed"


class Presentation(StrEnum):
    """D1 journal rendering of a capture result: ink (settled), pencil (draft), question."""

    INK = "ink"
    PENCIL = "pencil"
    QUESTION = "question"


class PencilReason(StrEnum):
    """Why a material is shown as a pencil draft / question note (wire value `pencilReason`)."""

    DATE_GLANCE = "date_glance"
    LOW_CONFIDENCE = "low_confidence"
    SPEAKER_UNKNOWN = "speaker_unknown"
    POLARITY_FLIP = "polarity_flip"
    MULTI_PERSON = "multi_person"
    EXTRACTION_FAILED = "extraction_failed"
    VENTING = "venting"
    NEW_PERSON = "new_person"
    AMBIGUOUS_PERSON = "ambiguous_person"


class InboxItemKind(StrEnum):
    PERSON_DISAMBIGUATION = "person_disambiguation"
    NEW_PERSON_CONFIRM = "new_person_confirm"
    FACT_CONFIRM = "fact_confirm"
    VENTING_HOLD = "venting_hold"


class InboxItemStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class InboxResolutionAction(StrEnum):
    CONFIRM = "confirm"
    KEEP_AS_NOTE = "keep_as_note"
    DISCARD = "discard"
    ASSIGN_PERSON = "assign_person"
    CREATE_PERSON = "create_person"
    MERGE_PERSON = "merge_person"


class SafetyCategory(StrEnum):
    NORMAL = "normal"
    CRISIS = "crisis"
    VIOLENCE_VICTIM = "violence_victim"
    ILLEGAL_INTENT = "illegal_intent"
    THIRD_PARTY_ATTACK = "third_party_attack"
    BOUNDARY_REFUSAL = "boundary_refusal"


class ReminderRule(StrEnum):
    ONCE = "once"
    YEARLY = "yearly"


class ReminderStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PushPlatform(StrEnum):
    IOS_APNS = "ios_apns"
    WEB_PUSH = "web_push"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ConsentKind(StrEnum):
    PRIVACY_POLICY = "privacy_policy"
    SENSITIVE_PERSONAL_INFO = "sensitive_personal_info"
    AI_PROCESSING = "ai_processing"


class ExportStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
