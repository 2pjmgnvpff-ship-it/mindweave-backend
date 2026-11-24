"""Privacy-desensitized notification copy builder (PRD §7.6 & Crisis/Anti-Addiction SOP §3.2).

Strict guarantees:
1. Push notifications NEVER accept or emit person names, aliases, or secret fact contents.
2. Anthropomorphic / emotionally needy solicitation copy ("想你了", "快来陪我聊", etc.) is banned.
3. Developer/internal jargon ("[rec_", "系统自动归档", etc.) is banned (Invariant I6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from mindweave.shared.enums import ReminderRule

# Crisis & Anti-Addiction SOP §3.1/§3.2 & not_to_do.md §1.3 strictly forbid needy/parasocial AI pushes
FORBIDDEN_ANTHROPOMORPHIC_PUSH_PHRASES: tuple[str, ...] = (
    "想你了",
    "快来陪我聊",
    "陪我聊聊天",
    "好久没理我",
    "只有我最懂你",
    "只有我最爱你",
    "我会永远陪着你",
    "别理现实里的人",
    "陪陪我",
    "在吗",
)

# Domain-pure mirror of Invariant I6 banned developer phrases
_FORBIDDEN_DEV_MARKERS: tuple[str, ...] = (
    "系统自动归档",
    "AI 提炼卡片",
    "AI提炼卡片",
    "温情伴读",
    "[rec_",
    "【rec_",
)


class PushCopySafetyError(ValueError):
    """Raised when push notification copy violates desensitization or anti-addiction rules."""


@dataclass(frozen=True, slots=True)
class DesensitizedPushCopy:
    """Privacy-safe push payload carrying zero person names or private fact text."""

    title: str
    body: str
    deeplink_path: str = "/reminders"
    category: str = "REMINDER_DUE"

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "body": self.body,
            "deeplinkPath": self.deeplink_path,
            "category": self.category,
        }

    def __getitem__(self, key: str) -> str:
        mapping = self.to_dict()
        if key in mapping:
            return mapping[key]
        if key == "deeplink_path":
            return self.deeplink_path
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in ("title", "body", "deeplinkPath", "deeplink_path", "category")


def validate_push_copy_safety(
    title: str,
    body: str,
    *,
    forbidden_substrings: Sequence[str] = (),
) -> None:
    """Verify notification copy contains zero parasocial hooks, dev jargon, or caller-specified PII."""
    if not title.strip() or not body.strip():
        raise PushCopySafetyError("Notification title and body must be non-empty")

    combined = f"{title}\n{body}"
    for phrase in FORBIDDEN_ANTHROPOMORPHIC_PUSH_PHRASES:
        if phrase in combined:
            raise PushCopySafetyError(
                f"Anthropomorphic/needy push phrase {phrase!r} is strictly forbidden (SOP §3.2)"
            )
    for marker in _FORBIDDEN_DEV_MARKERS:
        if marker in combined:
            raise PushCopySafetyError(
                f"Developer jargon {marker!r} is forbidden in user-facing copy (Invariant I6)"
            )
    for secret in forbidden_substrings:
        cleaned = secret.strip()
        if cleaned and cleaned in combined:
            raise PushCopySafetyError(
                "Privacy violation: sensitive person name or fact content leaked into push copy"
            )


def build_desensitized_push_copy(
    *,
    lead_day: int,
    rule: ReminderRule | str = ReminderRule.YEARLY,
    reminder_id: UUID | str | None = None,
) -> DesensitizedPushCopy:
    """Build privacy-desensitized notification `{title, body}` strictly from non-PII schedule metadata."""
    if isinstance(lead_day, bool) or lead_day < 0:
        raise PushCopySafetyError(f"lead_day must be a non-negative integer, got {lead_day!r}")

    # Validate rule enum even though copy stays privacy-neutral
    try:
        rule_enum = ReminderRule(rule)
    except ValueError as exc:
        raise PushCopySafetyError(f"Invalid reminder rule {rule!r}") from exc

    if lead_day == 0:
        title = "心络 · 今日有一条重要提醒"
        body = (
            "点开查看你记下的重要约定与待办承诺"
            if rule_enum is ReminderRule.ONCE
            else "点开查看你记下的重要日子与准备清单"
        )
    else:
        title = f"心络 · {lead_day} 天后有一个重要约定"
        body = "点开查看你记下的重要日子与准备清单"

    deeplink = f"/reminders/{reminder_id}" if reminder_id is not None else "/reminders"
    validate_push_copy_safety(title, body)
    return DesensitizedPushCopy(
        title=title,
        body=body,
        deeplink_path=deeplink,
    )
