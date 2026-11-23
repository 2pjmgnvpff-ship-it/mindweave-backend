"""CamelCase API schemas for Proactive Relationship Care Brief endpoints."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from mindweave.core.dto import CamelModel
from mindweave.shared.enums import Tier


class BriefItemSchema(CamelModel):
    """Single actionable highlight surfaced in a person's proactive care brief."""

    kind: Literal["reminder", "pencil", "fact"]
    when: str | None = None
    text: str
    record_id: UUID | None = None
    action_prompt: str


class PersonBriefResponse(CamelModel):
    """Calm, zero-guilt, tier-aware proactive relationship brief for a single person."""

    person_id: UUID
    tier: Tier
    upcoming_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    headline: str
    subline: str
    prompt: str
    items: list[BriefItemSchema] = Field(default_factory=list, max_length=3)
