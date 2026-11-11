"""Deterministic Screenshot Left/Right Bubble Rules (ADR-005 PRT-04, pure domain).

Prevents subject-object inversion in chat screenshots:
- Left bubbles (`SourceSpeaker.TARGET_PERSON`) represent the target person's own words.
- Right bubbles (`SourceSpeaker.USER_SELF`) represent the user's own words and ONLY
  allow `FactType.PROMISE`! Any `preference`, `taboo`, `wish`, `date`, `event`, or `note`
  from `USER_SELF` (e.g. user saying "我严重香菜过敏") MUST be deterministically stripped
  so the user's own traits are NEVER misattributed to the target person.
- Unknown bubbles (`SourceSpeaker.UNKNOWN`, e.g. group chat without clear attribution)
  are preserved with `SourceSpeaker.UNKNOWN` so `GatePolicy` routes them to `pending`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from mindweave.shared.enums import FactType, SourceSpeaker

_SYSTEM_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{1,2}:\d{2}$"),
    re.compile(r"^(?:昨天|前天|星期[一二三四五六日天])\s*\d{1,2}:\d{2}$"),
    re.compile(r"撤回了一条消息$"),
    re.compile(r"拍了拍"),
    re.compile(r"^以上是打招呼的内容$"),
)


class SpeakerFactLike(Protocol):
    @property
    def fact_type(self) -> FactType: ...

    @property
    def source_speaker(self) -> SourceSpeaker: ...

    @property
    def content(self) -> str: ...


class BubbleLike(Protocol):
    @property
    def speaker(self) -> SourceSpeaker: ...

    @property
    def speaker_label(self) -> str: ...

    @property
    def text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ScreenshotFilterResult[T]:
    kept: list[T]
    stripped: list[T]


def is_system_noise_bubble(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return True
    return any(pattern.search(cleaned) is not None for pattern in _SYSTEM_NOISE_PATTERNS)


def is_allowed_for_speaker(source_speaker: SourceSpeaker, fact_type: FactType) -> bool:
    """Right bubble (`USER_SELF`) ONLY keeps `FactType.PROMISE`."""
    if source_speaker is SourceSpeaker.USER_SELF:
        return fact_type is FactType.PROMISE
    return True


def partition_screenshot_facts[F: SpeakerFactLike](facts: Sequence[F]) -> ScreenshotFilterResult[F]:
    """Deterministically partition extracted facts into `(kept, stripped)`."""
    kept: list[F] = []
    stripped: list[F] = []
    for item in facts:
        if is_allowed_for_speaker(item.source_speaker, item.fact_type):
            kept.append(item)
        else:
            stripped.append(item)
    return ScreenshotFilterResult(kept=kept, stripped=stripped)


def filter_screenshot_facts[F: SpeakerFactLike](facts: Sequence[F]) -> list[F]:
    """Return only facts permitted by the right-bubble rule."""
    return partition_screenshot_facts(facts).kept


def format_screenshot_bubbles(
    bubbles: Sequence[BubbleLike],
    *,
    default_target_label: str = "对方",
) -> str:
    """Format parsed screenshot bubbles with explicit `[LEFT_BUBBLE/...]` / `[RIGHT_BUBBLE/我]` tags."""
    lines: list[str] = []
    for bubble in bubbles:
        text = bubble.text.strip()
        if is_system_noise_bubble(text):
            continue
        if bubble.speaker is SourceSpeaker.USER_SELF:
            lines.append(f"[RIGHT_BUBBLE/我]: {text}")
        elif bubble.speaker is SourceSpeaker.TARGET_PERSON:
            label = bubble.speaker_label.strip() or default_target_label
            lines.append(f"[LEFT_BUBBLE/{label}]: {text}")
        else:
            label = bubble.speaker_label.strip() or "群成员"
            lines.append(f"[UNKNOWN_BUBBLE/{label}]: {text}")
    return "\n".join(lines)
