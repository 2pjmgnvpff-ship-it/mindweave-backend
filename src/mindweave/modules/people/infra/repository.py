"""SQLAlchemy 2.0 encrypted repository for `persons` and `person_aliases` (Invariants I1, I12)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher, normalize_alias_for_index
from mindweave.core.errors import ConflictError, NotFoundError
from mindweave.core.ids import new_id
from mindweave.core.orm import PersonAliasRow, PersonRow, RecordRow
from mindweave.modules.people.domain.copy_policy import derive_cupid_title
from mindweave.modules.people.domain.entities import (
    PersonAlias,
    PersonProfile,
    PhotoSlot,
)
from mindweave.modules.people.domain.tier_transition import PersonRankEntry
from mindweave.shared.enums import AliasKind, PersonState, Tier
from mindweave.shared.scopes import UserScope

ALIAS_BLIND_INDEX_DOMAIN = "person_alias"


class PeopleRepository:
    """Tenant-scoped persistence adapter binding per-column AAD and blind-index HMAC."""

    def __init__(self, cipher: EnvelopeCipher) -> None:
        self._cipher = cipher

    # ------------------------------------------------------------------
    # Encryption / Decryption Helpers (Exact AAD Binding — Invariant I12)
    # ------------------------------------------------------------------

    def encrypt_person_str(
        self,
        dek: bytes,
        plaintext: str,
        *,
        scope: UserScope,
        person_id: UUID,
        column: str,
    ) -> bytes:
        return self._cipher.encrypt_str(
            dek,
            plaintext,
            user_id=scope.user_id,
            table="persons",
            column=column,
            row_id=person_id,
        )

    def decrypt_person_str(
        self,
        dek: bytes,
        ciphertext: bytes | None,
        *,
        scope: UserScope,
        person_id: UUID,
        column: str,
    ) -> str:
        if ciphertext is None:
            return ""
        return self._cipher.decrypt_str(
            dek,
            ciphertext,
            user_id=scope.user_id,
            table="persons",
            column=column,
            row_id=person_id,
        )

    def encrypt_traits(
        self,
        dek: bytes,
        traits: list[str],
        *,
        scope: UserScope,
        person_id: UUID,
    ) -> bytes:
        return self._cipher.encrypt_json(
            dek,
            traits,
            user_id=scope.user_id,
            table="persons",
            column="traits_enc",
            row_id=person_id,
        )

    def decrypt_traits(
        self,
        dek: bytes,
        ciphertext: bytes | None,
        *,
        scope: UserScope,
        person_id: UUID,
    ) -> list[str]:
        if ciphertext is None:
            return []
        raw = self._cipher.decrypt_json(
            dek,
            ciphertext,
            user_id=scope.user_id,
            table="persons",
            column="traits_enc",
            row_id=person_id,
        )
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw]

    def compute_alias_hmac(self, dek: bytes, *, scope: UserScope, alias: str) -> bytes:
        normalized = normalize_alias_for_index(alias)
        return self._cipher.compute_blind_index(
            dek,
            user_id=scope.user_id,
            domain=ALIAS_BLIND_INDEX_DOMAIN,
            normalized_value=normalized,
        )

    def encrypt_alias(
        self,
        dek: bytes,
        alias: str,
        *,
        scope: UserScope,
        alias_id: UUID,
    ) -> bytes:
        return self._cipher.encrypt_str(
            dek,
            alias,
            user_id=scope.user_id,
            table="person_aliases",
            column="alias_enc",
            row_id=alias_id,
        )

    def decrypt_alias_row(
        self,
        row: PersonAliasRow,
        *,
        scope: UserScope,
        dek: bytes,
    ) -> PersonAlias:
        alias_text = self._cipher.decrypt_str(
            dek,
            row.alias_enc,
            user_id=scope.user_id,
            table="person_aliases",
            column="alias_enc",
            row_id=row.id,
        )
        return PersonAlias(
            id=row.id,
            user_id=row.user_id,
            person_id=row.person_id,
            alias=alias_text,
            kind=AliasKind(row.kind),
            is_primary=row.is_primary,
            created_at=row.created_at,
        )

    def decrypt_person_row(
        self,
        row: PersonRow,
        *,
        scope: UserScope,
        dek: bytes,
    ) -> PersonProfile:
        name_cn = self.decrypt_person_str(
            dek,
            row.display_name_enc,
            scope=scope,
            person_id=row.id,
            column="display_name_enc",
        )
        role_tag = self.decrypt_person_str(
            dek,
            row.role_tag_enc,
            scope=scope,
            person_id=row.id,
            column="role_tag_enc",
        )
        context_sub = self.decrypt_person_str(
            dek,
            row.context_sub_enc,
            scope=scope,
            person_id=row.id,
            column="context_sub_enc",
        )
        short_memo = self.decrypt_person_str(
            dek,
            row.short_memo_enc,
            scope=scope,
            person_id=row.id,
            column="short_memo_enc",
        )
        traits = self.decrypt_traits(
            dek,
            row.traits_enc,
            scope=scope,
            person_id=row.id,
        )
        return PersonProfile(
            id=row.id,
            user_id=row.user_id,
            slug=row.slug,
            name_cn=name_cn,
            monogram=row.monogram,
            role_tag=role_tag,
            context_sub=context_sub,
            cupid_title=derive_cupid_title(name_cn),
            tier=Tier(row.tier),
            rank=row.rank,
            state=PersonState(row.state),
            has_photo=row.has_photo,
            short_memo=short_memo,
            traits=traits,
            visual_tokens=dict(row.visual_tokens or {}),
            avatar_object_key=row.avatar_object_key,
            main_photo_object_key=row.main_photo_object_key,
            sub_photo_object_key=row.sub_photo_object_key,
            version=row.version,
            archived_at=row.archived_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def to_rank_entry(row: PersonRow) -> PersonRankEntry:
        return PersonRankEntry(
            person_id=row.id,
            tier=Tier(row.tier),
            rank=row.rank,
            state=PersonState(row.state),
            deleted=row.deleted_at is not None,
        )

    # ------------------------------------------------------------------
    # Queries & Row Locking
    # ------------------------------------------------------------------

    async def list_non_deleted_rows(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> list[PersonRow]:
        stmt = (
            select(PersonRow)
            .where(
                PersonRow.user_id == scope.user_id,
                PersonRow.deleted_at.is_(None),
            )
            .order_by(PersonRow.rank.asc(), PersonRow.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def lock_user_persons_for_update(
        self,
        session: AsyncSession,
        scope: UserScope,
    ) -> list[PersonRow]:
        """Acquire deterministic row-level locks (`ORDER BY id ASC FOR UPDATE`) to serialize LexoRank mutations."""
        stmt = (
            select(PersonRow)
            .where(
                PersonRow.user_id == scope.user_id,
                PersonRow.deleted_at.is_(None),
            )
            .order_by(PersonRow.id.asc())
            .with_for_update()
        )
        return list((await session.scalars(stmt)).all())

    async def get_person_row(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_id: UUID,
        *,
        for_update: bool = False,
    ) -> PersonRow:
        stmt = select(PersonRow).where(
            PersonRow.user_id == scope.user_id,
            PersonRow.id == person_id,
            PersonRow.deleted_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = await session.scalar(stmt)
        if row is None:
            raise NotFoundError("Person not found.")
        return row

    # ------------------------------------------------------------------
    # Mutations (Persons + Aliases)
    # ------------------------------------------------------------------

    async def create_person_with_aliases(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        *,
        person_id: UUID,
        slug: str | None,
        name_cn: str,
        monogram: str,
        role_tag: str,
        context_sub: str,
        short_memo: str,
        traits: list[str],
        tier: Tier,
        rank: str,
        state: PersonState,
        visual_tokens: dict[str, Any],
        extra_aliases: Sequence[tuple[str, AliasKind]],
        now: datetime,
    ) -> PersonRow:
        existing = await session.get(PersonRow, person_id)
        if existing is not None:
            raise ConflictError("A person with this ID already exists.")

        row = PersonRow(
            id=person_id,
            user_id=scope.user_id,
            slug=slug,
            tier=tier.value,
            rank=rank,
            state=state.value,
            display_name_enc=self.encrypt_person_str(
                dek,
                name_cn,
                scope=scope,
                person_id=person_id,
                column="display_name_enc",
            ),
            monogram=monogram,
            role_tag_enc=self.encrypt_person_str(
                dek,
                role_tag,
                scope=scope,
                person_id=person_id,
                column="role_tag_enc",
            ),
            context_sub_enc=self.encrypt_person_str(
                dek,
                context_sub,
                scope=scope,
                person_id=person_id,
                column="context_sub_enc",
            ),
            short_memo_enc=self.encrypt_person_str(
                dek,
                short_memo,
                scope=scope,
                person_id=person_id,
                column="short_memo_enc",
            ),
            traits_enc=self.encrypt_traits(
                dek,
                traits,
                scope=scope,
                person_id=person_id,
            ),
            has_photo=False,
            avatar_object_key=None,
            main_photo_object_key=None,
            sub_photo_object_key=None,
            visual_tokens=visual_tokens,
            version=1,
            archived_at=now if state is PersonState.ARCHIVED else None,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()

        seen_hmacs: set[bytes] = set()
        primary_id = new_id()
        primary_hmac = self.compute_alias_hmac(dek, scope=scope, alias=name_cn)
        seen_hmacs.add(primary_hmac)
        session.add(
            PersonAliasRow(
                id=primary_id,
                user_id=scope.user_id,
                person_id=person_id,
                alias_hmac=primary_hmac,
                alias_enc=self.encrypt_alias(
                    dek,
                    name_cn,
                    scope=scope,
                    alias_id=primary_id,
                ),
                kind=AliasKind.NAME.value,
                is_primary=True,
                created_at=now,
            )
        )

        for alias_text, alias_kind in extra_aliases:
            if not normalize_alias_for_index(alias_text):
                continue
            hmac_val = self.compute_alias_hmac(dek, scope=scope, alias=alias_text)
            if hmac_val in seen_hmacs:
                continue
            seen_hmacs.add(hmac_val)
            alias_id = new_id()
            session.add(
                PersonAliasRow(
                    id=alias_id,
                    user_id=scope.user_id,
                    person_id=person_id,
                    alias_hmac=hmac_val,
                    alias_enc=self.encrypt_alias(
                        dek,
                        alias_text,
                        scope=scope,
                        alias_id=alias_id,
                    ),
                    kind=alias_kind.value,
                    is_primary=False,
                    created_at=now,
                )
            )

        await session.flush()
        return row

    async def sync_primary_alias(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        *,
        person_id: UUID,
        new_name_cn: str,
        now: datetime,
    ) -> None:
        """Update the person's `is_primary=True, kind='name'` alias when `nameCn` changes."""
        new_hmac = self.compute_alias_hmac(dek, scope=scope, alias=new_name_cn)
        stmt = (
            select(PersonAliasRow)
            .where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id == person_id,
            )
            .with_for_update()
        )
        rows = list((await session.scalars(stmt)).all())
        primary = next((r for r in rows if r.is_primary), None)
        colliding = next((r for r in rows if r.alias_hmac == new_hmac and not r.is_primary), None)

        if colliding is not None:
            if primary is not None and primary.id != colliding.id:
                await session.delete(primary)
                await session.flush()
            colliding.is_primary = True
            colliding.kind = AliasKind.NAME.value
            colliding.alias_enc = self.encrypt_alias(
                dek,
                new_name_cn,
                scope=scope,
                alias_id=colliding.id,
            )
            await session.flush()
            return

        if primary is not None:
            primary.alias_hmac = new_hmac
            primary.kind = AliasKind.NAME.value
            primary.alias_enc = self.encrypt_alias(
                dek,
                new_name_cn,
                scope=scope,
                alias_id=primary.id,
            )
            await session.flush()
            return

        alias_id = new_id()
        session.add(
            PersonAliasRow(
                id=alias_id,
                user_id=scope.user_id,
                person_id=person_id,
                alias_hmac=new_hmac,
                alias_enc=self.encrypt_alias(
                    dek,
                    new_name_cn,
                    scope=scope,
                    alias_id=alias_id,
                ),
                kind=AliasKind.NAME.value,
                is_primary=True,
                created_at=now,
            )
        )
        await session.flush()

    async def list_alias_rows(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_id: UUID,
    ) -> list[PersonAliasRow]:
        stmt = (
            select(PersonAliasRow)
            .where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id == person_id,
            )
            .order_by(
                PersonAliasRow.is_primary.desc(),
                PersonAliasRow.created_at.asc(),
                PersonAliasRow.id.asc(),
            )
        )
        return list((await session.scalars(stmt)).all())

    async def list_aliases_for_persons(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_ids: Sequence[UUID],
    ) -> dict[UUID, list[PersonAliasRow]]:
        if not person_ids:
            return {}
        stmt = (
            select(PersonAliasRow)
            .where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id.in_(person_ids),
            )
            .order_by(
                PersonAliasRow.is_primary.desc(),
                PersonAliasRow.created_at.asc(),
                PersonAliasRow.id.asc(),
            )
        )
        grouped: dict[UUID, list[PersonAliasRow]] = {pid: [] for pid in person_ids}
        for row in (await session.scalars(stmt)).all():
            grouped.setdefault(row.person_id, []).append(row)
        return grouped

    async def lookup_persons_by_alias_hmac(
        self,
        session: AsyncSession,
        scope: UserScope,
        alias_hmac: bytes,
    ) -> list[PersonRow]:
        """Find all non-deleted, non-archived persons matching `alias_hmac` (supports homonyms!)."""
        stmt = (
            select(PersonRow)
            .join(PersonAliasRow, PersonAliasRow.person_id == PersonRow.id)
            .where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.alias_hmac == alias_hmac,
                PersonRow.user_id == scope.user_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.archived_at.is_(None),
                PersonRow.state != PersonState.ARCHIVED.value,
            )
            .order_by(PersonRow.rank.asc(), PersonRow.id.asc())
        )
        seen: set[UUID] = set()
        result: list[PersonRow] = []
        for row in (await session.scalars(stmt)).all():
            if row.id not in seen:
                seen.add(row.id)
                result.append(row)
        return result

    async def add_alias_row(
        self,
        session: AsyncSession,
        scope: UserScope,
        dek: bytes,
        *,
        person_id: UUID,
        alias_text: str,
        kind: AliasKind,
        now: datetime,
    ) -> PersonAliasRow:
        hmac_val = self.compute_alias_hmac(dek, scope=scope, alias=alias_text)
        dup_stmt = select(PersonAliasRow.id).where(
            PersonAliasRow.user_id == scope.user_id,
            PersonAliasRow.person_id == person_id,
            PersonAliasRow.alias_hmac == hmac_val,
        )
        if (await session.scalar(dup_stmt)) is not None:
            raise ConflictError("Alias already registered for this person.")

        alias_id = new_id()
        row = PersonAliasRow(
            id=alias_id,
            user_id=scope.user_id,
            person_id=person_id,
            alias_hmac=hmac_val,
            alias_enc=self.encrypt_alias(
                dek,
                alias_text,
                scope=scope,
                alias_id=alias_id,
            ),
            kind=kind.value,
            is_primary=False,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def delete_alias_row(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        person_id: UUID,
        alias_id: UUID,
    ) -> None:
        stmt = (
            select(PersonAliasRow)
            .where(
                PersonAliasRow.user_id == scope.user_id,
                PersonAliasRow.person_id == person_id,
                PersonAliasRow.id == alias_id,
            )
            .with_for_update()
        )
        row = await session.scalar(stmt)
        if row is None:
            raise NotFoundError("Alias not found.")
        if row.is_primary:
            raise ConflictError(
                "Primary display-name alias cannot be deleted; update nameCn instead."
            )
        await session.delete(row)
        await session.flush()

    async def set_photo_object_key(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        person_id: UUID,
        slot: PhotoSlot,
        object_key: str,
        now: datetime,
    ) -> tuple[PersonRow, str | None]:
        row = await self.get_person_row(session, scope, person_id, for_update=True)
        old_key: str | None
        if slot is PhotoSlot.AVATAR:
            old_key = row.avatar_object_key
            row.avatar_object_key = object_key
        elif slot is PhotoSlot.MAIN:
            old_key = row.main_photo_object_key
            row.main_photo_object_key = object_key
        else:
            old_key = row.sub_photo_object_key
            row.sub_photo_object_key = object_key

        row.has_photo = True
        row.version += 1
        row.updated_at = now
        await session.flush()
        return row, old_key

    async def delete_person_and_collect_media_keys(
        self,
        session: AsyncSession,
        scope: UserScope,
        person_id: UUID,
    ) -> list[str]:
        """Cascade-delete `PersonRow` and return all associated encrypted blob keys to purge."""
        row = await self.get_person_row(session, scope, person_id, for_update=True)
        keys: list[str] = []
        for candidate in (
            row.avatar_object_key,
            row.main_photo_object_key,
            row.sub_photo_object_key,
        ):
            if candidate:
                keys.append(candidate)

        rec_keys = (
            await session.scalars(
                select(RecordRow.media_object_key).where(
                    RecordRow.user_id == scope.user_id,
                    RecordRow.person_id == person_id,
                    RecordRow.media_object_key.is_not(None),
                )
            )
        ).all()
        for rk in rec_keys:
            if rk and rk not in keys:
                keys.append(rk)

        await session.delete(row)
        await session.flush()
        return keys
