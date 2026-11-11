"""CamelCase Pydantic v2 request/response DTOs for the People API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from mindweave.core.dto import CamelModel
from mindweave.shared.enums import AliasKind, PersonState, Tier


def _coerce_tier(value: Any) -> Tier:
    if isinstance(value, Tier):
        return value
    if isinstance(value, str):
        return Tier.from_any(value)
    raise ValueError("Invalid arrow tier value.")


class AliasSeedItem(CamelModel):
    """Optional structured alias seed accepted on `POST /persons`."""

    alias: str
    kind: AliasKind = AliasKind.NICKNAME


class PersonDTO(CamelModel):
    """Canonical Person representation returned by People endpoints."""

    id: UUID
    slug: str | None = None
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
    traits: list[str] = Field(default_factory=list)
    visual_tokens: dict[str, Any] = Field(default_factory=dict)
    avatar_url: str | None = None
    main_photo_url: str | None = None
    sub_photo_url: str | None = None
    version: int
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PersonListResponse(CamelModel):
    """Response for `GET /persons`."""

    core_ids: list[UUID]
    casual_ids: list[UUID]
    items: list[PersonDTO]


class PersonCreateRequest(CamelModel):
    """Request body for `POST /persons`."""

    id: UUID | None = None
    slug: str | None = None
    name_cn: str
    monogram: str | None = None
    role_tag: str = ""
    context_sub: str = ""
    short_memo: str = ""
    traits: list[str] = Field(default_factory=list)
    tier: Tier = Tier.CRYSTAL
    visual_tokens: dict[str, Any] | None = None
    aliases: list[str | AliasSeedItem] = Field(default_factory=list)

    @field_validator("tier", mode="before")
    @classmethod
    def _validate_tier(cls, v: Any) -> Tier:
        return _coerce_tier(v)


class PersonPatchRequest(CamelModel):
    """Partial update request body for `PATCH /persons/{person_id}`."""

    name_cn: str | None = None
    monogram: str | None = None
    slug: str | None = None
    role_tag: str | None = None
    context_sub: str | None = None
    short_memo: str | None = None
    traits: list[str] | None = None
    visual_tokens: dict[str, Any] | None = None
    archived: bool | None = None
    state: PersonState | None = None


class TierTransitionRequest(CamelModel):
    """Request body for `PATCH /persons/{person_id}/tier`."""

    tier: Tier

    @field_validator("tier", mode="before")
    @classmethod
    def _validate_tier(cls, v: Any) -> Tier:
        return _coerce_tier(v)


class TierTransitionResponse(CamelModel):
    """Exact V10 response contract for `PATCH /persons/{person_id}/tier`."""

    person_id: UUID
    previous_tier: Tier
    tier: Tier
    core_ids: list[UUID]
    casual_ids: list[UUID]
    version: int


class PersonMoveRequest(CamelModel):
    """Request body for `POST /persons/{person_id}:move`."""

    after_person_id: UUID | None = None
    before_person_id: UUID | None = None


class PersonMoveResponse(CamelModel):
    """Response body for `POST /persons/{person_id}:move`."""

    person_id: UUID
    tier: Tier
    rank: str
    core_ids: list[UUID]
    casual_ids: list[UUID]
    version: int


class PersonAliasDTO(CamelModel):
    """Decrypted alias item returned by `/persons/{person_id}/aliases`."""

    id: UUID
    person_id: UUID
    alias: str
    kind: AliasKind
    is_primary: bool
    created_at: datetime


class PersonAliasListResponse(CamelModel):
    """Response body for `GET /persons/{person_id}/aliases`."""

    items: list[PersonAliasDTO]


class PersonAliasCreateRequest(CamelModel):
    """Request body for `POST /persons/{person_id}/aliases`."""

    alias: str
    kind: AliasKind = AliasKind.NICKNAME
