"""Deterministic Server-Side Write-Before-Confirm GatePolicy (Invariant I3).

LLM extraction only emits signals (`confidence`, `is_venting_or_irony`, `fact_type`,
`source_speaker`, etc.). Whether a fact enters `confirmed` (with a 5-second undo window)
or `pending` (routed to the Pending Inbox) is decided 100% deterministically here.

Multi-person global voice note:
- In `route_mode == "global"`, distinct clauses cleanly attributed to distinct known
  persons (`clause_person_unambiguous=True`) produce per-person `RecordRow` (`splits[]`)
  for immediate visual feedback and then pass through checks #5..#8 individually.
- Conversely, an entangled multi-person clause (2+ people in the same fact or an
  unresolved pronoun across multiple mentioned people, i.e. `multi_person=True` with
  `scoped_locked=False` and `clause_person_unambiguous` not True) MUST enter `pending`
  with `PendingReason.MULTI_PERSON`.

D1 pencil drafts (roadmap 0.6):
- Dates and promises (#5) are no longer parked in the inbox. They are CONFIRMED (so the
  reminder is created exactly as for any confirmed date) but flagged `needs_glance=True`
  and presented as a pencil draft the user can glance at / fix in place.
- Because #5 now confirms, it is evaluated AFTER every pending rule: a low-confidence or
  vented date must still stay pending.
- `presentation` tells the client how to draw the slip: `ink` (settled), `pencil` (draft
  still visible on the person's page) or `question` (needs the user to pick/confirm a
  person).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemKind,
    PendingReason,
    PersonState,
    Presentation,
    RecordReviewState,
    SourceSpeaker,
    Tier,
)


@dataclass(frozen=True, slots=True)
class GateConfig:
    confidence_threshold: float = 0.80
    undo_window_seconds: int = 5

    @property
    def threshold(self) -> float:
        return self.confidence_threshold


@dataclass(frozen=True, slots=True)
class GateInput:
    fact_type: FactType = FactType.PREFERENCE
    confidence: float = 0.95
    candidate_count: int = 1
    multi_person: bool = False
    scoped_locked: bool = False
    clause_person_unambiguous: bool | None = None
    is_venting: bool = False
    tier: Tier = Tier.CRYSTAL
    person_state: PersonState = PersonState.ACTIVE
    source_speaker: SourceSpeaker = SourceSpeaker.NARRATOR
    polarity_flipped: bool = False
    extraction_ok: bool = True


@dataclass(frozen=True, slots=True)
class GateDecision:
    status: FactStatus
    pending_reason: PendingReason | None = None
    record_review_state: RecordReviewState = RecordReviewState.NORMAL
    inbox_kind: InboxItemKind | None = None
    undo_window_seconds: int = 0
    undo_deadline: datetime | None = None
    needs_glance: bool = False
    presentation: Presentation = Presentation.INK

    @property
    def is_confirmed(self) -> bool:
        return self.status is FactStatus.CONFIRMED

    @property
    def is_pending(self) -> bool:
        return self.status is FactStatus.PENDING

    def with_now(self, now: datetime) -> GateDecision:
        if self.status is not FactStatus.CONFIRMED or self.undo_window_seconds <= 0:
            return self
        return GateDecision(
            status=self.status,
            pending_reason=self.pending_reason,
            record_review_state=self.record_review_state,
            inbox_kind=self.inbox_kind,
            undo_window_seconds=self.undo_window_seconds,
            undo_deadline=now + timedelta(seconds=self.undo_window_seconds),
            needs_glance=self.needs_glance,
            presentation=self.presentation,
        )


def _pending(
    reason: PendingReason,
    *,
    inbox_kind: InboxItemKind = InboxItemKind.FACT_CONFIRM,
    record_review_state: RecordReviewState = RecordReviewState.NORMAL,
    presentation: Presentation = Presentation.PENCIL,
) -> GateDecision:
    return GateDecision(
        status=FactStatus.PENDING,
        pending_reason=reason,
        record_review_state=record_review_state,
        inbox_kind=inbox_kind,
        undo_window_seconds=0,
        undo_deadline=None,
        presentation=presentation,
    )


def decide_gate(
    gate_input: GateInput,
    config: GateConfig = GateConfig(),
    *,
    now: datetime | None = None,
) -> GateDecision:
    """Evaluate all 8 gate conditions deterministically (pending rules first, then #5)."""
    # 1. Extraction or schema validation failed
    if not gate_input.extraction_ok:
        return _pending(PendingReason.EXTRACTION_FAILED, inbox_kind=InboxItemKind.FACT_CONFIRM)

    # 2. Newly discovered provisional person requires confirmation
    if gate_input.person_state is PersonState.PROVISIONAL:
        return _pending(
            PendingReason.NEW_PERSON,
            inbox_kind=InboxItemKind.NEW_PERSON_CONFIRM,
            presentation=Presentation.QUESTION,
        )

    # 3. Ambiguous or unresolved person candidate count != 1
    if gate_input.candidate_count != 1:
        return _pending(
            PendingReason.AMBIGUOUS_PERSON,
            inbox_kind=InboxItemKind.PERSON_DISAMBIGUATION,
            record_review_state=RecordReviewState.HELD,
            presentation=Presentation.QUESTION,
        )

    # 4. Multi-person entangled clause when not scoped-locked
    if (
        gate_input.multi_person
        and not gate_input.scoped_locked
        and gate_input.clause_person_unambiguous is not True
    ):
        return _pending(
            PendingReason.MULTI_PERSON,
            inbox_kind=InboxItemKind.FACT_CONFIRM,
            presentation=Presentation.QUESTION,
        )

    # 6. Angry venting / irony against a `gold` (记爱) partner goes to venting_hold (ADR-005 PRT-01)
    if gate_input.is_venting and gate_input.tier is Tier.GOLD:
        return _pending(
            PendingReason.VENTING,
            inbox_kind=InboxItemKind.VENTING_HOLD,
            record_review_state=RecordReviewState.HELD,
        )

    # 7. Group chat / screenshot where speaker identity is unknown
    if gate_input.source_speaker is SourceSpeaker.UNKNOWN:
        return _pending(PendingReason.SPEAKER_UNKNOWN, inbox_kind=InboxItemKind.FACT_CONFIRM)

    # 8a. Contradicts / flips polarity against an active confirmed fact
    if gate_input.polarity_flipped:
        return _pending(PendingReason.POLARITY_FLIP, inbox_kind=InboxItemKind.FACT_CONFIRM)

    # 8b. Below configured confidence threshold
    if gate_input.confidence < config.confidence_threshold:
        return _pending(PendingReason.LOW_CONFIDENCE, inbox_kind=InboxItemKind.FACT_CONFIRM)

    deadline = (
        now + timedelta(seconds=config.undo_window_seconds)
        if now is not None and config.undo_window_seconds > 0
        else None
    )

    # 5. Dates and promises: confirmed (reminder created as usual) but drawn in pencil so the
    #    user can glance at the parsed date and fix it in place (D1, replaces inbox parking).
    if gate_input.fact_type in (FactType.DATE, FactType.PROMISE):
        return GateDecision(
            status=FactStatus.CONFIRMED,
            pending_reason=None,
            record_review_state=RecordReviewState.NORMAL,
            inbox_kind=None,
            undo_window_seconds=config.undo_window_seconds,
            undo_deadline=deadline,
            needs_glance=True,
            presentation=Presentation.PENCIL,
        )

    return GateDecision(
        status=FactStatus.CONFIRMED,
        pending_reason=None,
        record_review_state=RecordReviewState.NORMAL,
        inbox_kind=None,
        undo_window_seconds=config.undo_window_seconds,
        undo_deadline=deadline,
    )


# Alias matching Master Plan snippet
decide = decide_gate
