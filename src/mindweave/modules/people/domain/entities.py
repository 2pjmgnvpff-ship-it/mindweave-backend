"""Pure domain entities, value objects, and exceptions for the People module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from mindweave.shared.enums import AliasKind, PersonState, Tier


class PhotoSlot(StrEnum):
    """Supported optional photo slots on a person's scrapbook page."""

    AVATAR = "avatar"
    MAIN = "main"
    SUB = "sub"


class PersonDomainError(ValueError):
    """Base pure-domain error for People invariant violations."""


class CopyPolicyViolationError(PersonDomainError):
    """Raised when a UI display field violates Invariant I6 (forbidden dev jargon) or bounds."""

    def __init__(
        self, detail: str, *, field_name: str, forbidden_phrase: str | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.field_name = field_name
        self.forbidden_phrase = forbidden_phrase


class InvalidMoveError(PersonDomainError):
    """Raised when a LexoRank move request references invalid or cross-bucket anchors."""


class InvalidTierTransitionError(PersonDomainError):
    """Raised when a tier transition cannot be applied to the target person state."""


_DEFAULT_VISUAL_TOKENS: dict[Tier, dict[str, str]] = {
    Tier.GOLD: {
        "sealBg": "linear-gradient(145deg, #fcf4e6 0%, #efd9b8 100%)",
        "sealColor": "#694418",
        "tapeColor": "rgba(226, 182, 118, 0.76)",
        "tabColor": "#D49A45",
    },
    Tier.SILVER: {
        "sealBg": "linear-gradient(145deg, #eef1f5 0%, #d7dde6 100%)",
        "sealColor": "#364152",
        "tapeColor": "rgba(164, 176, 194, 0.78)",
        "tabColor": "#7B8798",
    },
    Tier.CRYSTAL: {
        "sealBg": "linear-gradient(145deg, #f9f2e7 0%, #ead8be 100%)",
        "sealColor": "#5c4224",
        "tapeColor": "rgba(206, 178, 138, 0.76)",
        "tabColor": "#C59B6D",
    },
}


def default_visual_tokens(tier: Tier) -> dict[str, str]:
    """Return V10 kinetic-stone default seal/tape/tab tokens for a tier."""
    return dict(_DEFAULT_VISUAL_TOKENS[tier])


@dataclass(frozen=True, slots=True)
class PersonProfile:
    """Decrypted, pure-Python domain representation of a Person."""

    id: UUID
    user_id: UUID
    slug: str | None
    name_cn: str
    monogram: str
    role_tag: str
    context_sub: str
    cupid_title: str
    tier: Tier
    rank: str
    state: PersonState
    has_photo: bool
    short_memo: str
    traits: list[str] = field(default_factory=list)
    visual_tokens: dict[str, Any] = field(default_factory=dict)
    avatar_object_key: str | None = None
    main_photo_object_key: str | None = None
    sub_photo_object_key: str | None = None
    version: int = 1
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersonAlias:
    """Decrypted, pure-Python domain representation of a PersonAlias."""

    id: UUID
    user_id: UUID
    person_id: UUID
    alias: str
    kind: AliasKind
    is_primary: bool
    created_at: datetime
