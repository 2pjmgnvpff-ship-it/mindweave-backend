"""Bi-temporal fact supersession and polarity-flip detection (pure domain, zero IO).

Enforces history preservation: when a newly confirmed fact contradicts or updates
an existing active `confirmed` fact on the same `(user_id, person_id, fact_type)`
(or across `preference` <-> `taboo` polarity reversal), the old fact is NEVER
overwritten in place. Instead it receives:
- `invalid_at = now`
- `time_status = TimeStatus.PAST` ("past")
- `superseded_by_id = new_fact_id`
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from mindweave.shared.enums import FactStatus, FactType, Polarity, TimeStatus

_SENTIMENT_AND_FILLER_PHRASES: tuple[str, ...] = (
    "千万别让她",
    "千万别让他",
    "千万别",
    "特别喜欢",
    "非常喜欢",
    "超级喜欢",
    "超爱吃",
    "超喜欢",
    "最喜欢",
    "不喜欢",
    "不爱吃",
    "不爱喝",
    "不能吃",
    "不能喝",
    "忌口是",
    "更正为",
    "更新为",
    "改成了",
    "换成了",
    "戒掉了",
    "喜欢吃",
    "喜欢喝",
    "偏爱喝",
    "偏爱吃",
    "讨厌吃",
    "讨厌喝",
    "最讨厌",
    "最恨吃",
    "喜欢",
    "偏爱",
    "超爱",
    "最爱",
    "爱吃",
    "爱喝",
    "想吃",
    "想喝",
    "想要",
    "讨厌",
    "最恨",
    "厌恶",
    "反感",
    "害怕",
    "忌口",
    "严禁",
    "禁止",
    "过敏",
    "严重",
    "现在",
    "目前",
    "最近",
    "以前",
    "曾经",
    "原来",
    "其实",
    "已经",
    "不再",
    "戒掉",
    "戒了",
    "改喝",
    "改吃",
    "改成",
    "换成",
    "更正",
)

_SINGLE_CHAR_PARTICLES = frozenset("的了呀呢吧嘛哦啊哈很太最都也还又在把被给对和与及或且不忌无非")

_EXPLICIT_UPDATE_MARKERS: tuple[str, ...] = (
    "不再",
    "戒掉",
    "戒了",
    "改喝",
    "改吃",
    "改成",
    "换成",
    "更正",
    "更新",
    "变了",
    "不喝",
    "不吃",
    "不用",
    "取消",
    "推迟",
    "改期",
    "改到",
)

_WORD_OR_CJK_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExistingFactSnapshot:
    """Immutable view of an existing fact for pure bi-temporal evaluation."""

    id: UUID
    user_id: UUID
    person_id: UUID
    fact_type: FactType
    polarity: Polarity
    content: str
    status: FactStatus = FactStatus.CONFIRMED
    invalid_at: datetime | None = None
    trigger_date: date | None = None

    @property
    def is_active_confirmed(self) -> bool:
        return self.status is FactStatus.CONFIRMED and self.invalid_at is None


@dataclass(frozen=True, slots=True)
class NewFactCandidate:
    """Incoming fact being evaluated for polarity flip or bi-temporal supersession."""

    user_id: UUID
    person_id: UUID
    fact_type: FactType
    polarity: Polarity
    content: str
    new_fact_id: UUID | None = None
    trigger_date: date | None = None
    explicit_supersedes_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class FactSupersessionPatch:
    """Deterministic mutation instructions for an invalidated historical fact."""

    superseded_fact_id: UUID
    invalid_at: datetime
    time_status: TimeStatus
    superseded_by_id: UUID


def extract_topic_tokens(text: str) -> set[str]:
    """Extract normalized CJK 2-grams (with 1-char fallback) and Latin word tokens."""
    cleaned = text.strip().lower()
    if not cleaned:
        return set()

    for phrase in _SENTIMENT_AND_FILLER_PHRASES:
        cleaned = cleaned.replace(phrase, " ")

    tokens: set[str] = set()
    for chunk in _WORD_OR_CJK_RE.findall(cleaned):
        if chunk.isascii():
            if len(chunk) >= 2:
                tokens.add(chunk)
            continue
        # Strip leading/trailing filler particles from CJK run
        core = "".join(ch for ch in chunk if ch not in _SINGLE_CHAR_PARTICLES)
        if not core:
            continue
        if len(core) == 1:
            tokens.add(core)
        else:
            for idx in range(len(core) - 1):
                tokens.add(core[idx : idx + 2])
    return tokens


def has_explicit_update_marker(text: str) -> bool:
    return any(marker in text for marker in _EXPLICIT_UPDATE_MARKERS)


def _types_can_contradict(a: FactType, b: FactType) -> bool:
    if a == b:
        return True
    return {a, b} == {FactType.PREFERENCE, FactType.TABOO}


def _effective_polarity(fact_type: FactType, polarity: Polarity, content: str) -> Polarity:
    if fact_type is FactType.TABOO:
        return Polarity.NEGATIVE
    if polarity is not Polarity.NEUTRAL:
        return polarity
    neg_cues = ("不吃", "不喝", "不喜欢", "讨厌", "过敏", "忌", "严禁", "最恨", "不再", "戒")
    if any(cue in content for cue in neg_cues):
        return Polarity.NEGATIVE
    pos_cues = ("喜欢", "偏爱", "超爱", "最爱", "爱吃", "爱喝", "想要", "想吃")
    if any(cue in content for cue in pos_cues):
        return Polarity.POSITIVE
    return Polarity.NEUTRAL


def is_polar_opposite(
    existing: ExistingFactSnapshot,
    candidate: NewFactCandidate,
) -> bool:
    """Return True when candidate reverses sentiment/taboo polarity on the same topic."""
    if existing.user_id != candidate.user_id or existing.person_id != candidate.person_id:
        return False
    if not existing.is_active_confirmed:
        return False
    if not _types_can_contradict(existing.fact_type, candidate.fact_type):
        return False

    tokens_old = extract_topic_tokens(existing.content)
    tokens_new = extract_topic_tokens(candidate.content)
    if not tokens_old or not tokens_new or tokens_old.isdisjoint(tokens_new):
        return False

    pol_old = _effective_polarity(existing.fact_type, existing.polarity, existing.content)
    pol_new = _effective_polarity(candidate.fact_type, candidate.polarity, candidate.content)

    if {existing.fact_type, candidate.fact_type} == {FactType.PREFERENCE, FactType.TABOO}:
        return True
    return (pol_old is Polarity.POSITIVE and pol_new is Polarity.NEGATIVE) or (
        pol_old is Polarity.NEGATIVE and pol_new is Polarity.POSITIVE
    )


def detect_polarity_flip(
    existing_facts: Sequence[ExistingFactSnapshot],
    candidate: NewFactCandidate,
) -> bool:
    """Check whether `candidate` flips polarity against any active confirmed fact."""
    return any(is_polar_opposite(existing, candidate) for existing in existing_facts)


def find_facts_to_supersede(
    existing_facts: Sequence[ExistingFactSnapshot],
    candidate: NewFactCandidate,
    *,
    now: datetime,
) -> list[FactSupersessionPatch]:
    """Identify active confirmed facts superseded by `candidate` and build patches."""
    if candidate.new_fact_id is None:
        raise ValueError("NewFactCandidate.new_fact_id is required to build supersession patches")

    tokens_new = extract_topic_tokens(candidate.content)
    explicit_update = has_explicit_update_marker(candidate.content)
    patches: list[FactSupersessionPatch] = []

    for existing in existing_facts:
        if existing.id == candidate.new_fact_id:
            continue
        if existing.user_id != candidate.user_id or existing.person_id != candidate.person_id:
            continue
        if not existing.is_active_confirmed:
            continue

        should_supersede = False

        if candidate.explicit_supersedes_id is not None and (
            existing.id == candidate.explicit_supersedes_id
        ):
            should_supersede = True
        elif is_polar_opposite(existing, candidate):
            should_supersede = True
        elif existing.fact_type == candidate.fact_type:
            tokens_old = extract_topic_tokens(existing.content)
            has_topic_overlap = bool(tokens_old and tokens_new and (tokens_old & tokens_new))
            if has_topic_overlap:
                if explicit_update:
                    should_supersede = True
                elif (
                    existing.fact_type is FactType.DATE
                    and existing.content.strip() != candidate.content.strip()
                ):
                    should_supersede = True
                elif (
                    existing.fact_type is FactType.DATE
                    and existing.trigger_date != candidate.trigger_date
                    and candidate.trigger_date is not None
                ):
                    should_supersede = True

        if should_supersede:
            patches.append(
                FactSupersessionPatch(
                    superseded_fact_id=existing.id,
                    invalid_at=now,
                    time_status=TimeStatus.PAST,
                    superseded_by_id=candidate.new_fact_id,
                )
            )

    return patches
