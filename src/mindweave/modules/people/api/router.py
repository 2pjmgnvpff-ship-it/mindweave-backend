"""FastAPI router for People, 3-State Arrow Tiers, LexoRank Move, Aliases, and Photos."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from mindweave.core.dependencies import (
    AppContainer,
    get_container,
    get_current_user_scope,
    get_media_user_scope,
    parse_if_match_version,
    require_idempotency_key,
)
from mindweave.modules.people.api.schemas import (
    AliasSeedItem,
    PersonAliasCreateRequest,
    PersonAliasDTO,
    PersonAliasListResponse,
    PersonCreateRequest,
    PersonDTO,
    PersonListResponse,
    PersonMoveRequest,
    PersonMoveResponse,
    PersonPatchRequest,
    TierTransitionRequest,
    TierTransitionResponse,
)
from mindweave.modules.people.application.service import (
    MoveOutcome,
    PeopleService,
    TierTransitionOutcome,
)
from mindweave.modules.people.domain.entities import PersonAlias, PersonProfile, PhotoSlot
from mindweave.shared.enums import AliasKind
from mindweave.shared.scopes import UserScope

router = APIRouter(tags=["people"])

_STREAM_CHUNK_BYTES = 64 * 1024


def _profile_to_dto(service: PeopleService, scope: UserScope, profile: PersonProfile) -> PersonDTO:
    return PersonDTO(
        id=profile.id,
        slug=profile.slug,
        name_cn=profile.name_cn,
        monogram=profile.monogram,
        role_tag=profile.role_tag,
        context_sub=profile.context_sub,
        cupid_title=profile.cupid_title,
        tier=profile.tier,
        rank=profile.rank,
        state=profile.state,
        has_photo=profile.has_photo,
        short_memo=profile.short_memo,
        traits=profile.traits,
        visual_tokens=profile.visual_tokens,
        avatar_url=service.photo_url(
            scope, profile.id, PhotoSlot.AVATAR, profile.avatar_object_key
        ),
        main_photo_url=service.photo_url(
            scope, profile.id, PhotoSlot.MAIN, profile.main_photo_object_key
        ),
        sub_photo_url=service.photo_url(
            scope, profile.id, PhotoSlot.SUB, profile.sub_photo_object_key
        ),
        version=profile.version,
        archived_at=profile.archived_at,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _alias_to_dto(alias: PersonAlias) -> PersonAliasDTO:
    return PersonAliasDTO(
        id=alias.id,
        person_id=alias.person_id,
        alias=alias.alias,
        kind=alias.kind,
        is_primary=alias.is_primary,
        created_at=alias.created_at,
    )


@router.get(
    "/persons",
    response_model=PersonListResponse,
    response_model_by_alias=True,
)
async def list_persons(
    include_archived_camel: bool | None = Query(default=None, alias="includeArchived"),
    include_archived_snake: bool | None = Query(default=None, alias="include_archived"),
    include_provisional_camel: bool | None = Query(default=None, alias="includeProvisional"),
    include_provisional_snake: bool | None = Query(default=None, alias="include_provisional"),
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> PersonListResponse:
    resolved_include_archived = (
        include_archived_camel
        if include_archived_camel is not None
        else (include_archived_snake if include_archived_snake is not None else False)
    )
    resolved_include_provisional = (
        include_provisional_camel
        if include_provisional_camel is not None
        else (include_provisional_snake if include_provisional_snake is not None else True)
    )
    service = PeopleService(container)
    result = await service.list_persons(
        scope,
        include_archived=resolved_include_archived,
        include_provisional=resolved_include_provisional,
    )
    return PersonListResponse(
        core_ids=result.core_ids,
        casual_ids=result.casual_ids,
        items=[_profile_to_dto(service, scope, p) for p in result.items],
    )


@router.post(
    "/persons",
    response_model=PersonDTO,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_person(
    payload: PersonCreateRequest,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> PersonDTO:
    service = PeopleService(container)
    extra_aliases: list[tuple[str, AliasKind]] = []
    for entry in payload.aliases:
        if isinstance(entry, AliasSeedItem):
            extra_aliases.append((entry.alias, entry.kind))
        else:
            extra_aliases.append((entry, AliasKind.NICKNAME))

    def _serialize(profile: PersonProfile) -> dict[str, Any]:
        return _profile_to_dto(service, scope, profile).model_dump(mode="json", by_alias=True)

    body, code = await service.create_person(
        scope,
        idem_key=idem_key,
        request_payload_for_hash=payload.model_dump(mode="json", by_alias=True),
        person_id=payload.id,
        slug=payload.slug,
        name_cn=payload.name_cn,
        monogram=payload.monogram,
        role_tag=payload.role_tag,
        context_sub=payload.context_sub,
        short_memo=payload.short_memo,
        traits=payload.traits,
        tier=payload.tier,
        visual_tokens=payload.visual_tokens,
        aliases=extra_aliases,
        serialize_profile=_serialize,
    )
    response.status_code = code
    return PersonDTO.model_validate(body)


@router.get(
    "/persons/{person_id}",
    response_model=PersonDTO,
    response_model_by_alias=True,
)
async def get_person(
    person_id: UUID,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> PersonDTO:
    service = PeopleService(container)
    profile = await service.get_person(scope, person_id)
    return _profile_to_dto(service, scope, profile)


@router.patch(
    "/persons/{person_id}",
    response_model=PersonDTO,
    response_model_by_alias=True,
)
async def patch_person(
    person_id: UUID,
    payload: PersonPatchRequest,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    expected_version: int | None = Depends(parse_if_match_version),
    container: AppContainer = Depends(get_container),
) -> PersonDTO:
    service = PeopleService(container)
    patch_fields = payload.model_dump(exclude_unset=True)

    def _serialize(profile: PersonProfile) -> dict[str, Any]:
        return _profile_to_dto(service, scope, profile).model_dump(mode="json", by_alias=True)

    body, code = await service.update_person(
        scope,
        person_id,
        idem_key=idem_key,
        expected_version=expected_version,
        patch_fields=patch_fields,
        serialize_profile=_serialize,
    )
    response.status_code = code
    return PersonDTO.model_validate(body)


@router.delete(
    "/persons/{person_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_person(
    person_id: UUID,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> Response:
    service = PeopleService(container)
    await service.delete_person(
        scope,
        person_id,
        idem_key=idem_key,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/persons/{person_id}/tier",
    response_model=TierTransitionResponse,
    response_model_by_alias=True,
)
async def transition_person_tier(
    person_id: UUID,
    payload: TierTransitionRequest,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> TierTransitionResponse:
    service = PeopleService(container)

    def _serialize(outcome: TierTransitionOutcome) -> dict[str, Any]:
        dto = TierTransitionResponse(
            person_id=outcome.plan.person_id,
            previous_tier=outcome.plan.previous_tier,
            tier=outcome.plan.new_tier,
            core_ids=outcome.plan.core_ids,
            casual_ids=outcome.plan.casual_ids,
            version=outcome.version,
        )
        return dto.model_dump(mode="json", by_alias=True)

    body, code = await service.transition_tier(
        scope,
        person_id,
        target_tier=payload.tier,
        idem_key=idem_key,
        serialize_outcome=_serialize,
    )
    response.status_code = code
    return TierTransitionResponse.model_validate(body)


@router.post(
    "/persons/{person_id}:move",
    response_model=PersonMoveResponse,
    response_model_by_alias=True,
)
async def move_person(
    person_id: UUID,
    payload: PersonMoveRequest,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> PersonMoveResponse:
    service = PeopleService(container)

    def _serialize(outcome: MoveOutcome) -> dict[str, Any]:
        dto = PersonMoveResponse(
            person_id=outcome.plan.person_id,
            tier=outcome.plan.tier,
            rank=outcome.plan.new_rank,
            core_ids=outcome.plan.core_ids,
            casual_ids=outcome.plan.casual_ids,
            version=outcome.version,
        )
        return dto.model_dump(mode="json", by_alias=True)

    body, code = await service.move_person(
        scope,
        person_id,
        after_person_id=payload.after_person_id,
        before_person_id=payload.before_person_id,
        idem_key=idem_key,
        serialize_outcome=_serialize,
    )
    response.status_code = code
    return PersonMoveResponse.model_validate(body)


@router.get(
    "/persons/{person_id}/aliases",
    response_model=PersonAliasListResponse,
    response_model_by_alias=True,
)
async def list_person_aliases(
    person_id: UUID,
    scope: UserScope = Depends(get_current_user_scope),
    container: AppContainer = Depends(get_container),
) -> PersonAliasListResponse:
    service = PeopleService(container)
    aliases = await service.list_aliases(scope, person_id)
    return PersonAliasListResponse(items=[_alias_to_dto(a) for a in aliases])


@router.post(
    "/persons/{person_id}/aliases",
    response_model=PersonAliasDTO,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_person_alias(
    person_id: UUID,
    payload: PersonAliasCreateRequest,
    response: Response,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> PersonAliasDTO:
    service = PeopleService(container)

    def _serialize(alias_item: PersonAlias) -> dict[str, Any]:
        return _alias_to_dto(alias_item).model_dump(mode="json", by_alias=True)

    body, code = await service.add_alias(
        scope,
        person_id,
        alias=payload.alias,
        kind=payload.kind,
        idem_key=idem_key,
        serialize_alias=_serialize,
    )
    response.status_code = code
    return PersonAliasDTO.model_validate(body)


@router.delete(
    "/persons/{person_id}/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_person_alias(
    person_id: UUID,
    alias_id: UUID,
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> Response:
    service = PeopleService(container)
    await service.delete_alias(
        scope,
        person_id,
        alias_id,
        idem_key=idem_key,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/persons/{person_id}/photos/{slot}",
    response_model=PersonDTO,
    response_model_by_alias=True,
)
async def upload_person_photo(
    person_id: UUID,
    slot: PhotoSlot,
    response: Response,
    file: UploadFile = File(...),
    scope: UserScope = Depends(get_current_user_scope),
    idem_key: str = Depends(require_idempotency_key),
    container: AppContainer = Depends(get_container),
) -> PersonDTO:
    service = PeopleService(container)
    max_read = container.settings.max_image_bytes + 1
    raw_bytes = await file.read(max_read)

    def _serialize(profile: PersonProfile) -> dict[str, Any]:
        return _profile_to_dto(service, scope, profile).model_dump(mode="json", by_alias=True)

    body, code = await service.upload_photo(
        scope,
        person_id,
        slot,
        raw_bytes=raw_bytes,
        idem_key=idem_key,
        serialize_profile=_serialize,
    )
    response.status_code = code
    return PersonDTO.model_validate(body)


@router.get(
    "/persons/{person_id}/photos/{slot}",
    response_class=StreamingResponse,
)
async def get_person_photo(
    person_id: UUID,
    slot: PhotoSlot,
    scope: UserScope = Depends(get_media_user_scope),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    service = PeopleService(container)
    plaintext, content_type = await service.get_photo(scope, person_id, slot)

    def _chunks(buf: bytes) -> Iterator[bytes]:
        for offset in range(0, len(buf), _STREAM_CHUNK_BYTES):
            yield buf[offset : offset + _STREAM_CHUNK_BYTES]

    return StreamingResponse(
        _chunks(plaintext),
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(plaintext)),
        },
    )
