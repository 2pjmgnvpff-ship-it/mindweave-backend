"""Pure unit tests for D1 pencil-draft presentation rules (roadmap 0.6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from mindweave.core.dto import contains_forbidden_dev_jargon
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemKind,
    PencilReason,
    PendingReason,
    Presentation,
    RecordReviewState,
)
from mindweave.shared.pencil import (
    AUTO_INK_DELAY,
    INK_STATE,
    PencilFactView,
    auto_ink_due_at,
    build_glance_hint,
    derive_pencil_state,
    format_fact_date_label,
    hint_for_reason,
    is_auto_ink_eligible,
    lunar_day_label,
)


def test_glance_hint_solar_and_lunar_and_undated() -> None:
    assert (
        build_glance_hint(fact_type=FactType.DATE, trigger_date=date(2027, 5, 12))
        == "5月12日 · 点一下确认日期"
    )
    assert (
        build_glance_hint(
            fact_type=FactType.DATE,
            trigger_date=None,
            is_lunar=True,
            lunar_month=8,
            lunar_day=15,
        )
        == "农历八月十五 · 点一下确认日期"
    )
    assert build_glance_hint(fact_type=FactType.PROMISE, trigger_date=None).endswith("补上日期")
    assert build_glance_hint(fact_type=FactType.DATE, trigger_date=None).endswith("补上日期")


def test_lunar_labels() -> None:
    assert [lunar_day_label(d) for d in (1, 10, 11, 20, 21, 30)] == [
        "初一",
        "初十",
        "十一",
        "二十",
        "廿一",
        "三十",
    ]
    assert format_fact_date_label(
        trigger_date=None, is_lunar=True, lunar_month=12, lunar_day=8
    ) == ("农历腊月初八")
    assert format_fact_date_label(trigger_date=None) is None


def test_all_hints_are_jargon_free_chinese() -> None:
    for reason in PencilReason:
        hint = hint_for_reason(reason)
        assert hint
        assert not contains_forbidden_dev_jargon(hint)
        assert not any(tok in hint.lower() for tok in ("pending", "inbox", "fact", "null"))


def test_auto_ink_eligibility() -> None:
    assert is_auto_ink_eligible([PendingReason.LOW_CONFIDENCE])
    assert is_auto_ink_eligible([PendingReason.SPEAKER_UNKNOWN, PendingReason.POLARITY_FLIP])
    assert not is_auto_ink_eligible([])
    assert not is_auto_ink_eligible([PendingReason.VENTING])
    assert not is_auto_ink_eligible([PendingReason.LOW_CONFIDENCE, PendingReason.MULTI_PERSON])
    assert not is_auto_ink_eligible([PendingReason.NEW_PERSON])
    assert not is_auto_ink_eligible([None])
    now = datetime(2026, 11, 12, tzinfo=UTC)
    assert auto_ink_due_at(now) == now + timedelta(hours=48) == now + AUTO_INK_DELAY


def test_derive_pencil_state_ink_when_settled() -> None:
    confirmed = PencilFactView(fact_type=FactType.PREFERENCE, status=FactStatus.CONFIRMED)
    assert derive_pencil_state(facts=[confirmed]) == INK_STATE
    assert derive_pencil_state() == INK_STATE


def test_derive_pencil_state_date_glance() -> None:
    glance = PencilFactView(
        fact_type=FactType.DATE,
        status=FactStatus.CONFIRMED,
        needs_glance=True,
        trigger_date=date(2027, 5, 12),
    )
    state = derive_pencil_state(facts=[glance])
    assert state.presentation is Presentation.PENCIL
    assert state.pencil_reason is PencilReason.DATE_GLANCE
    assert state.glance_hint == "5月12日 · 点一下确认日期"


def test_derive_pencil_state_priorities() -> None:
    low = PencilFactView(
        fact_type=FactType.PREFERENCE,
        status=FactStatus.PENDING,
        pending_reason=PendingReason.LOW_CONFIDENCE,
    )
    glance = PencilFactView(fact_type=FactType.DATE, status=FactStatus.CONFIRMED, needs_glance=True)
    state = derive_pencil_state(facts=[glance, low], open_inbox_kinds=[InboxItemKind.FACT_CONFIRM])
    assert state.pencil_reason is PencilReason.LOW_CONFIDENCE
    assert state.presentation is Presentation.PENCIL

    new_person = derive_pencil_state(
        facts=[low],
        open_inbox_kinds=[InboxItemKind.NEW_PERSON_CONFIRM],
        is_new_person=True,
    )
    assert new_person.presentation is Presentation.QUESTION
    assert new_person.pencil_reason is PencilReason.NEW_PERSON

    ambiguous = derive_pencil_state(
        review_state=RecordReviewState.HELD,
        open_inbox_kinds=[InboxItemKind.PERSON_DISAMBIGUATION],
    )
    assert ambiguous.presentation is Presentation.QUESTION

    venting = derive_pencil_state(
        review_state=RecordReviewState.HELD,
        open_inbox_kinds=[InboxItemKind.VENTING_HOLD],
    )
    assert venting.presentation is Presentation.PENCIL
    assert venting.pencil_reason is PencilReason.VENTING
    assert venting.glance_hint == "先替你收在冷静区"


def test_legacy_date_or_promise_pending_maps_to_glance() -> None:
    legacy = PencilFactView(
        fact_type=FactType.DATE,
        status=FactStatus.PENDING,
        pending_reason=PendingReason.DATE_OR_PROMISE,
        trigger_date=date(2027, 1, 3),
    )
    state = derive_pencil_state(facts=[legacy], open_inbox_kinds=[InboxItemKind.FACT_CONFIRM])
    assert state.pencil_reason is PencilReason.DATE_GLANCE
    assert state.glance_hint == "1月3日 · 点一下确认日期"
