"""D1 pencil-draft presentation rules shared by capture (splits) and memory (person page).

Pure functions only: no I/O, no ORM. Every user-facing hint is short natural Chinese with
zero developer jargon.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemKind,
    PencilReason,
    PendingReason,
    Presentation,
    RecordReviewState,
)

AUTO_INK_DELAY = timedelta(hours=48)

# Only "probably right, just unsure" drafts auto-promote. Venting never does (ADR-005), and
# person questions always need the user.
AUTO_INK_PENDING_REASONS: frozenset[PendingReason] = frozenset(
    {
        PendingReason.LOW_CONFIDENCE,
        PendingReason.SPEAKER_UNKNOWN,
        PendingReason.POLARITY_FLIP,
    }
)

_QUESTION_REASONS: frozenset[PencilReason] = frozenset(
    {PencilReason.NEW_PERSON, PencilReason.AMBIGUOUS_PERSON, PencilReason.MULTI_PERSON}
)
QUESTION_INBOX_KINDS: frozenset[InboxItemKind] = frozenset(
    {InboxItemKind.NEW_PERSON_CONFIRM, InboxItemKind.PERSON_DISAMBIGUATION}
)

# When several facts on one slip disagree, surface the most actionable reason first.
_PENCIL_PRIORITY: tuple[PencilReason, ...] = (
    PencilReason.NEW_PERSON,
    PencilReason.AMBIGUOUS_PERSON,
    PencilReason.MULTI_PERSON,
    PencilReason.VENTING,
    PencilReason.POLARITY_FLIP,
    PencilReason.SPEAKER_UNKNOWN,
    PencilReason.LOW_CONFIDENCE,
    PencilReason.EXTRACTION_FAILED,
    PencilReason.DATE_GLANCE,
)

_PENDING_TO_PENCIL: dict[PendingReason, PencilReason] = {
    PendingReason.NEW_PERSON: PencilReason.NEW_PERSON,
    PendingReason.AMBIGUOUS_PERSON: PencilReason.AMBIGUOUS_PERSON,
    PendingReason.MULTI_PERSON: PencilReason.MULTI_PERSON,
    PendingReason.VENTING: PencilReason.VENTING,
    PendingReason.POLARITY_FLIP: PencilReason.POLARITY_FLIP,
    PendingReason.SPEAKER_UNKNOWN: PencilReason.SPEAKER_UNKNOWN,
    PendingReason.LOW_CONFIDENCE: PencilReason.LOW_CONFIDENCE,
    PendingReason.EXTRACTION_FAILED: PencilReason.EXTRACTION_FAILED,
    # Legacy rows written before D1 (and chat write-back) still carry this reason.
    PendingReason.DATE_OR_PROMISE: PencilReason.DATE_GLANCE,
}

_INBOX_KIND_TO_PENCIL: dict[InboxItemKind, PencilReason] = {
    InboxItemKind.NEW_PERSON_CONFIRM: PencilReason.NEW_PERSON,
    InboxItemKind.PERSON_DISAMBIGUATION: PencilReason.AMBIGUOUS_PERSON,
    InboxItemKind.VENTING_HOLD: PencilReason.VENTING,
}

_REASON_HINTS: dict[PencilReason, str] = {
    PencilReason.LOW_CONFIDENCE: "没太听清 · 点一下确认",
    PencilReason.SPEAKER_UNKNOWN: "不确定是谁说的 · 点一下确认",
    PencilReason.POLARITY_FLIP: "和之前记的不一样 · 点一下确认",
    PencilReason.EXTRACTION_FAILED: "先替你记下原话 · 点一下整理",
    PencilReason.VENTING: "先替你收在冷静区",
    PencilReason.NEW_PERSON: "第一次提到 TA · 点一下确认",
    PencilReason.AMBIGUOUS_PERSON: "说的是哪一位？点一下选",
    PencilReason.MULTI_PERSON: "说的是哪一位？点一下选",
    PencilReason.DATE_GLANCE: "点一下确认日期",
}

_LUNAR_MONTH_NAMES = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
_CN_DIGITS = ("", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def pencil_reason_for_pending(reason: PendingReason) -> PencilReason:
    return _PENDING_TO_PENCIL[reason]


def presentation_for_reason(reason: PencilReason | None) -> Presentation:
    if reason is None:
        return Presentation.INK
    if reason in _QUESTION_REASONS:
        return Presentation.QUESTION
    return Presentation.PENCIL


def is_auto_ink_eligible(reasons: Iterable[PendingReason | None]) -> bool:
    """True only when every pending reason on an item is an auto-promotable kind."""
    materialized = list(reasons)
    return bool(materialized) and all(
        r is not None and r in AUTO_INK_PENDING_REASONS for r in materialized
    )


def auto_ink_due_at(created_at: datetime) -> datetime:
    return created_at + AUTO_INK_DELAY


def lunar_day_label(day: int) -> str:
    if day == 10:
        return "初十"
    if day == 20:
        return "二十"
    if day == 30:
        return "三十"
    if day < 10:
        return f"初{_CN_DIGITS[day]}"
    if day < 20:
        return f"十{_CN_DIGITS[day - 10]}"
    return f"廿{_CN_DIGITS[day - 20]}"


def format_fact_date_label(
    *,
    trigger_date: date | None,
    is_lunar: bool = False,
    lunar_month: int | None = None,
    lunar_day: int | None = None,
) -> str | None:
    """`5月12日` / `农历八月十五`; None when the fact carries no usable date."""
    if is_lunar and lunar_month and lunar_day and 1 <= lunar_month <= 12 and 1 <= lunar_day <= 30:
        return f"农历{_LUNAR_MONTH_NAMES[lunar_month - 1]}月{lunar_day_label(lunar_day)}"
    if trigger_date is not None:
        return f"{trigger_date.month}月{trigger_date.day}日"
    return None


def build_glance_hint(
    *,
    fact_type: FactType,
    trigger_date: date | None,
    is_lunar: bool = False,
    lunar_month: int | None = None,
    lunar_day: int | None = None,
) -> str:
    label = format_fact_date_label(
        trigger_date=trigger_date,
        is_lunar=is_lunar,
        lunar_month=lunar_month,
        lunar_day=lunar_day,
    )
    if label is not None:
        return f"{label} · 点一下确认日期"
    if fact_type is FactType.PROMISE:
        return "答应过的事 · 点一下补上日期"
    return "还没定日子 · 点一下补上日期"


def hint_for_reason(reason: PencilReason) -> str:
    return _REASON_HINTS[reason]


@dataclass(frozen=True, slots=True)
class PencilFactView:
    """Minimal fact projection needed to decide a slip's pencil state."""

    fact_type: FactType
    status: FactStatus
    pending_reason: PendingReason | None = None
    needs_glance: bool = False
    trigger_date: date | None = None
    is_lunar: bool = False
    lunar_month: int | None = None
    lunar_day: int | None = None

    @property
    def is_pencil(self) -> bool:
        return self.status is FactStatus.PENDING or (
            self.status is FactStatus.CONFIRMED and self.needs_glance
        )


@dataclass(frozen=True, slots=True)
class PencilState:
    presentation: Presentation
    pencil_reason: PencilReason | None = None
    glance_hint: str | None = None


INK_STATE = PencilState(presentation=Presentation.INK)


def derive_pencil_state(
    *,
    review_state: RecordReviewState = RecordReviewState.NORMAL,
    open_inbox_kinds: Sequence[InboxItemKind] = (),
    facts: Sequence[PencilFactView] = (),
    is_new_person: bool = False,
) -> PencilState:
    """Collapse a slip's facts + open review items into one D1 presentation."""
    if review_state is RecordReviewState.NOTE_ONLY and not open_inbox_kinds:
        return INK_STATE

    candidates: set[PencilReason] = set()
    if is_new_person:
        candidates.add(PencilReason.NEW_PERSON)
    for kind in open_inbox_kinds:
        mapped = _INBOX_KIND_TO_PENCIL.get(kind)
        if mapped is not None:
            candidates.add(mapped)
    glance_fact: PencilFactView | None = None
    for fact in facts:
        if fact.status is FactStatus.PENDING and fact.pending_reason is not None:
            candidates.add(pencil_reason_for_pending(fact.pending_reason))
            if fact.pending_reason is PendingReason.DATE_OR_PROMISE and glance_fact is None:
                glance_fact = fact
        elif fact.status is FactStatus.CONFIRMED and fact.needs_glance:
            candidates.add(PencilReason.DATE_GLANCE)
            if glance_fact is None:
                glance_fact = fact
    if review_state is RecordReviewState.HELD and not candidates:
        candidates.add(PencilReason.VENTING)
    if any(k in QUESTION_INBOX_KINDS for k in open_inbox_kinds) and not (
        candidates & _QUESTION_REASONS
    ):
        candidates.add(PencilReason.AMBIGUOUS_PERSON)

    if not candidates:
        return INK_STATE
    reason = next(r for r in _PENCIL_PRIORITY if r in candidates)
    if reason is PencilReason.DATE_GLANCE and glance_fact is not None:
        hint = build_glance_hint(
            fact_type=glance_fact.fact_type,
            trigger_date=glance_fact.trigger_date,
            is_lunar=glance_fact.is_lunar,
            lunar_month=glance_fact.lunar_month,
            lunar_day=glance_fact.lunar_day,
        )
    else:
        hint = hint_for_reason(reason)
    return PencilState(
        presentation=presentation_for_reason(reason),
        pencil_reason=reason,
        glance_hint=hint,
    )
