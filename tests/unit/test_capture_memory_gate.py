"""Pure unit tests for GatePolicy, Screenshot Right-Bubble Filter, Bi-Temporal Supersession,
XSS-Safe Transcript Highlighter, Media Magic-Byte Sniffer, EXIF Sanitizer, and Routing."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest
from PIL import Image

from mindweave.core.dto import contains_forbidden_dev_jargon, strip_forbidden_dev_jargon
from mindweave.core.errors import PayloadTooLargeError, ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.modules.capture.domain.gate_policy import (
    GateConfig,
    GateInput,
    decide,
    decide_gate,
)
from mindweave.modules.capture.domain.highlighter import (
    build_natural_slip_tag,
    build_transcript_html,
    format_duration_mmss,
)
from mindweave.modules.capture.domain.media_sniffer import (
    InvalidMediaFormatError,
    MediaTooLargeError,
    validate_and_sniff_audio,
)
from mindweave.modules.capture.domain.routing import (
    KnownPersonProfile,
    is_non_person_stopword_mention,
    resolve_mention_for_fact,
)
from mindweave.modules.capture.domain.screenshot_rules import (
    filter_screenshot_facts,
    format_screenshot_bubbles,
    partition_screenshot_facts,
)
from mindweave.modules.capture.infra.image_sanitizer import validate_and_strip_image_exif
from mindweave.modules.memory.domain.bitemporal import (
    ExistingFactSnapshot,
    NewFactCandidate,
    detect_polarity_flip,
    extract_topic_tokens,
    find_facts_to_supersede,
)
from mindweave.modules.memory.domain.inbox_policy import (
    InboxDomainError,
    InboxResolutionInput,
    validate_inbox_resolution,
)
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PendingReason,
    PersonState,
    Polarity,
    Presentation,
    RecordReviewState,
    RouteMode,
    SourceSpeaker,
    Tier,
    TimeStatus,
)


@dataclass(frozen=True)
class _SampleFact:
    fact_type: FactType
    source_speaker: SourceSpeaker
    content: str


@dataclass(frozen=True)
class _SampleBubble:
    speaker: SourceSpeaker
    speaker_label: str
    text: str


# ============================================================================
# 1. Deterministic GatePolicy (Invariant I3 — all 8 pending conditions)
# ============================================================================


def test_gate_policy_happy_path_confirmed_with_5s_undo() -> None:
    now = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
    decision = decide_gate(
        GateInput(
            fact_type=FactType.TABOO,
            confidence=0.92,
            candidate_count=1,
            multi_person=False,
            scoped_locked=True,
            is_venting=False,
            tier=Tier.GOLD,
            person_state=PersonState.ACTIVE,
            source_speaker=SourceSpeaker.NARRATOR,
            polarity_flipped=False,
            extraction_ok=True,
        ),
        GateConfig(confidence_threshold=0.80, undo_window_seconds=5),
        now=now,
    )
    assert decision.is_confirmed
    assert decision.status is FactStatus.CONFIRMED
    assert decision.pending_reason is None
    assert decision.inbox_kind is None
    assert decision.record_review_state is RecordReviewState.NORMAL
    assert decision.undo_window_seconds == 5
    assert decision.undo_deadline == now + timedelta(seconds=5)


def test_gate_policy_condition_1_extraction_failed() -> None:
    dec = decide(GateInput(extraction_ok=False))
    assert dec.is_pending
    assert dec.pending_reason is PendingReason.EXTRACTION_FAILED
    assert dec.inbox_kind is InboxItemKind.FACT_CONFIRM


def test_gate_policy_condition_2_provisional_new_person() -> None:
    dec = decide_gate(GateInput(person_state=PersonState.PROVISIONAL, confidence=0.99))
    assert dec.is_pending
    assert dec.pending_reason is PendingReason.NEW_PERSON
    assert dec.inbox_kind is InboxItemKind.NEW_PERSON_CONFIRM


@pytest.mark.parametrize("candidate_count", [0, 2, 3])
def test_gate_policy_condition_3_ambiguous_person(candidate_count: int) -> None:
    dec = decide_gate(GateInput(candidate_count=candidate_count, confidence=0.99))
    assert dec.is_pending
    assert dec.pending_reason is PendingReason.AMBIGUOUS_PERSON
    assert dec.inbox_kind is InboxItemKind.PERSON_DISAMBIGUATION
    assert dec.record_review_state is RecordReviewState.HELD


def test_gate_policy_condition_4_multi_person_entangled_vs_clean_clauses() -> None:
    # Entangled clause in global mode -> PENDING (MULTI_PERSON)
    entangled = decide_gate(
        GateInput(
            fact_type=FactType.PREFERENCE,
            multi_person=True,
            scoped_locked=False,
            clause_person_unambiguous=False,
            confidence=0.95,
        )
    )
    assert entangled.is_pending
    assert entangled.pending_reason is PendingReason.MULTI_PERSON

    # Scoped-locked current mode -> passes check #4 and confirms
    scoped_ok = decide_gate(
        GateInput(
            fact_type=FactType.PREFERENCE,
            multi_person=True,
            scoped_locked=True,
            confidence=0.95,
        )
    )
    assert scoped_ok.is_confirmed

    # Global distinct clause cleanly attributed to one known person -> passes check #4
    global_clean_clause = decide_gate(
        GateInput(
            fact_type=FactType.TABOO,
            multi_person=True,
            scoped_locked=False,
            clause_person_unambiguous=True,
            confidence=0.95,
        )
    )
    assert global_clean_clause.is_confirmed


@pytest.mark.parametrize("fact_type", [FactType.DATE, FactType.PROMISE])
def test_gate_policy_condition_5_date_or_promise_confirms_as_pencil_glance(
    fact_type: FactType,
) -> None:
    # D1 (roadmap 0.6): dates/promises no longer park in the inbox. They confirm (so the
    # reminder is created as for any confirmed date) but are drawn in pencil for a glance.
    now = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
    dec = decide_gate(
        GateInput(
            fact_type=fact_type,
            confidence=0.99,
            multi_person=True,
            clause_person_unambiguous=True,
        ),
        GateConfig(undo_window_seconds=5),
        now=now,
    )
    assert dec.is_confirmed
    assert dec.pending_reason is None
    assert dec.inbox_kind is None
    assert dec.needs_glance is True
    assert dec.presentation is Presentation.PENCIL
    assert dec.undo_deadline == now + timedelta(seconds=5)
    # with_now must keep the glance flag
    assert dec.with_now(now).needs_glance is True


@pytest.mark.parametrize(
    ("gate_input", "reason"),
    [
        (GateInput(fact_type=FactType.DATE, confidence=0.5), PendingReason.LOW_CONFIDENCE),
        (
            GateInput(fact_type=FactType.PROMISE, source_speaker=SourceSpeaker.UNKNOWN),
            PendingReason.SPEAKER_UNKNOWN,
        ),
        (GateInput(fact_type=FactType.DATE, polarity_flipped=True), PendingReason.POLARITY_FLIP),
        (
            GateInput(fact_type=FactType.DATE, is_venting=True, tier=Tier.GOLD),
            PendingReason.VENTING,
        ),
    ],
)
def test_gate_policy_condition_5_never_overrides_pending_rules(
    gate_input: GateInput, reason: PendingReason
) -> None:
    dec = decide_gate(gate_input)
    assert dec.is_pending
    assert dec.pending_reason is reason
    assert dec.needs_glance is False
    assert dec.presentation is Presentation.PENCIL


def test_gate_policy_presentation_per_rule() -> None:
    assert decide_gate(GateInput()).presentation is Presentation.INK
    assert decide_gate(GateInput()).needs_glance is False
    assert decide_gate(GateInput(extraction_ok=False)).presentation is Presentation.PENCIL
    assert (
        decide_gate(GateInput(person_state=PersonState.PROVISIONAL)).presentation
        is Presentation.QUESTION
    )
    assert decide_gate(GateInput(candidate_count=2)).presentation is Presentation.QUESTION
    assert (
        decide_gate(GateInput(multi_person=True, clause_person_unambiguous=False)).presentation
        is Presentation.QUESTION
    )
    gold_vent = decide_gate(GateInput(is_venting=True, tier=Tier.GOLD))
    assert gold_vent.presentation is Presentation.PENCIL
    assert gold_vent.record_review_state is RecordReviewState.HELD


def test_gate_policy_condition_6_venting_holds_gold_only() -> None:
    gold_vent = decide_gate(
        GateInput(
            fact_type=FactType.EVENT,
            is_venting=True,
            tier=Tier.GOLD,
            confidence=0.96,
        )
    )
    assert gold_vent.is_pending
    assert gold_vent.pending_reason is PendingReason.VENTING
    assert gold_vent.inbox_kind is InboxItemKind.VENTING_HOLD
    assert gold_vent.record_review_state is RecordReviewState.HELD

    # Silver/Crystal venting does not trigger gold love-archive venting_hold
    silver_vent = decide_gate(
        GateInput(
            fact_type=FactType.EVENT,
            is_venting=True,
            tier=Tier.SILVER,
            confidence=0.96,
        )
    )
    assert silver_vent.is_confirmed
    assert silver_vent.record_review_state is RecordReviewState.NORMAL


def test_gate_policy_condition_7_speaker_unknown() -> None:
    dec = decide_gate(
        GateInput(
            fact_type=FactType.PREFERENCE,
            source_speaker=SourceSpeaker.UNKNOWN,
            confidence=0.95,
        )
    )
    assert dec.is_pending
    assert dec.pending_reason is PendingReason.SPEAKER_UNKNOWN


def test_gate_policy_condition_8_polarity_flip_and_low_confidence() -> None:
    flipped = decide_gate(
        GateInput(
            fact_type=FactType.PREFERENCE,
            polarity_flipped=True,
            confidence=0.98,
        )
    )
    assert flipped.is_pending
    assert flipped.pending_reason is PendingReason.POLARITY_FLIP

    low_conf = decide_gate(
        GateInput(
            fact_type=FactType.PREFERENCE,
            confidence=0.79,
        ),
        GateConfig(confidence_threshold=0.80),
    )
    assert low_conf.is_pending
    assert low_conf.pending_reason is PendingReason.LOW_CONFIDENCE


# ============================================================================
# 2. Screenshot Right-Bubble Filter (ADR-005 PRT-04)
# ============================================================================


def test_screenshot_right_bubble_keeps_only_promises_and_strips_self_traits() -> None:
    facts = [
        _SampleFact(FactType.TABOO, SourceSpeaker.USER_SELF, "我严重香菜过敏"),
        _SampleFact(FactType.PREFERENCE, SourceSpeaker.USER_SELF, "我喜欢冰美式"),
        _SampleFact(FactType.WISH, SourceSpeaker.USER_SELF, "我想去北海道"),
        _SampleFact(FactType.PROMISE, SourceSpeaker.USER_SELF, "下周五带你去那家日料"),
        _SampleFact(FactType.TABOO, SourceSpeaker.TARGET_PERSON, "海鲜过敏不能吃虾"),
        _SampleFact(FactType.PREFERENCE, SourceSpeaker.UNKNOWN, "抹茶千层很不错"),
    ]
    partitioned = partition_screenshot_facts(facts)
    assert len(partitioned.stripped) == 3
    assert {f.content for f in partitioned.stripped} == {
        "我严重香菜过敏",
        "我喜欢冰美式",
        "我想去北海道",
    }

    kept = filter_screenshot_facts(facts)
    assert [f.content for f in kept] == [
        "下周五带你去那家日料",
        "海鲜过敏不能吃虾",
        "抹茶千层很不错",
    ]


def test_format_screenshot_bubbles_strips_system_noise() -> None:
    bubbles = [
        _SampleBubble(SourceSpeaker.UNKNOWN, "", "昨天 21:08"),
        _SampleBubble(SourceSpeaker.TARGET_PERSON, "林溪", "最近迷上了桂花拿铁"),
        _SampleBubble(SourceSpeaker.UNKNOWN, "", "林溪 拍了拍 我"),
        _SampleBubble(SourceSpeaker.USER_SELF, "我", "我严重香菜过敏，周末带你去喝桂花拿铁"),
        _SampleBubble(SourceSpeaker.UNKNOWN, "群友阿泽", "这家店周末排队很长"),
    ]
    formatted = format_screenshot_bubbles(bubbles, default_target_label="林溪")
    assert "21:08" not in formatted
    assert "拍了拍" not in formatted
    assert "[LEFT_BUBBLE/林溪]: 最近迷上了桂花拿铁" in formatted
    assert "[RIGHT_BUBBLE/我]: 我严重香菜过敏，周末带你去喝桂花拿铁" in formatted
    assert "[UNKNOWN_BUBBLE/群友阿泽]: 这家店周末排队很长" in formatted


# ============================================================================
# 3. Bi-Temporal Fact Supersession (`memory/domain/bitemporal.py`)
# ============================================================================


def test_bitemporal_polarity_flip_and_history_preservation() -> None:
    user_id = new_id()
    person_id = new_id()
    other_person_id = new_id()
    old_matcha = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="喜欢吃抹茶甜点",
    )
    unrelated_strawberry = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="喜欢吃草莓蛋糕",
    )
    other_person_matcha = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=other_person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="喜欢吃抹茶甜点",
    )

    new_fact_id = new_id()
    candidate = NewFactCandidate(
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.TABOO,
        polarity=Polarity.NEGATIVE,
        content="其实对抹茶严重过敏，不能吃抹茶",
        new_fact_id=new_fact_id,
    )

    existing = [old_matcha, unrelated_strawberry, other_person_matcha]
    assert detect_polarity_flip(existing, candidate) is True

    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
    patches = find_facts_to_supersede(existing, candidate, now=now)
    assert len(patches) == 1
    patch = patches[0]
    assert patch.superseded_fact_id == old_matcha.id
    assert patch.invalid_at == now
    assert patch.time_status is TimeStatus.PAST
    assert patch.superseded_by_id == new_fact_id


def test_bitemporal_explicit_update_and_date_reschedule() -> None:
    user_id = new_id()
    person_id = new_id()
    old_coffee = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="平时爱喝拿铁咖啡",
    )
    old_date = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.DATE,
        polarity=Polarity.NEUTRAL,
        content="结婚纪念日 11月20日",
        trigger_date=date(2026, 11, 20),
    )

    now = datetime(2026, 11, 12, 15, 30, 0, tzinfo=UTC)
    coffee_upd = NewFactCandidate(
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="咖啡改喝手冲黑咖啡了",
        new_fact_id=new_id(),
    )
    patches_coffee = find_facts_to_supersede([old_coffee, old_date], coffee_upd, now=now)
    assert [p.superseded_fact_id for p in patches_coffee] == [old_coffee.id]

    date_upd = NewFactCandidate(
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.DATE,
        polarity=Polarity.NEUTRAL,
        content="结婚纪念日更正为 11月22日",
        trigger_date=date(2026, 11, 22),
        new_fact_id=new_id(),
    )
    patches_date = find_facts_to_supersede([old_coffee, old_date], date_upd, now=now)
    assert [p.superseded_fact_id for p in patches_date] == [old_date.id]


# ============================================================================
# 4. XSS-Safe Transcript Highlighter & Invariant I6 Copy Sanitizer
# ============================================================================


def test_transcript_highlighter_escapes_xss_in_text_and_names() -> None:
    raw = '今天遇到林溪 <script>alert("xss")</script> & 妈妈，她提到了陈明远和陈明。'
    html_out = build_transcript_html(raw, ["林溪", "妈妈", "陈明", "陈明远"])
    assert "<script>" not in html_out
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html_out
    assert "&amp;" in html_out
    assert "<mark>林溪</mark>" in html_out
    assert "<mark>妈妈</mark>" in html_out
    # Longest match first: 陈明远 wrapped whole, not split as <mark>陈明</mark>远
    assert "<mark>陈明远</mark>和<mark>陈明</mark>" in html_out

    # Malicious name itself must also be escaped inside <mark>
    evil_name = '林<img src=x onerror="alert(1)">'
    raw_evil = f"昨天和{evil_name}聊天"
    safe_out = build_transcript_html(raw_evil, [evil_name])
    assert "<img" not in safe_out
    assert "<mark>林&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</mark>" in safe_out


def test_duration_and_natural_slip_formatting_no_jargon() -> None:
    assert format_duration_mmss(None) == "0:00"
    assert format_duration_mmss(4200) == "0:04"
    assert format_duration_mmss(65000) == "1:05"

    cleaned = strip_forbidden_dev_jargon("系统自动归档：海鲜忌口")
    assert contains_forbidden_dev_jargon(cleaned) is None
    tag = build_natural_slip_tag("", "不能吃香菜和折耳根，吃火锅记得点清汤锅底")
    assert tag == "不能吃香菜和折耳根"


# ============================================================================
# 5. Audio Magic-Byte Sniffer & Pillow Image EXIF Sanitizer
# ============================================================================


@pytest.mark.parametrize(
    ("payload", "expected_mime", "expected_ext"),
    [
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81webmdata", "audio/webm", "webm"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00", "audio/wav", "wav"),
        (b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00", "audio/mp4", "m4a"),
        (b"OggS\x00\x02\x00\x00\x00\x00\x00\x00", "audio/ogg", "ogg"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00mp3payload", "audio/mpeg", "mp3"),
        (b"\xff\xfb\x90\x64\x00\x00\x00\x00mp3frame", "audio/mpeg", "mp3"),
    ],
)
def test_audio_magic_sniffer_accepts_valid_containers(
    payload: bytes, expected_mime: str, expected_ext: str
) -> None:
    info = validate_and_sniff_audio(payload, max_bytes=1024)
    assert info.mime_type == expected_mime
    assert info.extension == expected_ext
    assert info.byte_length == len(payload)


def test_audio_magic_sniffer_rejects_invalid_and_oversized() -> None:
    with pytest.raises(InvalidMediaFormatError):
        validate_and_sniff_audio(b"MZ\x90\x00not_an_audio_file", max_bytes=1024)

    with pytest.raises(MediaTooLargeError):
        validate_and_sniff_audio(b"OggS" + b"\x00" * 200, max_bytes=64)


def test_image_sanitizer_strips_exif_and_enforces_limits() -> None:
    img = Image.new("RGB", (48, 48), color=(180, 140, 110))
    exif = Image.Exif()
    exif[0x010F] = "SecretPhoneMaker"  # Make
    exif[0x0110] = "SecretModelPro"  # Model
    exif[0x0131] = "GPS-Leak-Software"  # Software
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    raw_jpeg_with_exif = buf.getvalue()

    # Confirm raw test fixture genuinely carries EXIF tags
    with Image.open(io.BytesIO(raw_jpeg_with_exif)) as pre_check:
        assert len(pre_check.getexif()) >= 2

    sanitized = validate_and_strip_image_exif(raw_jpeg_with_exif, max_bytes=1024 * 1024)
    assert sanitized.mime_type == "image/jpeg"
    assert sanitized.extension == "jpg"
    assert sanitized.width == 48
    assert sanitized.height == 48

    with Image.open(io.BytesIO(sanitized.clean_bytes)) as post_check:
        assert len(post_check.getexif()) == 0

    with pytest.raises(ValidationFailedError):
        validate_and_strip_image_exif(b"not-an-image", max_bytes=1024 * 1024)

    with pytest.raises(PayloadTooLargeError):
        validate_and_strip_image_exif(raw_jpeg_with_exif, max_bytes=10)


# ============================================================================
# 6. Scoped vs Global Routing & Inbox Resolution Policy
# ============================================================================


def test_scoped_and_global_routing_resolution() -> None:
    linxi = KnownPersonProfile(
        person_id=new_id(),
        display_name="林溪",
        aliases=("溪溪",),
        tier=Tier.GOLD,
    )
    mom = KnownPersonProfile(
        person_id=new_id(),
        display_name="妈妈",
        aliases=("老妈",),
        tier=Tier.SILVER,
    )
    chen1 = KnownPersonProfile(person_id=new_id(), display_name="陈明", aliases=("老陈",))
    chen2 = KnownPersonProfile(person_id=new_id(), display_name="陈叔", aliases=("老陈",))
    known = [linxi, mom, chen1, chen2]

    # 1. Scoped mode locks pronoun to activePersonId even if "妈妈做的红烧肉" is a food modifier
    res_scoped = resolve_mention_for_fact(
        mention="她",
        fact_person_id_hint=None,
        fact_content="超爱吃红烧肉",
        full_transcript="她超爱吃红烧肉",
        route_mode=RouteMode.CURRENT,
        scoped_person_id=linxi.person_id,
        known_people=known,
    )
    assert res_scoped.matched_person_id == linxi.person_id
    assert res_scoped.scoped_locked is True
    assert res_scoped.is_cross_person_in_scoped_mode is False

    # 2. Scoped mode explicit other-person clause splits to other person's pending inbox
    res_cross = resolve_mention_for_fact(
        mention="老妈",
        fact_person_id_hint=None,
        fact_content="下周日体检",
        full_transcript="溪溪喜欢抹茶，另外老妈下周日体检",
        route_mode=RouteMode.CURRENT,
        scoped_person_id=linxi.person_id,
        known_people=known,
    )
    assert res_cross.matched_person_id == mom.person_id
    assert res_cross.is_cross_person_in_scoped_mode is True
    assert res_cross.scoped_locked is False

    # 3. Global mode ambiguous alias ("老陈") returns 2 candidates and matched_person_id=None
    res_amb = resolve_mention_for_fact(
        mention="老陈",
        fact_person_id_hint=None,
        fact_content="只喝普洱",
        full_transcript="老陈只喝普洱",
        route_mode=RouteMode.GLOBAL,
        scoped_person_id=None,
        known_people=known,
    )
    assert res_amb.matched_person_id is None
    assert set(res_amb.candidate_person_ids) == {chen1.person_id, chen2.person_id}

    # 4. Global mode unknown human name ("王总") -> provisional new crystal person
    res_new = resolve_mention_for_fact(
        mention="王总",
        fact_person_id_hint=None,
        fact_content="海鲜过敏",
        full_transcript="今天见了王总，王总海鲜过敏",
        route_mode=RouteMode.GLOBAL,
        scoped_person_id=None,
        known_people=known,
    )
    assert res_new.is_new_person is True
    assert res_new.matched_person_id is None


def test_inbox_resolution_policy_validation() -> None:
    p1 = new_id()
    with pytest.raises(InboxDomainError):
        validate_inbox_resolution(
            InboxResolutionInput(
                item_kind=InboxItemKind.FACT_CONFIRM,
                item_status=InboxItemStatus.RESOLVED,
                action=InboxResolutionAction.CONFIRM,
                current_person_id=p1,
            )
        )
    with pytest.raises(InboxDomainError):
        validate_inbox_resolution(
            InboxResolutionInput(
                item_kind=InboxItemKind.PERSON_DISAMBIGUATION,
                item_status=InboxItemStatus.OPEN,
                action=InboxResolutionAction.ASSIGN_PERSON,
                target_person_id=None,
            )
        )
    with pytest.raises(InboxDomainError):
        validate_inbox_resolution(
            InboxResolutionInput(
                item_kind=InboxItemKind.NEW_PERSON_CONFIRM,
                item_status=InboxItemStatus.OPEN,
                action=InboxResolutionAction.MERGE_PERSON,
                current_person_id=p1,
                target_person_id=p1,
            )
        )


@pytest.mark.parametrize(
    "stopword_mention",
    [
        "今天",
        "明天",
        "昨天",
        "周末",
        "周末记",
        "下周",
        "上周",
        "刚才",
        "他说",
        "她说",
        "他严重",
        "她不能",
        "它很怕",
        "有人",
        "大家",
        "自己",
        "我",
    ],
)
def test_routing_non_person_stopwords_never_spawn_provisional_person(
    stopword_mention: str,
) -> None:
    assert is_non_person_stopword_mention(stopword_mention) is True
    known = [
        KnownPersonProfile(person_id=new_id(), display_name="林溪", tier=Tier.GOLD),
        KnownPersonProfile(person_id=new_id(), display_name="妈妈", tier=Tier.SILVER),
    ]
    res = resolve_mention_for_fact(
        mention=stopword_mention,
        fact_person_id_hint=None,
        fact_content="对花生过敏",
        full_transcript=f"{stopword_mention}对花生过敏",
        route_mode=RouteMode.GLOBAL,
        scoped_person_id=None,
        known_people=known,
    )
    assert res.is_new_person is False
    assert res.matched_person_id is None


def test_bitemporal_topic_tokens_exclude_sentiment_carriers() -> None:
    user_id = new_id()
    person_id = new_id()

    # Case 1: "超级喜欢吃草莓" vs "现在不喜欢吃香菜" MUST NOT collide on "喜欢"/"欢吃"/"吃"!
    tokens_strawberry = extract_topic_tokens("超级喜欢吃草莓")
    tokens_cilantro_neg = extract_topic_tokens("现在不喜欢吃香菜")
    assert "草莓" in tokens_strawberry
    assert "香菜" in tokens_cilantro_neg
    assert tokens_strawberry.isdisjoint(tokens_cilantro_neg)

    existing_strawberry = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="超级喜欢吃草莓",
    )
    cand_cilantro_dislike = NewFactCandidate(
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.NEGATIVE,
        content="现在不喜欢吃香菜",
        new_fact_id=new_id(),
    )
    assert detect_polarity_flip([existing_strawberry], cand_cilantro_dislike) is False
    assert (
        find_facts_to_supersede(
            [existing_strawberry],
            cand_cilantro_dislike,
            now=datetime(2026, 11, 12, 16, 0, 0, tzinfo=UTC),
        )
        == []
    )

    # Case 2: "喜欢吃香菜" vs "对香菜严重过敏不能吃香菜" DO share "香菜" and trigger polarity flip!
    tokens_cilantro_pos = extract_topic_tokens("喜欢吃香菜")
    tokens_cilantro_allergy = extract_topic_tokens("对香菜严重过敏不能吃香菜")
    assert "香菜" in tokens_cilantro_pos
    assert "香菜" in tokens_cilantro_allergy
    assert tokens_cilantro_pos & tokens_cilantro_allergy == {"香菜"}

    existing_cilantro = ExistingFactSnapshot(
        id=new_id(),
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.PREFERENCE,
        polarity=Polarity.POSITIVE,
        content="喜欢吃香菜",
    )
    cand_cilantro_allergy = NewFactCandidate(
        user_id=user_id,
        person_id=person_id,
        fact_type=FactType.TABOO,
        polarity=Polarity.NEGATIVE,
        content="对香菜严重过敏不能吃香菜",
        new_fact_id=new_id(),
    )
    assert detect_polarity_flip([existing_cilantro], cand_cilantro_allergy) is True
    patches = find_facts_to_supersede(
        [existing_cilantro],
        cand_cilantro_allergy,
        now=datetime(2026, 11, 12, 16, 0, 0, tzinfo=UTC),
    )
    assert [p.superseded_fact_id for p in patches] == [existing_cilantro.id]


def test_fact_create_request_lunar_validation_and_material_dto_optional_person_id() -> None:
    from pydantic import ValidationError as PydanticValidationError

    from mindweave.modules.memory.application.dto import FactCreateRequest, MaterialDto
    from mindweave.shared.enums import RecordKind

    with pytest.raises(PydanticValidationError):
        FactCreateRequest(
            type=FactType.DATE,
            content="农历生日缺月日",
            is_lunar=True,
        )

    with pytest.raises(PydanticValidationError):
        FactCreateRequest(
            type=FactType.DATE,
            content="非农历闰月冲突",
            is_lunar=False,
            lunar_is_leap_month=True,
        )

    with pytest.raises(PydanticValidationError):
        FactCreateRequest(
            type=FactType.DATE,
            content="阳历却携带农历月日",
            is_lunar=False,
            lunar_month=8,
            lunar_day=15,
        )

    valid_lunar = FactCreateRequest(
        type=FactType.DATE,
        content="农历八月十五",
        is_lunar=True,
        lunar_month=8,
        lunar_day=15,
    )
    assert valid_lunar.lunar_month == 8
    assert valid_lunar.lunar_day == 15

    now = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
    dto = MaterialDto(
        id=new_id(),
        person_id=None,
        kind=RecordKind.NOTE,
        type=RecordKind.NOTE,
        title="未归档随手记",
        body="买一束向日葵",
        snippet="买一束向日葵",
        created_at=now,
        updated_at=now,
    )
    dumped = dto.model_dump(mode="json", by_alias=True)
    assert dumped["personId"] is None
