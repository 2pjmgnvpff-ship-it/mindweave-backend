"""Application use-case service for the People module."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionMismatchError,
)
from mindweave.core.idempotency import hash_request_payload
from mindweave.core.ids import new_id
from mindweave.modules.people.domain.copy_policy import (
    MAX_CONTEXT_SUB_LEN,
    MAX_ROLE_TAG_LEN,
    MAX_SHORT_MEMO_LEN,
    derive_monogram,
    validate_alias_text,
    validate_name_cn,
    validate_optional_copy_field,
    validate_traits,
    validate_visual_tokens,
)
from mindweave.modules.people.domain.entities import (
    CopyPolicyViolationError,
    InvalidMoveError,
    InvalidTierTransitionError,
    PersonAlias,
    PersonProfile,
    PhotoSlot,
    default_visual_tokens,
)
from mindweave.modules.people.domain.tier_transition import (
    MovePlan,
    TierTransitionPlan,
    compute_move,
    compute_new_person_rank,
    compute_tier_transition,
    partition_person_ids,
)
from mindweave.modules.people.infra.media import verify_and_strip_exif
from mindweave.modules.people.infra.repository import PeopleRepository
from mindweave.shared.enums import UNASSIGNED_PERSON_SLUG, AliasKind, PersonState, Tier
from mindweave.shared.scopes import UserScope


def _translate_domain_error(exc: Exception) -> ValidationFailedError | ConflictError:
    if isinstance(exc, CopyPolicyViolationError):
        return ValidationFailedError(
            exc.detail,
            extensions={"field": exc.field_name},
        )
    if isinstance(exc, InvalidMoveError):
        return ValidationFailedError(str(exc))
    if isinstance(exc, InvalidTierTransitionError):
        return ConflictError(str(exc))
    return ValidationFailedError(str(exc))


@dataclass(frozen=True, slots=True)
class ListedPersonsResult:
    core_ids: list[UUID]
    casual_ids: list[UUID]
    items: list[PersonProfile]


@dataclass(frozen=True, slots=True)
class TierTransitionOutcome:
    plan: TierTransitionPlan
    version: int


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    plan: MovePlan
    version: int


class PeopleService:
    """Transactional use cases for Persons, 3-State Arrow Tiers, Aliases, and Encrypted Photos."""

    def __init__(self, container: AppContainer) -> None:
        self._container = container
        self._repo = PeopleRepository(container.cipher)

    def photo_url(
        self,
        scope: UserScope,
        person_id: UUID,
        slot: PhotoSlot,
        object_key: str | None,
    ) -> str | None:
        if not object_key:
            return None
        prefix = self._container.settings.api_prefix.rstrip("/")
        raw_path = f"{prefix}/persons/{person_id}/photos/{slot.value}"
        return self._container.tokens.sign_media_path(
            raw_path,
            user_id=scope.user_id,
            now=self._container.clock.now(),
        )

    async def list_persons(
        self,
        scope: UserScope,
        *,
        include_archived: bool = False,
        include_provisional: bool = True,
    ) -> ListedPersonsResult:
        async with self._container.db.user_session(scope) as session:
            rows = await self._repo.list_non_deleted_rows(session, scope)
            if not rows:
                return ListedPersonsResult(core_ids=[], casual_ids=[], items=[])

            dek = await self._container.load_user_dek(session, scope)
            entries = [self._repo.to_rank_entry(r) for r in rows]
            partition = partition_person_ids(entries, include_provisional=include_provisional)

            row_by_id = {r.id: r for r in rows}
            ordered_profiles: list[PersonProfile] = []
            seen: set[UUID] = set()

            for pid in (*partition.core_ids, *partition.casual_ids):
                row = row_by_id.get(pid)
                if row is not None:
                    ordered_profiles.append(
                        self._repo.decrypt_person_row(row, scope=scope, dek=dek)
                    )
                    seen.add(pid)

            if include_archived:
                for row in rows:
                    if row.id in seen:
                        continue
                    st = PersonState(row.state)
                    if st is PersonState.ARCHIVED or row.archived_at is not None:
                        ordered_profiles.append(
                            self._repo.decrypt_person_row(row, scope=scope, dek=dek)
                        )
                        seen.add(row.id)

            return ListedPersonsResult(
                core_ids=partition.core_ids,
                casual_ids=partition.casual_ids,
                items=ordered_profiles,
            )

    async def get_person(self, scope: UserScope, person_id: UUID) -> PersonProfile:
        async with self._container.db.user_session(scope) as session:
            row = await self._repo.get_person_row(session, scope, person_id)
            dek = await self._container.load_user_dek(session, scope)
            return self._repo.decrypt_person_row(row, scope=scope, dek=dek)

    async def create_person(
        self,
        scope: UserScope,
        *,
        idem_key: str,
        request_payload_for_hash: Mapping[str, Any],
        person_id: UUID | None,
        slug: str | None,
        name_cn: str,
        monogram: str | None,
        role_tag: str,
        context_sub: str,
        short_memo: str,
        traits: Sequence[str],
        tier: Tier,
        visual_tokens: Mapping[str, Any] | None,
        aliases: Sequence[tuple[str, AliasKind]],
        serialize_profile: Any,
    ) -> tuple[dict[str, Any], int]:
        try:
            cleaned_name = validate_name_cn(name_cn)
            resolved_monogram = derive_monogram(cleaned_name, monogram)
            cleaned_role = validate_optional_copy_field(
                role_tag, field_name="roleTag", max_length=MAX_ROLE_TAG_LEN
            )
            cleaned_context = validate_optional_copy_field(
                context_sub, field_name="contextSub", max_length=MAX_CONTEXT_SUB_LEN
            )
            cleaned_memo = validate_optional_copy_field(
                short_memo, field_name="shortMemo", max_length=MAX_SHORT_MEMO_LEN
            )
            cleaned_traits = validate_traits(traits)
            cleaned_slug = (
                validate_optional_copy_field(slug, field_name="slug", max_length=64) or None
                if slug is not None
                else None
            )
            if cleaned_slug == UNASSIGNED_PERSON_SLUG:
                raise ValidationFailedError(
                    "Reserved internal slug.",
                    extensions={"field": "slug"},
                )
            resolved_tokens = (
                validate_visual_tokens(visual_tokens)
                if visual_tokens is not None
                else default_visual_tokens(tier)
            )
            cleaned_aliases = [(validate_alias_text(a_text), a_kind) for a_text, a_kind in aliases]
        except CopyPolicyViolationError as exc:
            raise _translate_domain_error(exc) from exc

        req_hash = hash_request_payload(request_payload_for_hash)
        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path="/v1/persons",
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            dek = await self._container.load_user_dek(session, scope)
            now = self._container.clock.now()
            locked_rows = await self._repo.lock_user_persons_for_update(session, scope)
            entries = [self._repo.to_rank_entry(r) for r in locked_rows]
            new_rank, peer_updates = compute_new_person_rank(entries, tier=tier)
            if peer_updates:
                row_map = {r.id: r for r in locked_rows}
                for pid, updated_rank in peer_updates.items():
                    if pid in row_map:
                        row_map[pid].rank = updated_rank
                        row_map[pid].updated_at = now

            target_id = person_id or new_id()
            row = await self._repo.create_person_with_aliases(
                session,
                scope,
                dek,
                person_id=target_id,
                slug=cleaned_slug,
                name_cn=cleaned_name,
                monogram=resolved_monogram,
                role_tag=cleaned_role,
                context_sub=cleaned_context,
                short_memo=cleaned_memo,
                traits=cleaned_traits,
                tier=tier,
                rank=new_rank,
                state=PersonState.ACTIVE,
                visual_tokens=resolved_tokens,
                extra_aliases=cleaned_aliases,
                now=now,
            )
            profile = self._repo.decrypt_person_row(row, scope=scope, dek=dek)
            body: dict[str, Any] = serialize_profile(profile)
            await self._container.idempotency.complete(
                session, idem_row, status_code=201, response_body=body
            )
            return body, 201

    async def update_person(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        idem_key: str,
        expected_version: int | None,
        patch_fields: Mapping[str, Any],
        serialize_profile: Any,
    ) -> tuple[dict[str, Any], int]:
        req_hash = hash_request_payload(
            {
                "ifMatch": expected_version,
                "patch": dict(patch_fields),
            }
        )
        path = f"/v1/persons/{person_id}"
        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="PATCH",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            row = await self._repo.get_person_row(session, scope, person_id, for_update=True)
            if expected_version is not None and row.version != expected_version:
                raise VersionMismatchError(
                    f"Expected person version {expected_version}, found {row.version}."
                )

            dek = await self._container.load_user_dek(session, scope)
            now = self._container.clock.now()
            current_name = self._repo.decrypt_person_str(
                dek,
                row.display_name_enc,
                scope=scope,
                person_id=row.id,
                column="display_name_enc",
            )

            try:
                if "name_cn" in patch_fields and patch_fields["name_cn"] is not None:
                    new_name = validate_name_cn(str(patch_fields["name_cn"]))
                    if new_name != current_name:
                        row.display_name_enc = self._repo.encrypt_person_str(
                            dek,
                            new_name,
                            scope=scope,
                            person_id=row.id,
                            column="display_name_enc",
                        )
                        await self._repo.sync_primary_alias(
                            session,
                            scope,
                            dek,
                            person_id=row.id,
                            new_name_cn=new_name,
                            now=now,
                        )
                        current_name = new_name

                if "monogram" in patch_fields and patch_fields["monogram"] is not None:
                    row.monogram = derive_monogram(current_name, str(patch_fields["monogram"]))

                if "slug" in patch_fields:
                    raw_slug = patch_fields["slug"]
                    next_slug = (
                        validate_optional_copy_field(
                            str(raw_slug), field_name="slug", max_length=64
                        )
                        or None
                        if raw_slug is not None
                        else None
                    )
                    if next_slug == UNASSIGNED_PERSON_SLUG:
                        raise ValidationFailedError(
                            "Reserved internal slug.",
                            extensions={"field": "slug"},
                        )
                    row.slug = next_slug

                if "role_tag" in patch_fields and patch_fields["role_tag"] is not None:
                    cleaned_role = validate_optional_copy_field(
                        str(patch_fields["role_tag"]),
                        field_name="roleTag",
                        max_length=MAX_ROLE_TAG_LEN,
                    )
                    row.role_tag_enc = self._repo.encrypt_person_str(
                        dek,
                        cleaned_role,
                        scope=scope,
                        person_id=row.id,
                        column="role_tag_enc",
                    )

                if "context_sub" in patch_fields and patch_fields["context_sub"] is not None:
                    cleaned_ctx = validate_optional_copy_field(
                        str(patch_fields["context_sub"]),
                        field_name="contextSub",
                        max_length=MAX_CONTEXT_SUB_LEN,
                    )
                    row.context_sub_enc = self._repo.encrypt_person_str(
                        dek,
                        cleaned_ctx,
                        scope=scope,
                        person_id=row.id,
                        column="context_sub_enc",
                    )

                if "short_memo" in patch_fields and patch_fields["short_memo"] is not None:
                    cleaned_memo = validate_optional_copy_field(
                        str(patch_fields["short_memo"]),
                        field_name="shortMemo",
                        max_length=MAX_SHORT_MEMO_LEN,
                    )
                    row.short_memo_enc = self._repo.encrypt_person_str(
                        dek,
                        cleaned_memo,
                        scope=scope,
                        person_id=row.id,
                        column="short_memo_enc",
                    )

                if "traits" in patch_fields and patch_fields["traits"] is not None:
                    cleaned_traits = validate_traits(patch_fields["traits"])
                    row.traits_enc = self._repo.encrypt_traits(
                        dek,
                        cleaned_traits,
                        scope=scope,
                        person_id=row.id,
                    )

                if "visual_tokens" in patch_fields and patch_fields["visual_tokens"] is not None:
                    row.visual_tokens = validate_visual_tokens(patch_fields["visual_tokens"])
            except CopyPolicyViolationError as exc:
                raise _translate_domain_error(exc) from exc

            # Handle `archived` boolean and/or explicit `state` transition
            archived_flag: bool | None = patch_fields.get("archived")
            explicit_state: PersonState | None = patch_fields.get("state")

            if archived_flag is True or explicit_state is PersonState.ARCHIVED:
                row.state = PersonState.ARCHIVED.value
                if row.archived_at is None:
                    row.archived_at = now
                await self._repo.pause_active_reminders_for_person(
                    session, scope, person_id=row.id, now=now
                )
            elif archived_flag is False or (
                explicit_state in (PersonState.ACTIVE, PersonState.PROVISIONAL)
            ):
                was_archived = (
                    row.state == PersonState.ARCHIVED.value or row.archived_at is not None
                )
                target_state = (
                    explicit_state
                    if explicit_state in (PersonState.ACTIVE, PersonState.PROVISIONAL)
                    else PersonState.ACTIVE
                )
                row.state = target_state.value
                row.archived_at = None
                if was_archived:
                    all_rows = await self._repo.lock_user_persons_for_update(session, scope)
                    other_entries = [
                        self._repo.to_rank_entry(r) for r in all_rows if r.id != row.id
                    ]
                    new_rank, peer_updates = compute_new_person_rank(
                        other_entries, tier=Tier(row.tier)
                    )
                    row.rank = new_rank
                    if peer_updates:
                        rmap = {r.id: r for r in all_rows}
                        for pid, urank in peer_updates.items():
                            if pid in rmap:
                                rmap[pid].rank = urank
                                rmap[pid].updated_at = now

            row.version += 1
            row.updated_at = now
            await session.flush()

            profile = self._repo.decrypt_person_row(row, scope=scope, dek=dek)
            body: dict[str, Any] = serialize_profile(profile)
            await self._container.idempotency.complete(
                session, idem_row, status_code=200, response_body=body
            )
            return body, 200

    async def delete_person(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        idem_key: str,
    ) -> None:
        path = f"/v1/persons/{person_id}"
        req_hash = hash_request_payload({"delete": str(person_id)})

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="DELETE",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return
            assert idem_row is not None

            now = self._container.clock.now()
            await self._repo.soft_delete_person(
                session,
                scope,
                person_id=person_id,
                now=now,
            )
            await self._container.idempotency.complete(
                session, idem_row, status_code=204, response_body={"deleted": True}
            )

    async def transition_tier(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        target_tier: Tier,
        idem_key: str,
        serialize_outcome: Any,
    ) -> tuple[dict[str, Any], int]:
        path = f"/v1/persons/{person_id}/tier"
        req_hash = hash_request_payload({"tier": target_tier.value})

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="PATCH",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            locked_rows = await self._repo.lock_user_persons_for_update(session, scope)
            row_map = {r.id: r for r in locked_rows}
            target_row = row_map.get(person_id)
            if target_row is None:
                raise NotFoundError("Person not found.")

            entries = [self._repo.to_rank_entry(r) for r in locked_rows]
            try:
                plan = compute_tier_transition(
                    entries, person_id=person_id, target_tier=target_tier
                )
            except InvalidTierTransitionError as exc:
                raise _translate_domain_error(exc) from exc

            now = self._container.clock.now()
            if plan.previous_tier is not plan.new_tier or plan.rank_updates:
                target_row.tier = plan.new_tier.value
                target_row.rank = plan.new_rank
                target_row.version += 1
                target_row.updated_at = now
                for pid, updated_rank in plan.rank_updates.items():
                    if pid != person_id and pid in row_map:
                        row_map[pid].rank = updated_rank
                        row_map[pid].updated_at = now
                await session.flush()

            outcome = TierTransitionOutcome(plan=plan, version=target_row.version)
            body: dict[str, Any] = serialize_outcome(outcome)
            await self._container.idempotency.complete(
                session, idem_row, status_code=200, response_body=body
            )
            return body, 200

    async def move_person(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        after_person_id: UUID | None,
        before_person_id: UUID | None,
        idem_key: str,
        serialize_outcome: Any,
    ) -> tuple[dict[str, Any], int]:
        path = f"/v1/persons/{person_id}:move"
        req_hash = hash_request_payload(
            {
                "afterPersonId": str(after_person_id) if after_person_id else None,
                "beforePersonId": str(before_person_id) if before_person_id else None,
            }
        )

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            locked_rows = await self._repo.lock_user_persons_for_update(session, scope)
            row_map = {r.id: r for r in locked_rows}
            target_row = row_map.get(person_id)
            if target_row is None:
                raise NotFoundError("Person not found.")

            entries = [self._repo.to_rank_entry(r) for r in locked_rows]
            try:
                plan = compute_move(
                    entries,
                    person_id=person_id,
                    after_person_id=after_person_id,
                    before_person_id=before_person_id,
                )
            except InvalidMoveError as exc:
                raise _translate_domain_error(exc) from exc

            now = self._container.clock.now()
            if plan.rank_updates:
                target_row.rank = plan.new_rank
                target_row.version += 1
                target_row.updated_at = now
                for pid, updated_rank in plan.rank_updates.items():
                    if pid != person_id and pid in row_map:
                        row_map[pid].rank = updated_rank
                        row_map[pid].updated_at = now
                await session.flush()

            outcome = MoveOutcome(plan=plan, version=target_row.version)
            body: dict[str, Any] = serialize_outcome(outcome)
            await self._container.idempotency.complete(
                session, idem_row, status_code=200, response_body=body
            )
            return body, 200

    async def list_aliases(
        self,
        scope: UserScope,
        person_id: UUID,
    ) -> list[PersonAlias]:
        async with self._container.db.user_session(scope) as session:
            await self._repo.get_person_row(session, scope, person_id)
            dek = await self._container.load_user_dek(session, scope)
            rows = await self._repo.list_alias_rows(session, scope, person_id)
            return [self._repo.decrypt_alias_row(r, scope=scope, dek=dek) for r in rows]

    async def add_alias(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        alias: str,
        kind: AliasKind,
        idem_key: str,
        serialize_alias: Any,
    ) -> tuple[dict[str, Any], int]:
        try:
            cleaned_alias = validate_alias_text(alias)
        except CopyPolicyViolationError as exc:
            raise _translate_domain_error(exc) from exc

        path = f"/v1/persons/{person_id}/aliases"
        req_hash = hash_request_payload({"alias": cleaned_alias, "kind": kind.value})

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            await self._repo.get_person_row(session, scope, person_id, for_update=True)
            dek = await self._container.load_user_dek(session, scope)
            now = self._container.clock.now()
            row = await self._repo.add_alias_row(
                session,
                scope,
                dek,
                person_id=person_id,
                alias_text=cleaned_alias,
                kind=kind,
                now=now,
            )
            decrypted = self._repo.decrypt_alias_row(row, scope=scope, dek=dek)
            body: dict[str, Any] = serialize_alias(decrypted)
            await self._container.idempotency.complete(
                session, idem_row, status_code=201, response_body=body
            )
            return body, 201

    async def delete_alias(
        self,
        scope: UserScope,
        person_id: UUID,
        alias_id: UUID,
        *,
        idem_key: str,
    ) -> None:
        path = f"/v1/persons/{person_id}/aliases/{alias_id}"
        req_hash = hash_request_payload({"aliasId": str(alias_id)})

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="DELETE",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return
            assert idem_row is not None

            await self._repo.get_person_row(session, scope, person_id)
            await self._repo.delete_alias_row(
                session, scope, person_id=person_id, alias_id=alias_id
            )
            await self._container.idempotency.complete(
                session, idem_row, status_code=204, response_body={"deleted": True}
            )

    async def upload_photo(
        self,
        scope: UserScope,
        person_id: UUID,
        slot: PhotoSlot,
        *,
        raw_bytes: bytes,
        idem_key: str,
        serialize_profile: Any,
    ) -> tuple[dict[str, Any], int]:
        path = f"/v1/persons/{person_id}/photos/{slot.value}"
        digest = hashlib.sha256(raw_bytes).hexdigest()
        req_hash = hash_request_payload({"slot": slot.value, "sha256": digest})

        # Verify image and strip EXIF BEFORE mutating DB or blob store
        processed = await verify_and_strip_exif(
            raw_bytes, max_bytes=self._container.settings.max_image_bytes
        )

        old_object_key: str | None = None
        new_object_key = (
            f"{scope.user_id}/persons/{person_id}/{slot.value}_{new_id()}.{processed.extension}"
        )

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            await self._repo.get_person_row(session, scope, person_id, for_update=True)
            dek = await self._container.load_user_dek(session, scope)
            await self._container.storage.put_encrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=new_object_key,
                plaintext=processed.data,
                content_type=processed.content_type,
            )

            now = self._container.clock.now()
            token_updates = (
                processed.chroma.to_visual_token_dict()
                if slot is PhotoSlot.MAIN and processed.chroma is not None
                else None
            )
            row, old_object_key = await self._repo.set_photo_object_key(
                session,
                scope,
                person_id=person_id,
                slot=slot,
                object_key=new_object_key,
                now=now,
                visual_token_updates=token_updates,
            )
            profile = self._repo.decrypt_person_row(row, scope=scope, dek=dek)
            body: dict[str, Any] = serialize_profile(profile)
            await self._container.idempotency.complete(
                session, idem_row, status_code=200, response_body=body
            )

        if old_object_key and old_object_key != new_object_key:
            await self._container.storage.delete_object(object_key=old_object_key)

        return body, 200

    async def set_background_from_material(
        self,
        scope: UserScope,
        person_id: UUID,
        *,
        material_id: UUID,
        face_zone: str | None,
        desk_focus: str | None,
        phone_focus: str | None,
        idem_key: str,
        serialize_profile: Any,
    ) -> tuple[dict[str, Any], int]:
        """Promote an existing photo material on this person's journal to their background portrait."""
        path = f"/v1/persons/{person_id}/background:from-material"
        req_hash = hash_request_payload(
            {
                "personId": str(person_id),
                "materialId": str(material_id),
                "faceZone": face_zone,
                "deskFocus": desk_focus,
                "phoneFocus": phone_focus,
            }
        )

        old_object_key: str | None = None
        new_object_key: str | None = None

        async with self._container.db.user_session(scope) as session:
            idem_row, hit = await self._container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=path,
                request_hash=req_hash,
            )
            if hit is not None:
                return hit.response_body, hit.status_code
            assert idem_row is not None

            person_row = await self._repo.get_person_row(session, scope, person_id, for_update=True)
            if Tier.from_any(person_row.tier) is Tier.SILVER:
                raise ValidationFailedError(
                    "银箭人物遵循零照片隐私护盾，不可设置背景摄影。",
                    extensions={"field": "tier"},
                )

            mat_row = await self._repo.get_person_photo_material_row(
                session, scope, person_id=person_id, material_id=material_id
            )
            if mat_row is None:
                raise NotFoundError("Material not found for this person.")
            if not mat_row.media_object_key:
                raise ValidationFailedError(
                    "Target material does not contain a photo.",
                    extensions={"field": "materialId"},
                )

            dek = await self._container.load_user_dek(session, scope)
            raw_bytes, _ctype = await self._container.storage.get_decrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=mat_row.media_object_key,
            )
            processed = await verify_and_strip_exif(
                raw_bytes, max_bytes=self._container.settings.max_image_bytes
            )
            new_object_key = (
                f"{scope.user_id}/persons/{person_id}/main_{new_id()}.{processed.extension}"
            )
            await self._container.storage.put_encrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=new_object_key,
                plaintext=processed.data,
                content_type=processed.content_type,
            )

            token_updates: dict[str, Any] = (
                dict(processed.chroma.to_visual_token_dict())
                if processed.chroma is not None
                else {}
            )
            token_updates["activeHeroMaterialId"] = str(material_id)
            if face_zone is not None:
                token_updates["faceZone"] = face_zone
            try:
                if desk_focus is not None:
                    if "\x00" in desk_focus:
                        raise ValidationFailedError("Null bytes are not permitted in deskFocus.")
                    token_updates["deskFocus"] = validate_optional_copy_field(
                        desk_focus, field_name="deskFocus", max_length=32
                    )
                if phone_focus is not None:
                    if "\x00" in phone_focus:
                        raise ValidationFailedError("Null bytes are not permitted in phoneFocus.")
                    token_updates["phoneFocus"] = validate_optional_copy_field(
                        phone_focus, field_name="phoneFocus", max_length=32
                    )
            except CopyPolicyViolationError as exc:
                raise _translate_domain_error(exc) from exc

            now = self._container.clock.now()
            row, old_object_key = await self._repo.set_photo_object_key(
                session,
                scope,
                person_id=person_id,
                slot=PhotoSlot.MAIN,
                object_key=new_object_key,
                now=now,
                visual_token_updates=token_updates,
            )
            profile = self._repo.decrypt_person_row(row, scope=scope, dek=dek)
            body: dict[str, Any] = serialize_profile(profile)
            await self._container.idempotency.complete(
                session, idem_row, status_code=200, response_body=body
            )

        if old_object_key and old_object_key != new_object_key:
            await self._container.storage.delete_object(object_key=old_object_key)

        return body, 200

    async def get_photo(
        self,
        scope: UserScope,
        person_id: UUID,
        slot: PhotoSlot,
    ) -> tuple[bytes, str]:
        async with self._container.db.user_session(scope) as session:
            row = await self._repo.get_person_row(session, scope, person_id)
            if slot is PhotoSlot.AVATAR:
                object_key = row.avatar_object_key
            elif slot is PhotoSlot.MAIN:
                object_key = row.main_photo_object_key
            else:
                object_key = row.sub_photo_object_key

            if not object_key:
                raise NotFoundError(f"No '{slot.value}' photo uploaded for this person.")

            dek = await self._container.load_user_dek(session, scope)
            return await self._container.storage.get_decrypted(
                user_id=scope.user_id,
                dek=dek,
                object_key=object_key,
            )
