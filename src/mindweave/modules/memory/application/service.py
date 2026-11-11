"""Application service for Materials (`RecordRow`), Facts (`FactRow`), and Pending Inbox (`InboxItemRow`)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher, normalize_alias_for_index
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import CursorPage, strip_forbidden_dev_jargon
from mindweave.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionMismatchError,
)
from mindweave.core.idempotency import IdempotencyHit, hash_request_payload
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    FactRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
)
from mindweave.modules.capture.domain.highlighter import (
    build_natural_slip_tag,
    format_duration_mmss,
)
from mindweave.modules.capture.domain.routing import derive_monogram
from mindweave.modules.memory.application.dto import (
    FactCreateRequest,
    FactDto,
    FactPatchRequest,
    InboxCandidatePersonDto,
    InboxItemDto,
    InboxResolveRequest,
    InboxResolveResponse,
    MaterialCreateRequest,
    MaterialDto,
    MaterialPatchRequest,
)
from mindweave.modules.memory.domain.inbox_policy import (
    InboxDomainError,
    InboxResolutionInput,
    validate_inbox_resolution,
)
from mindweave.modules.memory.infra.image_sanitizer import validate_and_strip_image_exif
from mindweave.modules.memory.infra.reminders_helper import (
    cancel_reminders_for_facts,
    upsert_reminder_for_fact,
)
from mindweave.modules.memory.infra.repository import (
    apply_bitemporal_supersession_for_fact,
    apply_created_cursor_filter,
    build_created_cursor,
    create_person_with_primary_alias,
    decrypt_fact_content,
    decrypt_inbox_payload,
    decrypt_person_display_name,
    decrypt_person_role_tag,
    decrypt_record_fields,
    get_user_timezone,
    merge_provisional_person_into_target,
    next_rank_for_tier,
    require_person_row,
    sync_capture_status_after_inbox_resolution,
)
from mindweave.shared.enums import (
    CreatedBy,
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    InboxResolutionAction,
    PendingReason,
    PersonState,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderStatus,
    SourceSpeaker,
    Tier,
    TimeStatus,
)
from mindweave.shared.scopes import UserScope


def record_row_to_dto(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: RecordRow,
) -> MaterialDto:
    title, body, snippet = decrypt_record_fields(cipher, dek, row)
    has_media = bool(row.media_object_key)
    kind_enum = RecordKind(row.kind)
    return MaterialDto(
        id=row.id,
        person_id=row.person_id,
        capture_id=row.capture_id,
        kind=kind_enum,
        type=kind_enum,
        title=title,
        body=body,
        snippet=snippet,
        has_media=has_media,
        media_url=f"/v1/materials/{row.id}/media" if has_media else None,
        dur=format_duration_mmss(row.audio_duration_ms)
        if row.audio_duration_ms is not None
        else None,
        audio_duration_ms=row.audio_duration_ms,
        stamp_code=row.stamp_code,
        happened_on=row.happened_on,
        pinned=row.pinned,
        review_state=RecordReviewState(row.review_state),
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def fact_row_to_dto(
    cipher: EnvelopeCipher,
    dek: bytes,
    row: FactRow,
) -> FactDto:
    content = decrypt_fact_content(cipher, dek, row)
    return FactDto(
        id=row.id,
        person_id=row.person_id,
        record_id=row.record_id,
        capture_id=row.capture_id,
        type=FactType(row.type),
        polarity=Polarity(row.polarity),
        time_status=TimeStatus(row.time_status),
        source_speaker=SourceSpeaker(row.source_speaker),
        content=content,
        trigger_date=row.trigger_date,
        is_lunar=row.is_lunar,
        lunar_month=row.lunar_month,
        lunar_day=row.lunar_day,
        lunar_is_leap_month=row.lunar_is_leap_month,
        status=FactStatus(row.status),
        pending_reason=PendingReason(row.pending_reason) if row.pending_reason else None,
        confidence=row.confidence,
        created_by=CreatedBy(row.created_by),
        valid_at=row.valid_at,
        invalid_at=row.invalid_at,
        superseded_by_id=row.superseded_by_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _cleanup_unassigned_placeholder_if_empty(
    session: AsyncSession,
    *,
    scope: UserScope,
    person_id: UUID | None,
    now: Any,
) -> None:
    if person_id is None:
        return
    person = await session.scalar(
        select(PersonRow).where(
            PersonRow.id == person_id,
            PersonRow.user_id == scope.user_id,
            PersonRow.slug == "__unassigned__",
            PersonRow.deleted_at.is_(None),
        )
    )
    if person is None:
        return
    remaining = await session.scalar(
        select(func.count(RecordRow.id)).where(
            RecordRow.user_id == scope.user_id,
            RecordRow.person_id == person.id,
            RecordRow.deleted_at.is_(None),
        )
    )
    if int(remaining or 0) == 0:
        person.state = PersonState.ARCHIVED.value
        person.archived_at = now
        person.deleted_at = now
        person.updated_at = now
        await session.flush()


@dataclass(slots=True)
class MemoryService:
    container: AppContainer

    # =========================================================================
    # Materials (RecordRow)
    # =========================================================================

    async def list_person_materials(
        self,
        *,
        scope: UserScope,
        person_id: UUID,
        cursor: str | None = None,
        limit: int = 20,
        include_held: bool = False,
        kind: RecordKind | None = None,
    ) -> CursorPage[MaterialDto]:
        async with self.container.db.user_session(scope) as session:
            await require_person_row(session, scope, person_id)
            dek = await self.container.load_user_dek(session, scope)

            stmt = select(RecordRow).where(
                RecordRow.user_id == scope.user_id,
                RecordRow.person_id == person_id,
                RecordRow.deleted_at.is_(None),
            )
            if not include_held:
                stmt = stmt.where(RecordRow.review_state != RecordReviewState.HELD.value)
            if kind is not None:
                stmt = stmt.where(RecordRow.kind == kind.value)

            stmt = apply_created_cursor_filter(stmt, RecordRow, cursor)
            stmt = stmt.order_by(RecordRow.created_at.desc(), RecordRow.id.desc()).limit(limit + 1)
            rows = list((await session.scalars(stmt)).all())

            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_cursor = (
                build_created_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if has_more and page_rows
                else None
            )
            items = [record_row_to_dto(self.container.cipher, dek, r) for r in page_rows]
            return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def create_material(
        self,
        *,
        scope: UserScope,
        person_id: UUID,
        req: MaterialCreateRequest,
        photo_bytes: bytes | None,
        idem_key: str,
        route_path: str,
    ) -> tuple[MaterialDto | None, IdempotencyHit | None]:
        raw_photo = photo_bytes
        if raw_photo is None and req.photo_base64:
            try:
                raw_photo = base64.b64decode(req.photo_base64, validate=True)
            except Exception as exc:
                raise ValidationFailedError("Invalid base64 photo payload") from exc

        sanitized = None
        if raw_photo is not None:
            sanitized = validate_and_strip_image_exif(
                raw_photo, max_bytes=self.container.settings.max_image_bytes
            )

        req_hash = hash_request_payload(
            {
                "personId": str(person_id),
                "body": req.model_dump(mode="json", exclude={"photo_base64"}),
                "hasPhoto": raw_photo is not None,
                "photoLen": len(raw_photo) if raw_photo is not None else 0,
            }
        )

        async with self.container.db.user_session(scope) as session:
            idem_row, hit = await self.container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=route_path,
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            await require_person_row(session, scope, person_id)
            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            rec_id = req.id or new_id()

            existing_rec = await session.get(RecordRow, rec_id)
            if existing_rec is not None:
                raise ConflictError("Material ID already exists")

            effective_kind = RecordKind.PHOTO if sanitized is not None else req.kind
            object_key: str | None = None
            if sanitized is not None:
                object_key = f"{scope.user_id}/materials/{rec_id}/photo.{sanitized.extension}"
                await self.container.storage.put_encrypted(
                    user_id=scope.user_id,
                    dek=dek,
                    object_key=object_key,
                    plaintext=sanitized.clean_bytes,
                    content_type=sanitized.mime_type,
                )

            clean_title = strip_forbidden_dev_jargon(req.title) or "随手手札"
            clean_body = strip_forbidden_dev_jargon(req.body)
            clean_snippet = strip_forbidden_dev_jargon(req.snippet or clean_body[:60])

            row = RecordRow(
                id=rec_id,
                user_id=scope.user_id,
                person_id=person_id,
                capture_id=None,
                kind=effective_kind.value,
                title_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_title,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=rec_id,
                ),
                body_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_body,
                    user_id=scope.user_id,
                    table="records",
                    column="body_enc",
                    row_id=rec_id,
                ),
                snippet_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_snippet,
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                media_object_key=object_key,
                stamp_code=req.stamp_code,
                happened_on=req.happened_on or now.date(),
                pinned=req.pinned,
                review_state=RecordReviewState.NORMAL.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()

            dto = record_row_to_dto(self.container.cipher, dek, row)
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=201,
                response_body=dto.model_dump(mode="json", by_alias=True),
            )
            return dto, None

    async def get_material(self, *, scope: UserScope, material_id: UUID) -> MaterialDto:
        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(RecordRow).where(
                    RecordRow.id == material_id,
                    RecordRow.user_id == scope.user_id,
                    RecordRow.deleted_at.is_(None),
                )
            )
            if row is None:
                raise NotFoundError("Material not found")
            dek = await self.container.load_user_dek(session, scope)
            return record_row_to_dto(self.container.cipher, dek, row)

    async def patch_material(
        self,
        *,
        scope: UserScope,
        material_id: UUID,
        req: MaterialPatchRequest,
        expected_version: int | None,
    ) -> MaterialDto:
        if expected_version is None:
            raise BadRequestError("If-Match header is required for optimistic locking")

        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(RecordRow)
                .where(
                    RecordRow.id == material_id,
                    RecordRow.user_id == scope.user_id,
                    RecordRow.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise NotFoundError("Material not found")
            if row.version != expected_version:
                raise VersionMismatchError(
                    f"Expected version {expected_version}, current version is {row.version}."
                )

            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()

            if req.title is not None:
                clean_title = strip_forbidden_dev_jargon(req.title) or "随手手札"
                row.title_enc = self.container.cipher.encrypt_str(
                    dek,
                    clean_title,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=row.id,
                )
            if req.body is not None:
                clean_body = strip_forbidden_dev_jargon(req.body)
                row.body_enc = self.container.cipher.encrypt_str(
                    dek,
                    clean_body,
                    user_id=scope.user_id,
                    table="records",
                    column="body_enc",
                    row_id=row.id,
                )
                if req.snippet is None:
                    row.snippet_enc = self.container.cipher.encrypt_str(
                        dek,
                        clean_body[:60],
                        user_id=scope.user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=row.id,
                    )
            if req.snippet is not None:
                clean_snippet = strip_forbidden_dev_jargon(req.snippet)
                row.snippet_enc = self.container.cipher.encrypt_str(
                    dek,
                    clean_snippet,
                    user_id=scope.user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=row.id,
                )
            if req.stamp_code is not None:
                row.stamp_code = req.stamp_code
            if req.happened_on is not None:
                row.happened_on = req.happened_on
            if req.pinned is not None:
                row.pinned = req.pinned
            if req.review_state is not None:
                row.review_state = req.review_state.value

            row.version += 1
            row.updated_at = now
            await session.flush()
            return record_row_to_dto(self.container.cipher, dek, row)

    async def delete_material(self, *, scope: UserScope, material_id: UUID) -> None:
        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(RecordRow)
                .where(
                    RecordRow.id == material_id,
                    RecordRow.user_id == scope.user_id,
                    RecordRow.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                raise NotFoundError("Material not found")
            now = self.container.clock.now()
            row.deleted_at = now
            row.updated_at = now
            row.version += 1
            await session.flush()

    async def get_material_media(
        self,
        *,
        scope: UserScope,
        material_id: UUID,
    ) -> tuple[bytes, str]:
        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(RecordRow).where(
                    RecordRow.id == material_id,
                    RecordRow.user_id == scope.user_id,
                    RecordRow.deleted_at.is_(None),
                )
            )
            if row is None or not row.media_object_key:
                raise NotFoundError("Material media not found")
            dek = await self.container.load_user_dek(session, scope)
            object_key = row.media_object_key

        return await self.container.storage.get_decrypted(
            user_id=scope.user_id,
            dek=dek,
            object_key=object_key,
        )

    # =========================================================================
    # Facts (FactRow)
    # =========================================================================

    async def list_person_facts(
        self,
        *,
        scope: UserScope,
        person_id: UUID,
        status: FactStatus | None = None,
        fact_type: FactType | None = None,
        include_invalidated: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CursorPage[FactDto]:
        async with self.container.db.user_session(scope) as session:
            await require_person_row(session, scope, person_id)
            dek = await self.container.load_user_dek(session, scope)

            stmt = select(FactRow).where(
                FactRow.user_id == scope.user_id,
                FactRow.person_id == person_id,
            )
            if status is not None:
                stmt = stmt.where(FactRow.status == status.value)
            if fact_type is not None:
                stmt = stmt.where(FactRow.type == fact_type.value)
            if not include_invalidated and status != FactStatus.REJECTED:
                stmt = stmt.where(FactRow.invalid_at.is_(None))

            stmt = apply_created_cursor_filter(stmt, FactRow, cursor)
            stmt = stmt.order_by(FactRow.created_at.desc(), FactRow.id.desc()).limit(limit + 1)
            rows = list((await session.scalars(stmt)).all())

            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_cursor = (
                build_created_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if has_more and page_rows
                else None
            )
            items = [fact_row_to_dto(self.container.cipher, dek, r) for r in page_rows]
            return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def create_fact(
        self,
        *,
        scope: UserScope,
        person_id: UUID,
        req: FactCreateRequest,
        idem_key: str,
        route_path: str,
    ) -> tuple[FactDto | None, IdempotencyHit | None]:
        req_hash = hash_request_payload(
            {"personId": str(person_id), "body": req.model_dump(mode="json")}
        )
        async with self.container.db.user_session(scope) as session:
            idem_row, hit = await self.container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=route_path,
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            await require_person_row(session, scope, person_id)
            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)

            clean_content = strip_forbidden_dev_jargon(req.content)
            if not clean_content:
                raise ValidationFailedError("Fact content cannot be empty")

            record_id = req.record_id
            if record_id is not None:
                linked_rec = await session.scalar(
                    select(RecordRow).where(
                        RecordRow.id == record_id,
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == person_id,
                        RecordRow.deleted_at.is_(None),
                    )
                )
                if linked_rec is None:
                    raise NotFoundError("Referenced material not found for this person")
            else:
                # Ensure every confirmed fact has a verifiable backing RecordRow for Invariant I4 citations
                record_id = new_id()
                slip_title = build_natural_slip_tag("", clean_content)
                session.add(
                    RecordRow(
                        id=record_id,
                        user_id=scope.user_id,
                        person_id=person_id,
                        capture_id=None,
                        kind=RecordKind.NOTE.value,
                        title_enc=self.container.cipher.encrypt_str(
                            dek,
                            slip_title,
                            user_id=scope.user_id,
                            table="records",
                            column="title_enc",
                            row_id=record_id,
                        ),
                        body_enc=self.container.cipher.encrypt_str(
                            dek,
                            clean_content,
                            user_id=scope.user_id,
                            table="records",
                            column="body_enc",
                            row_id=record_id,
                        ),
                        snippet_enc=self.container.cipher.encrypt_str(
                            dek,
                            clean_content[:60],
                            user_id=scope.user_id,
                            table="records",
                            column="snippet_enc",
                            row_id=record_id,
                        ),
                        happened_on=now.date(),
                        pinned=True,
                        review_state=RecordReviewState.NORMAL.value,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()

            fact_id = req.id or new_id()
            if await session.get(FactRow, fact_id) is not None:
                raise ConflictError("Fact ID already exists")

            fact_row = FactRow(
                id=fact_id,
                user_id=scope.user_id,
                person_id=person_id,
                record_id=record_id,
                capture_id=None,
                type=req.type.value,
                polarity=req.polarity.value,
                time_status=TimeStatus.CURRENT.value,
                source_speaker=SourceSpeaker.NARRATOR.value,
                content_enc=self.container.cipher.encrypt_str(
                    dek,
                    clean_content,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fact_id,
                ),
                trigger_date=req.trigger_date,
                is_lunar=req.is_lunar,
                lunar_month=req.lunar_month,
                lunar_day=req.lunar_day,
                lunar_is_leap_month=req.lunar_is_leap_month,
                status=FactStatus.CONFIRMED.value,
                pending_reason=None,
                confidence=1.0,
                created_by=CreatedBy.USER.value,
                valid_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(fact_row)
            await session.flush()

            await apply_bitemporal_supersession_for_fact(
                session,
                scope=scope,
                dek=dek,
                cipher=self.container.cipher,
                new_fact=fact_row,
                new_fact_content=clean_content,
                now=now,
                explicit_supersedes_id=req.supersedes_fact_id,
            )

            if req.type in (FactType.DATE, FactType.PROMISE):
                await upsert_reminder_for_fact(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    fact=fact_row,
                    fact_content=clean_content,
                    desired_status=ReminderStatus.ACTIVE,
                    now=now,
                    tz_name=tz_name,
                )

            dto = fact_row_to_dto(self.container.cipher, dek, fact_row)
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=201,
                response_body=dto.model_dump(mode="json", by_alias=True),
            )
            return dto, None

    async def patch_fact(
        self,
        *,
        scope: UserScope,
        fact_id: UUID,
        req: FactPatchRequest,
        expected_version: int | None,
    ) -> FactDto:
        if expected_version is None:
            raise BadRequestError("If-Match header is required for optimistic locking")

        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(FactRow)
                .where(
                    FactRow.id == fact_id,
                    FactRow.user_id == scope.user_id,
                )
                .with_for_update()
            )
            if row is None:
                raise NotFoundError("Fact not found")
            if row.version != expected_version:
                raise VersionMismatchError(
                    f"Expected version {expected_version}, current version is {row.version}."
                )

            dek = await self.container.load_user_dek(session, scope)
            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)

            current_content = decrypt_fact_content(self.container.cipher, dek, row)
            if req.content is not None:
                current_content = strip_forbidden_dev_jargon(req.content)
                row.content_enc = self.container.cipher.encrypt_str(
                    dek,
                    current_content,
                    user_id=scope.user_id,
                    table="facts",
                    column="content_enc",
                    row_id=row.id,
                )
            if req.type is not None:
                row.type = req.type.value
            if req.polarity is not None:
                row.polarity = req.polarity.value
            if req.time_status is not None:
                row.time_status = req.time_status.value
            if req.trigger_date is not None:
                row.trigger_date = req.trigger_date
            if req.is_lunar is not None:
                row.is_lunar = req.is_lunar
            if req.lunar_month is not None:
                row.lunar_month = req.lunar_month
            if req.lunar_day is not None:
                row.lunar_day = req.lunar_day
            if req.lunar_is_leap_month is not None:
                row.lunar_is_leap_month = req.lunar_is_leap_month
            if req.status is not None:
                row.status = req.status.value
                if req.status is FactStatus.CONFIRMED:
                    row.pending_reason = None
                    row.invalid_at = None
                elif req.status is FactStatus.REJECTED:
                    row.invalid_at = now

            row.version += 1
            row.updated_at = now
            await session.flush()

            if row.status == FactStatus.CONFIRMED.value and row.person_id is not None:
                await apply_bitemporal_supersession_for_fact(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    new_fact=row,
                    new_fact_content=current_content,
                    now=now,
                )
                if row.type in (FactType.DATE.value, FactType.PROMISE.value):
                    await upsert_reminder_for_fact(
                        session,
                        scope=scope,
                        dek=dek,
                        cipher=self.container.cipher,
                        fact=row,
                        fact_content=current_content,
                        desired_status=ReminderStatus.ACTIVE,
                        now=now,
                        tz_name=tz_name,
                    )
            elif row.status == FactStatus.REJECTED.value:
                await cancel_reminders_for_facts(
                    session,
                    user_id=scope.user_id,
                    fact_ids=[row.id],
                    now=now,
                )

            return fact_row_to_dto(self.container.cipher, dek, row)

    async def delete_fact(self, *, scope: UserScope, fact_id: UUID) -> None:
        async with self.container.db.user_session(scope) as session:
            row = await session.scalar(
                select(FactRow)
                .where(
                    FactRow.id == fact_id,
                    FactRow.user_id == scope.user_id,
                )
                .with_for_update()
            )
            if row is None:
                raise NotFoundError("Fact not found")
            now = self.container.clock.now()
            row.status = FactStatus.REJECTED.value
            row.invalid_at = now
            row.updated_at = now
            row.version += 1
            await cancel_reminders_for_facts(
                session,
                user_id=scope.user_id,
                fact_ids=[row.id],
                now=now,
            )
            await session.flush()

    # =========================================================================
    # Pending Inbox (InboxItemRow)
    # =========================================================================

    async def _hydrate_inbox_item(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        dek: bytes,
        row: InboxItemRow,
    ) -> InboxItemDto:
        payload = decrypt_inbox_payload(self.container.cipher, dek, row)
        summary = strip_forbidden_dev_jargon(str(payload.get("summary") or ""))
        mention_val = payload.get("mention")
        mention = str(mention_val) if isinstance(mention_val, str) and mention_val else None

        person_name: str | None = None
        if row.person_id is not None:
            person_row = await session.scalar(
                select(PersonRow).where(
                    PersonRow.id == row.person_id,
                    PersonRow.user_id == scope.user_id,
                )
            )
            if person_row is not None and person_row.slug != "__unassigned__":
                person_name = decrypt_person_display_name(self.container.cipher, dek, person_row)

        fact_dtos: list[FactDto] = []
        if row.fact_ids:
            fact_rows = (
                await session.scalars(
                    select(FactRow).where(
                        FactRow.user_id == scope.user_id,
                        FactRow.id.in_(list(row.fact_ids)),
                    )
                )
            ).all()
            by_id = {f.id: f for f in fact_rows}
            for fid in row.fact_ids:
                if fid in by_id:
                    fact_dtos.append(fact_row_to_dto(self.container.cipher, dek, by_id[fid]))

        candidate_dtos: list[InboxCandidatePersonDto] = []
        if row.candidate_person_ids:
            cand_rows = (
                await session.scalars(
                    select(PersonRow).where(
                        PersonRow.user_id == scope.user_id,
                        PersonRow.id.in_(list(row.candidate_person_ids)),
                        PersonRow.deleted_at.is_(None),
                    )
                )
            ).all()
            cand_by_id = {p.id: p for p in cand_rows if p.slug != "__unassigned__"}
            for cid in row.candidate_person_ids:
                p = cand_by_id.get(cid)
                if p is not None:
                    candidate_dtos.append(
                        InboxCandidatePersonDto(
                            person_id=p.id,
                            display_name=decrypt_person_display_name(self.container.cipher, dek, p),
                            monogram=p.monogram,
                            tier=Tier(p.tier),
                            role_tag=decrypt_person_role_tag(self.container.cipher, dek, p),
                            state=PersonState(p.state),
                        )
                    )

        return InboxItemDto(
            id=row.id,
            kind=InboxItemKind(row.kind),
            status=InboxItemStatus(row.status),
            person_id=row.person_id if person_name is not None else None,
            person_name=person_name,
            capture_id=row.capture_id,
            record_id=row.record_id,
            fact_ids=list(row.fact_ids),
            facts=fact_dtos,
            candidate_persons=candidate_dtos,
            summary=summary,
            mention=mention,
            resolution_action=InboxResolutionAction(row.resolution_action)
            if row.resolution_action
            else None,
            expires_at=row.expires_at,
            resolved_at=row.resolved_at,
            created_at=row.created_at,
        )

    async def list_inbox(
        self,
        *,
        scope: UserScope,
        status: InboxItemStatus = InboxItemStatus.OPEN,
        person_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CursorPage[InboxItemDto]:
        async with self.container.db.user_session(scope) as session:
            dek = await self.container.load_user_dek(session, scope)
            stmt = select(InboxItemRow).where(
                InboxItemRow.user_id == scope.user_id,
                InboxItemRow.status == status.value,
            )
            if person_id is not None:
                stmt = stmt.where(InboxItemRow.person_id == person_id)

            stmt = apply_created_cursor_filter(stmt, InboxItemRow, cursor)
            stmt = stmt.order_by(InboxItemRow.created_at.desc(), InboxItemRow.id.desc()).limit(
                limit + 1
            )
            rows = list((await session.scalars(stmt)).all())

            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_cursor = (
                build_created_cursor(page_rows[-1].created_at, page_rows[-1].id)
                if has_more and page_rows
                else None
            )
            items = [
                await self._hydrate_inbox_item(session, scope=scope, dek=dek, row=r)
                for r in page_rows
            ]
            return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def resolve_inbox_item(
        self,
        *,
        scope: UserScope,
        item_id: UUID,
        req: InboxResolveRequest,
        idem_key: str,
        route_path: str,
    ) -> tuple[InboxResolveResponse | None, IdempotencyHit | None]:
        req_hash = hash_request_payload(
            {"itemId": str(item_id), "body": req.model_dump(mode="json")}
        )
        async with self.container.db.user_session(scope) as session:
            idem_row, hit = await self.container.idempotency.claim_or_replay(
                session,
                scope=scope,
                idem_key=idem_key,
                method="POST",
                path=route_path,
                request_hash=req_hash,
            )
            if hit is not None:
                return None, hit

            item = await session.scalar(
                select(InboxItemRow)
                .where(
                    InboxItemRow.id == item_id,
                    InboxItemRow.user_id == scope.user_id,
                )
                .with_for_update()
            )
            if item is None:
                raise NotFoundError("Inbox item not found")

            dek = await self.container.load_user_dek(session, scope)
            payload = decrypt_inbox_payload(self.container.cipher, dek, item)
            fallback_mention = (
                str(payload.get("mention")) if payload.get("mention") is not None else None
            )

            # Check if item.person_id is the internal __unassigned__ placeholder
            current_person_row: PersonRow | None = None
            effective_current_person_id = item.person_id
            if item.person_id is not None:
                current_person_row = await session.scalar(
                    select(PersonRow).where(
                        PersonRow.id == item.person_id,
                        PersonRow.user_id == scope.user_id,
                    )
                )
                if current_person_row is not None and current_person_row.slug == "__unassigned__":
                    effective_current_person_id = None

            try:
                validate_inbox_resolution(
                    InboxResolutionInput(
                        item_kind=InboxItemKind(item.kind),
                        item_status=InboxItemStatus(item.status),
                        action=req.action,
                        current_person_id=effective_current_person_id,
                        target_person_id=req.target_person_id,
                        display_name=req.display_name,
                        fallback_mention=fallback_mention,
                    )
                )
            except InboxDomainError as exc:
                if exc.code == "inbox_not_open":
                    raise ConflictError(str(exc), error_code=exc.code) from exc
                raise BadRequestError(str(exc), error_code=exc.code) from exc

            now = self.container.clock.now()
            tz_name = await get_user_timezone(session, scope)

            facts = (
                list(
                    (
                        await session.scalars(
                            select(FactRow)
                            .where(
                                FactRow.user_id == scope.user_id,
                                FactRow.id.in_(list(item.fact_ids)),
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                if item.fact_ids
                else []
            )

            linked_record: RecordRow | None = None
            if item.record_id is not None:
                linked_record = await session.scalar(
                    select(RecordRow)
                    .where(
                        RecordRow.id == item.record_id,
                        RecordRow.user_id == scope.user_id,
                    )
                    .with_for_update()
                )

            resolved_fact_ids = [f.id for f in facts]
            superseded_ids: list[UUID] = []
            final_person_id: UUID | None = effective_current_person_id

            # -----------------------------------------------------------------
            # Execute one of the 6 resolution actions
            # -----------------------------------------------------------------
            if req.action is InboxResolutionAction.KEEP_AS_NOTE:
                if linked_record is not None and linked_record.deleted_at is None:
                    linked_record.review_state = RecordReviewState.NOTE_ONLY.value
                    linked_record.updated_at = now
                    linked_record.version += 1
                for fact in facts:
                    fact.status = FactStatus.REJECTED.value
                    fact.invalid_at = now
                    fact.updated_at = now
                    fact.version += 1
                await cancel_reminders_for_facts(
                    session, user_id=scope.user_id, fact_ids=resolved_fact_ids, now=now
                )

            elif req.action is InboxResolutionAction.DISCARD:
                old_rec_person_id = linked_record.person_id if linked_record is not None else None
                if linked_record is not None and linked_record.deleted_at is None:
                    linked_record.deleted_at = now
                    linked_record.updated_at = now
                    linked_record.version += 1
                for fact in facts:
                    fact.status = FactStatus.REJECTED.value
                    fact.invalid_at = now
                    fact.updated_at = now
                    fact.version += 1
                await cancel_reminders_for_facts(
                    session, user_id=scope.user_id, fact_ids=resolved_fact_ids, now=now
                )
                # If new_person_confirm was discarded and provisional person has no other active records, archive it
                if (
                    item.kind == InboxItemKind.NEW_PERSON_CONFIRM.value
                    and current_person_row is not None
                    and current_person_row.state == PersonState.PROVISIONAL.value
                ):
                    rem_count = await session.scalar(
                        select(func.count(RecordRow.id)).where(
                            RecordRow.user_id == scope.user_id,
                            RecordRow.person_id == current_person_row.id,
                            RecordRow.deleted_at.is_(None),
                        )
                    )
                    if int(rem_count or 0) == 0:
                        current_person_row.state = PersonState.ARCHIVED.value
                        current_person_row.archived_at = now
                        current_person_row.deleted_at = now
                        current_person_row.updated_at = now
                await _cleanup_unassigned_placeholder_if_empty(
                    session, scope=scope, person_id=old_rec_person_id, now=now
                )

            elif req.action is InboxResolutionAction.CONFIRM:
                target_pid = effective_current_person_id or req.target_person_id
                assert target_pid is not None
                target_person = await require_person_row(session, scope, target_pid)
                if target_person.state == PersonState.PROVISIONAL.value:
                    target_person.state = PersonState.ACTIVE.value
                    target_person.updated_at = now
                    target_person.version += 1
                final_person_id = target_person.id

                ensured_rec = await self._ensure_normal_record_for_target(
                    session,
                    scope=scope,
                    dek=dek,
                    linked_record=linked_record,
                    target_person_id=target_person.id,
                    capture_id=item.capture_id,
                    facts=facts,
                    fallback_summary=str(payload.get("summary") or ""),
                    now=now,
                )
                item.record_id = ensured_rec.id
                item.person_id = target_person.id
                superseded_ids = await self._confirm_facts_and_reminders(
                    session,
                    scope=scope,
                    dek=dek,
                    facts=facts,
                    target_person_id=target_person.id,
                    record_id=ensured_rec.id,
                    override_trigger_date=req.trigger_date,
                    tz_name=tz_name,
                    now=now,
                )

            elif req.action is InboxResolutionAction.ASSIGN_PERSON:
                assert req.target_person_id is not None
                target_person = await require_person_row(session, scope, req.target_person_id)
                if target_person.state == PersonState.PROVISIONAL.value:
                    target_person.state = PersonState.ACTIVE.value
                    target_person.updated_at = now
                    target_person.version += 1
                final_person_id = target_person.id

                prev_rec_person_id = linked_record.person_id if linked_record is not None else None
                ensured_rec = await self._ensure_normal_record_for_target(
                    session,
                    scope=scope,
                    dek=dek,
                    linked_record=linked_record,
                    target_person_id=target_person.id,
                    capture_id=item.capture_id,
                    facts=facts,
                    fallback_summary=str(payload.get("summary") or ""),
                    now=now,
                )
                item.person_id = target_person.id
                item.record_id = ensured_rec.id

                superseded_ids = await self._confirm_facts_and_reminders(
                    session,
                    scope=scope,
                    dek=dek,
                    facts=facts,
                    target_person_id=target_person.id,
                    record_id=ensured_rec.id,
                    override_trigger_date=req.trigger_date,
                    tz_name=tz_name,
                    now=now,
                )
                await _cleanup_unassigned_placeholder_if_empty(
                    session, scope=scope, person_id=prev_rec_person_id, now=now
                )

            elif req.action is InboxResolutionAction.CREATE_PERSON:
                desired_tier = req.tier or Tier.CRYSTAL
                if (
                    current_person_row is not None
                    and current_person_row.slug != "__unassigned__"
                    and current_person_row.deleted_at is None
                ):
                    # Promote existing provisional person to active
                    current_person_row.state = PersonState.ACTIVE.value
                    if req.tier is not None and current_person_row.tier != req.tier.value:
                        current_person_row.tier = req.tier.value
                        current_person_row.rank = await next_rank_for_tier(session, scope, req.tier)
                    if req.display_name and req.display_name.strip():
                        clean_name = strip_forbidden_dev_jargon(req.display_name.strip())
                        current_person_row.display_name_enc = self.container.cipher.encrypt_str(
                            dek,
                            clean_name,
                            user_id=scope.user_id,
                            table="persons",
                            column="display_name_enc",
                            row_id=current_person_row.id,
                        )
                        current_person_row.monogram = derive_monogram(clean_name)
                        norm = normalize_alias_for_index(clean_name)
                        if norm:
                            hmac_val = self.container.cipher.compute_blind_index(
                                dek,
                                user_id=scope.user_id,
                                domain="alias",
                                normalized_value=norm,
                            )
                            existing_alias = await session.scalar(
                                select(PersonAliasRow).where(
                                    PersonAliasRow.user_id == scope.user_id,
                                    PersonAliasRow.person_id == current_person_row.id,
                                    PersonAliasRow.alias_hmac == hmac_val,
                                )
                            )
                            if existing_alias is None:
                                aid = new_id()
                                session.add(
                                    PersonAliasRow(
                                        id=aid,
                                        user_id=scope.user_id,
                                        person_id=current_person_row.id,
                                        alias_hmac=hmac_val,
                                        alias_enc=self.container.cipher.encrypt_str(
                                            dek,
                                            clean_name,
                                            user_id=scope.user_id,
                                            table="person_aliases",
                                            column="alias_enc",
                                            row_id=aid,
                                        ),
                                        kind="name",
                                        is_primary=True,
                                        created_at=now,
                                    )
                                )
                    current_person_row.updated_at = now
                    current_person_row.version += 1
                    promoted_person = current_person_row
                else:
                    name_to_create = (req.display_name or fallback_mention or "").strip()
                    promoted_person = await create_person_with_primary_alias(
                        session,
                        scope=scope,
                        dek=dek,
                        cipher=self.container.cipher,
                        display_name=name_to_create,
                        tier=desired_tier,
                        state=PersonState.ACTIVE,
                        now=now,
                    )

                final_person_id = promoted_person.id
                prev_rec_person_id = linked_record.person_id if linked_record is not None else None
                ensured_rec = await self._ensure_normal_record_for_target(
                    session,
                    scope=scope,
                    dek=dek,
                    linked_record=linked_record,
                    target_person_id=promoted_person.id,
                    capture_id=item.capture_id,
                    facts=facts,
                    fallback_summary=str(payload.get("summary") or ""),
                    now=now,
                )
                item.person_id = promoted_person.id
                item.record_id = ensured_rec.id

                superseded_ids = await self._confirm_facts_and_reminders(
                    session,
                    scope=scope,
                    dek=dek,
                    facts=facts,
                    target_person_id=promoted_person.id,
                    record_id=ensured_rec.id,
                    override_trigger_date=req.trigger_date,
                    tz_name=tz_name,
                    now=now,
                )
                await _cleanup_unassigned_placeholder_if_empty(
                    session, scope=scope, person_id=prev_rec_person_id, now=now
                )

            elif req.action is InboxResolutionAction.MERGE_PERSON:
                assert req.target_person_id is not None
                if current_person_row is None or current_person_row.deleted_at is not None:
                    raise NotFoundError("Provisional source person not found for merge")
                target_person = await require_person_row(session, scope, req.target_person_id)
                if target_person.state == PersonState.PROVISIONAL.value:
                    target_person.state = PersonState.ACTIVE.value
                    target_person.updated_at = now
                    target_person.version += 1

                await merge_provisional_person_into_target(
                    session,
                    scope=scope,
                    source_person=current_person_row,
                    target_person=target_person,
                    current_inbox_id=item.id,
                    now=now,
                )
                final_person_id = target_person.id
                item.person_id = target_person.id

                ensured_rec = await self._ensure_normal_record_for_target(
                    session,
                    scope=scope,
                    dek=dek,
                    linked_record=linked_record,
                    target_person_id=target_person.id,
                    capture_id=item.capture_id,
                    facts=facts,
                    fallback_summary=str(payload.get("summary") or ""),
                    now=now,
                )
                item.record_id = ensured_rec.id
                superseded_ids = await self._confirm_facts_and_reminders(
                    session,
                    scope=scope,
                    dek=dek,
                    facts=facts,
                    target_person_id=target_person.id,
                    record_id=ensured_rec.id,
                    override_trigger_date=req.trigger_date,
                    tz_name=tz_name,
                    now=now,
                )

            item.status = InboxItemStatus.RESOLVED.value
            item.resolution_action = req.action.value
            item.resolved_at = now
            await session.flush()

            await sync_capture_status_after_inbox_resolution(
                session,
                scope=scope,
                capture_id=item.capture_id,
                now=now,
            )

            hydrated = await self._hydrate_inbox_item(session, scope=scope, dek=dek, row=item)
            resp = InboxResolveResponse(
                item=hydrated,
                resolved_fact_ids=resolved_fact_ids,
                superseded_fact_ids=superseded_ids,
                target_person_id=final_person_id,
            )
            assert idem_row is not None
            await self.container.idempotency.complete(
                session,
                idem_row,
                status_code=200,
                response_body=resp.model_dump(mode="json", by_alias=True),
            )
            return resp, None

    async def _ensure_normal_record_for_target(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        dek: bytes,
        linked_record: RecordRow | None,
        target_person_id: UUID,
        capture_id: UUID | None,
        facts: list[FactRow],
        fallback_summary: str,
        now: Any,
    ) -> RecordRow:
        if linked_record is not None and linked_record.deleted_at is None:
            linked_record.person_id = target_person_id
            linked_record.review_state = RecordReviewState.NORMAL.value
            linked_record.updated_at = now
            linked_record.version += 1
            await session.flush()
            return linked_record

        contents = [decrypt_fact_content(self.container.cipher, dek, f) for f in facts]
        body_text = "；".join(c for c in contents if c) or fallback_summary or "已确认记录"
        body_text = strip_forbidden_dev_jargon(body_text) or "已确认记录"
        title_text = build_natural_slip_tag("", body_text)
        rec_id = new_id()
        rec = RecordRow(
            id=rec_id,
            user_id=scope.user_id,
            person_id=target_person_id,
            capture_id=capture_id,
            kind=RecordKind.NOTE.value,
            title_enc=self.container.cipher.encrypt_str(
                dek,
                title_text,
                user_id=scope.user_id,
                table="records",
                column="title_enc",
                row_id=rec_id,
            ),
            body_enc=self.container.cipher.encrypt_str(
                dek,
                body_text,
                user_id=scope.user_id,
                table="records",
                column="body_enc",
                row_id=rec_id,
            ),
            snippet_enc=self.container.cipher.encrypt_str(
                dek,
                body_text[:60],
                user_id=scope.user_id,
                table="records",
                column="snippet_enc",
                row_id=rec_id,
            ),
            happened_on=now.date(),
            pinned=True,
            review_state=RecordReviewState.NORMAL.value,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(rec)
        await session.flush()
        return rec

    async def _confirm_facts_and_reminders(
        self,
        session: AsyncSession,
        *,
        scope: UserScope,
        dek: bytes,
        facts: list[FactRow],
        target_person_id: UUID,
        record_id: UUID,
        override_trigger_date: Any,
        tz_name: str,
        now: Any,
    ) -> list[UUID]:
        all_superseded: list[UUID] = []
        for fact in facts:
            fact.person_id = target_person_id
            if fact.record_id is None:
                fact.record_id = record_id
            if override_trigger_date is not None and fact.type in (
                FactType.DATE.value,
                FactType.PROMISE.value,
            ):
                fact.trigger_date = override_trigger_date
            fact.status = FactStatus.CONFIRMED.value
            fact.pending_reason = None
            fact.invalid_at = None
            fact.valid_at = now
            fact.updated_at = now
            fact.version += 1
            await session.flush()

            content = decrypt_fact_content(self.container.cipher, dek, fact)
            sup_ids = await apply_bitemporal_supersession_for_fact(
                session,
                scope=scope,
                dek=dek,
                cipher=self.container.cipher,
                new_fact=fact,
                new_fact_content=content,
                now=now,
            )
            all_superseded.extend(sup_ids)

            if fact.type in (FactType.DATE.value, FactType.PROMISE.value):
                await upsert_reminder_for_fact(
                    session,
                    scope=scope,
                    dek=dek,
                    cipher=self.container.cipher,
                    fact=fact,
                    fact_content=content,
                    desired_status=ReminderStatus.ACTIVE,
                    now=now,
                    tz_name=tz_name,
                )
        return all_superseded
