"""Pure domain formatting and copy-sanitization helpers for Workspace Bootstrap.

Zero framework, database, HTTP, or vendor SDK imports (enforced by `.importlinter`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from mindweave.modules.people.domain.copy_policy import FORBIDDEN_UI_DEV_PHRASES
from mindweave.shared.enums import RecordKind


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
