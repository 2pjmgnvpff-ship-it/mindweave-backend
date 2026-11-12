"""Base camelCase DTOs, cursor pagination, and Invariant I6 UI copy sanitizer."""

from __future__ import annotations

import base64
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from mindweave.core.errors import ValidationFailedError

# Invariant I6 (AGENTS.md & V10 Spec §1.4): Developer/PM jargon strictly banned in display fields!
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


def _assert_no_null_bytes(value: Any) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("Null bytes (\\u0000) are not permitted in text fields")
    elif isinstance(value, dict):
        for k, v in value.items():
            _assert_no_null_bytes(k)
            _assert_no_null_bytes(v)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _assert_no_null_bytes(item)


class CamelModel(BaseModel):
    """Canonical API DTO base model enforcing camelCase JSON wire format."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_null_bytes_in_payload(cls, data: Any) -> Any:
        _assert_no_null_bytes(data)
        return data


class CursorPage[T](CamelModel):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(payload: dict[str, str | int]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, str | int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Cursor must decode to object")
        return data
    except Exception as exc:
        raise ValidationFailedError("Invalid pagination cursor") from exc


def contains_forbidden_dev_jargon(text: str) -> str | None:
    for phrase in FORBIDDEN_UI_DEV_PHRASES:
        if phrase in text:
            return phrase
    return None


def strip_forbidden_dev_jargon(text: str) -> str:
    cleaned = text
    for phrase in FORBIDDEN_UI_DEV_PHRASES:
        if phrase in ("[rec_", "【rec_"):
            continue
        cleaned = cleaned.replace(phrase, "").strip(" ·-—:：")
    return cleaned.strip()
