"""Pure domain formatting and copy-sanitization helpers for Workspace Bootstrap.

Zero framework, database, HTTP, or vendor SDK imports (enforced by `.importlinter`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from mindweave.modules.people.domain.copy_policy import FORBIDDEN_UI_DEV_PHRASES
from mindweave.shared.enums import RecordKind, Tier


@dataclass(frozen=True, slots=True)
class MaterialPreview:
    """Decrypted, UI-ready scrapbook material preview aligned with V10 `PEOPLE_DB.materials`."""

    id: UUID
    kind: RecordKind
    title: str
    body: str
    snippet: str
    dur: str | None
    stamp_code: str | None
    happened_on: date | None
    media_object_key: str | None


@dataclass(frozen=True, slots=True)
class VisualTokenColors:
    """Resolved top-level V10 `gleb-app.mjs` seal/tape/tab color tokens."""

    seal_bg: str
    seal_color: str
    tape_color: str
    tab_color: str


_TIER_FALLBACK_COLORS: dict[Tier, VisualTokenColors] = {
    Tier.GOLD: VisualTokenColors(
        seal_bg="#F3E5AB",
        seal_color="#7A5C1E",
        tape_color="#E8D8B5",
        tab_color="#D4AF37",
    ),
    Tier.SILVER: VisualTokenColors(
        seal_bg="#E5E9F0",
        seal_color="#4C566A",
        tape_color="#D8DEE9",
        tab_color="#8FBCBB",
    ),
    Tier.CRYSTAL: VisualTokenColors(
        seal_bg="#EAE6DF",
        seal_color="#5C5852",
        tape_color="#DDD7CE",
        tab_color="#B8B2A6",
    ),
}


def _pick_token(tokens: Mapping[str, Any], camel_key: str, snake_key: str, fallback: str) -> str:
    for key in (camel_key, snake_key):
        raw = tokens.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return fallback


def resolve_visual_token_colors(
    tier: Tier, visual_tokens: Mapping[str, Any] | None
) -> VisualTokenColors:
    """Extract V10 top-level `sealBg`, `sealColor`, `tapeColor`, `tabColor` with tier fallbacks."""
    tokens = visual_tokens or {}
    defaults = _TIER_FALLBACK_COLORS[tier]
    return VisualTokenColors(
        seal_bg=_pick_token(tokens, "sealBg", "seal_bg", defaults.seal_bg),
        seal_color=_pick_token(tokens, "sealColor", "seal_color", defaults.seal_color),
        tape_color=_pick_token(tokens, "tapeColor", "tape_color", defaults.tape_color),
        tab_color=_pick_token(tokens, "tabColor", "tab_color", defaults.tab_color),
    )


def format_duration_ms(duration_ms: int | None) -> str | None:
    """Format `audio_duration_ms` into V10 `M:SS` string (e.g. `18000` -> `'0:18'`)."""
    if duration_ms is None:
        return None
    if duration_ms <= 0:
        return "0:00"
    total_seconds = max(1, round(duration_ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def sanitize_material_preview_text(text: str) -> str:
    """Strip any forbidden developer/PM jargon from material preview display fields (Invariant I6)."""
    cleaned = text
    for phrase in FORBIDDEN_UI_DEV_PHRASES:
        if phrase in ("[rec_", "【rec_"):
            continue
        cleaned = cleaned.replace(phrase, "").strip(" ·-—:：")
    return cleaned.strip()
