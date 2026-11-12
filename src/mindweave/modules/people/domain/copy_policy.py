"""Invariant I6 UI copy sanitizer, monogram derivation, and Cupid title policy.

Pure Python domain module — zero framework, database, or vendor imports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mindweave.modules.people.domain.entities import CopyPolicyViolationError

# Must stay aligned with Invariant I6 (AGENTS.md & V10 Spec §一.4)
FORBIDDEN_UI_DEV_PHRASES: tuple[str, ...] = (
    "系统自动归档",
    "AI 提炼卡片",
    "AI提炼卡片",
    "温情伴读模式",
    "温情伴读",
    "方案 A",
    "方案 B",
    "方案A",
    "方案B",
    "移出胶囊归入名录",
    "点击切换",
    "无需照片",
    "[rec_",
    "【rec_",
)

MAX_NAME_CN_LEN = 80
MAX_MONOGRAM_LEN = 8
MAX_ROLE_TAG_LEN = 64
MAX_CONTEXT_SUB_LEN = 120
MAX_SHORT_MEMO_LEN = 240
MAX_TRAIT_LEN = 200
MAX_TRAITS_COUNT = 50
MAX_ALIAS_LEN = 80


def find_forbidden_phrase(text: str) -> str | None:
    """Return the first forbidden developer/PM phrase found in `text`, or None."""
    for phrase in FORBIDDEN_UI_DEV_PHRASES:
        if phrase in text:
            return phrase
    return None


def assert_no_forbidden_ui_phrases(text: str, *, field_name: str) -> None:
    """Raise `CopyPolicyViolationError` if `text` contains banned developer jargon."""
    hit = find_forbidden_phrase(text)
    if hit is not None:
        raise CopyPolicyViolationError(
            f"Field '{field_name}' contains forbidden developer/UI phrase.",
            field_name=field_name,
            forbidden_phrase=hit,
        )


def validate_name_cn(name_cn: str) -> str:
    cleaned = name_cn.strip()
    if not cleaned:
        raise CopyPolicyViolationError("nameCn must not be empty.", field_name="nameCn")
    if len(cleaned) > MAX_NAME_CN_LEN:
        raise CopyPolicyViolationError(
            f"nameCn must be at most {MAX_NAME_CN_LEN} characters.",
            field_name="nameCn",
        )
    assert_no_forbidden_ui_phrases(cleaned, field_name="nameCn")
    return cleaned


def validate_optional_copy_field(
    value: str,
    *,
    field_name: str,
    max_length: int,
) -> str:
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise CopyPolicyViolationError(
            f"Field '{field_name}' must be at most {max_length} characters.",
            field_name=field_name,
        )
    if cleaned:
        assert_no_forbidden_ui_phrases(cleaned, field_name=field_name)
    return cleaned


def validate_traits(traits: Sequence[str]) -> list[str]:
    if len(traits) > MAX_TRAITS_COUNT:
        raise CopyPolicyViolationError(
            f"traits may contain at most {MAX_TRAITS_COUNT} items.",
            field_name="traits",
        )
    normalized: list[str] = []
    for idx, raw in enumerate(traits):
        item = raw.strip()
        if not item:
            continue
        if len(item) > MAX_TRAIT_LEN:
            raise CopyPolicyViolationError(
                f"traits[{idx}] must be at most {MAX_TRAIT_LEN} characters.",
                field_name=f"traits[{idx}]",
            )
        assert_no_forbidden_ui_phrases(item, field_name=f"traits[{idx}]")
        normalized.append(item)
    return normalized


def validate_alias_text(alias: str) -> str:
    cleaned = alias.strip()
    if not cleaned:
        raise CopyPolicyViolationError("alias must not be empty.", field_name="alias")
    if len(cleaned) > MAX_ALIAS_LEN:
        raise CopyPolicyViolationError(
            f"alias must be at most {MAX_ALIAS_LEN} characters.",
            field_name="alias",
        )
    assert_no_forbidden_ui_phrases(cleaned, field_name="alias")
    return cleaned


def validate_visual_tokens(tokens: Mapping[str, Any]) -> dict[str, Any]:
    if len(tokens) > 32:
        raise CopyPolicyViolationError(
            "visualTokens may contain at most 32 keys.",
            field_name="visualTokens",
        )
    cleaned: dict[str, Any] = {}
    for k, v in tokens.items():
        key = str(k).strip()
        if not key or len(key) > 64:
            raise CopyPolicyViolationError(
                "visualTokens keys must be 1..64 non-empty characters.",
                field_name="visualTokens",
            )
        if isinstance(v, str):
            if len(v) > 256:
                raise CopyPolicyViolationError(
                    f"visualTokens['{key}'] exceeds 256 characters.",
                    field_name="visualTokens",
                )
            assert_no_forbidden_ui_phrases(v, field_name=f"visualTokens.{key}")
            cleaned[key] = v
        elif isinstance(v, int | float | bool) or v is None:
            cleaned[key] = v
        else:
            raise CopyPolicyViolationError(
                f"visualTokens['{key}'] must be a scalar value.",
                field_name="visualTokens",
            )
    return cleaned


def _is_cjk_ideograph(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2A6DF
    )


def derive_monogram(name_cn: str, explicit_monogram: str | None = None) -> str:
    """Derive single-character seal monogram when omitted.

    - Uses `explicit_monogram` if non-empty (validated against length and Invariant I6).
    - Otherwise returns the first Chinese character or uppercase first ASCII letter of `name_cn`.
    """
    if explicit_monogram is not None and explicit_monogram.strip():
        mono = explicit_monogram.strip()
        if len(mono) > MAX_MONOGRAM_LEN:
            raise CopyPolicyViolationError(
                f"monogram must be at most {MAX_MONOGRAM_LEN} characters.",
                field_name="monogram",
            )
        assert_no_forbidden_ui_phrases(mono, field_name="monogram")
        return mono

    cleaned = validate_name_cn(name_cn)
    for ch in cleaned:
        if _is_cjk_ideograph(ch):
            return ch
        if ch.isascii() and ch.isalpha():
            return ch.upper()
    for ch in cleaned:
        if ch.isalnum():
            return ch.upper()
    return cleaned[0]


def derive_cupid_title(name_cn: str) -> str:
    """Derive the canonical V10 Muse companion title `<name_cn>的丘比特`."""
    cleaned = name_cn.strip()
    return f"{cleaned}的丘比特"
