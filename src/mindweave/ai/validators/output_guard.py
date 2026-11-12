"""Post-generation guardrails enforcing Tier policies, anti-dependency, and Invariant I6."""

from __future__ import annotations

import re

from mindweave.ai.schemas.contracts import CompanionChatOutput
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES, strip_forbidden_dev_jargon
from mindweave.shared.enums import Tier

# SOP §3.1: Virtual-lover / social-isolation phrases strictly banned
_FORBIDDEN_DEPENDENCY_PHRASES: tuple[str, ...] = (
    "只有我最爱你",
    "别理现实里的人了",
    "只有我懂你",
)

# ADR-005 #3: Excusing / neutral preaching phrases strictly banned in SILVER (hate) tier
_SILVER_EXCUSING_RE = re.compile(
    r"[^。！？!\?\n]*(?:也许.{0,8}也有压力|换个角度想.{0,8}也有难处|你可以试着体谅|一个巴掌拍不响|去见见(?:他|她)吧)[^。！？!\?\n]*[。！？!\?]?"
)


_EXTRA_TECH_JARGON_RE = re.compile(
    r"(?:JSON\s*Schema|system_prompt|PostgreSQL|\bRLS\b|\bUUID\b|\bSQL\b)",
    re.IGNORECASE,
)


def _clean_text_field(text: str, *, allow_internal_rec_tags: bool) -> str:
    cleaned = text
    for phrase in FORBIDDEN_UI_DEV_PHRASES:
        if allow_internal_rec_tags and phrase in ("[rec_", "【rec_"):
            continue
        cleaned = cleaned.replace(phrase, "")
    for dep in _FORBIDDEN_DEPENDENCY_PHRASES:
        cleaned = cleaned.replace(dep, "")
    cleaned = _EXTRA_TECH_JARGON_RE.sub("", cleaned)
    return cleaned.strip()


def enforce_companion_output_guard(
    output: CompanionChatOutput,
    *,
    tier: Tier,
) -> CompanionChatOutput:
    """Enforce tier output invariants (`silver`/`crystal` => `perspectives=[]`) and I6 copy hygiene."""
    reply = _clean_text_field(output.reply_text, allow_internal_rec_tags=True)
    if tier is Tier.SILVER:
        stripped_reply = _SILVER_EXCUSING_RE.sub("", reply).strip()
        if stripped_reply:
            reply = stripped_reply

    perspectives: list[str] = []
    if tier.allows_perspectives:
        for item in output.alternative_perspectives:
            sanitized = strip_forbidden_dev_jargon(
                _clean_text_field(item, allow_internal_rec_tags=False)
            )
            if sanitized:
                perspectives.append(sanitized)

    suggestions: list[str] = []
    for item in output.actionable_suggestions:
        sanitized = strip_forbidden_dev_jargon(
            _clean_text_field(item, allow_internal_rec_tags=False)
        )
        if tier is Tier.SILVER:
            sanitized = _SILVER_EXCUSING_RE.sub("", sanitized).strip()
        if sanitized:
            suggestions.append(sanitized)

    whisper = (
        strip_forbidden_dev_jargon(
            _clean_text_field(output.whisper_note, allow_internal_rec_tags=False)
        )
        if output.whisper_note
        else None
    )
    disclaimer = (
        strip_forbidden_dev_jargon(
            _clean_text_field(output.disclaimer, allow_internal_rec_tags=False)
        )
        if output.disclaimer
        else None
    )

    return output.model_copy(
        update={
            "reply_text": reply,
            "whisper_note": whisper or None,
            "alternative_perspectives": perspectives,
            "actionable_suggestions": suggestions,
            "disclaimer": disclaimer or None,
        }
    )
