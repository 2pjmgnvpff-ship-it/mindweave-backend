"""Encrypted SQLAlchemy repository for Companion chat threads, messages, and citations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.dto import decode_cursor, encode_cursor
from mindweave.core.errors import NotFoundError, ValidationFailedError
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    ChatCitationRow,
    ChatMessageRow,
    ChatThreadRow,
    PersonRow,
    RecordRow,
)
from mindweave.modules.companion.domain.citation_processor import VerifiedCitation
from mindweave.modules.companion.domain.output_guard import sanitize_text
from mindweave.shared.enums import ChatRole, RecordReviewState, SafetyCategory, Tier
from mindweave.shared.scopes import PersonScope


@dataclass(frozen=True, slots=True)
class HydratedCitation:
    index: int
    material_id: UUID
    title: str
    snippet: str
    happened_on: str | None = None


@dataclass(frozen=True, slots=True)
class HydratedChatMessage:
    id: UUID
    person_id: UUID
    thread_id: UUID
    role: ChatRole
    content: str
    whisper_note: str | None
    perspectives: list[str]
    suggestions: list[str]
    citations: list[HydratedCitation]
    referenced_material_ids: list[UUID]
    tier_snapshot: Tier
    safety_category: SafetyCategory
    has_unrecorded_gap: bool
    client_message_id: str | None
    created_at: datetime


class CompanionRepository:
    """Tenant- and person-isolated repository for `chat_threads`, `chat_messages`, `chat_citations`."""

    def __init__(self, cipher: EnvelopeCipher) -> None:
        self._cipher = cipher

    async def verify_person(self, session: AsyncSession, scope: PersonScope) -> PersonRow:
        person = await session.scalar(
            select(PersonRow).where(
                PersonRow.user_id == scope.user_id,
                PersonRow.id == scope.person_id,
                PersonRow.deleted_at.is_(None),
            )
        )
        if person is None:
            raise NotFoundError("Person not found")
        return person

    async def get_or_create_thread_for_update(
        self,
        session: AsyncSession,
        scope: PersonScope,
        *,
        now: datetime,
    ) -> ChatThreadRow:
        candidate_id = new_id()
        stmt = (
            pg_insert(ChatThreadRow)
            .values(
                id=candidate_id,
                user_id=scope.user_id,
                person_id=scope.person_id,
                message_count=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "person_id"])
        )
        await session.execute(stmt)

        thread = await session.scalar(
            select(ChatThreadRow)
            .where(
                ChatThreadRow.user_id == scope.user_id,
                ChatThreadRow.person_id == scope.person_id,
            )
            .with_for_update()
        )
        if thread is None:
            raise NotFoundError("Chat thread could not be initialized")
        return thread

    async def insert_message(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        dek: bytes,
        thread_id: UUID,
        role: ChatRole,
        content: str,
        tier_snapshot: Tier,
        now: datetime,
        message_id: UUID | None = None,
        whisper_note: str | None = None,
        perspectives: Sequence[str] = (),
        suggestions: Sequence[str] = (),
        referenced_record_ids: Sequence[UUID] = (),
        safety_category: SafetyCategory = SafetyCategory.NORMAL,
        has_unrecorded_gap: bool = False,
        prompt_version: str | None = None,
        client_message_id: str | None = None,
    ) -> ChatMessageRow:
        msg_id = message_id or new_id()

        content_enc = self._cipher.encrypt_str(
            dek,
            content,
            user_id=scope.user_id,
            table="chat_messages",
            column="content_enc",
            row_id=msg_id,
        )
        whisper_enc = (
            self._cipher.encrypt_str(
                dek,
                whisper_note,
                user_id=scope.user_id,
                table="chat_messages",
                column="whisper_enc",
                row_id=msg_id,
            )
            if whisper_note
            else None
        )
        effective_perspectives = list(perspectives) if tier_snapshot is Tier.GOLD else []
        perspectives_enc = (
            self._cipher.encrypt_json(
                dek,
                effective_perspectives,
                user_id=scope.user_id,
                table="chat_messages",
                column="perspectives_enc",
                row_id=msg_id,
            )
            if effective_perspectives
            else None
        )
        suggestions_list = list(suggestions)
        suggestions_enc = (
            self._cipher.encrypt_json(
                dek,
                suggestions_list,
                user_id=scope.user_id,
                table="chat_messages",
                column="suggestions_enc",
                row_id=msg_id,
            )
            if suggestions_list
            else None
        )

        row = ChatMessageRow(
            id=msg_id,
            user_id=scope.user_id,
            thread_id=thread_id,
            person_id=scope.person_id,
            role=role.value,
            content_enc=content_enc,
            whisper_enc=whisper_enc,
            perspectives_enc=perspectives_enc,
            suggestions_enc=suggestions_enc,
            referenced_record_ids=list(referenced_record_ids),
            tier_snapshot=tier_snapshot.value,
            safety_category=safety_category.value,
            has_unrecorded_gap=has_unrecorded_gap,
            prompt_version=prompt_version,
            client_message_id=client_message_id,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def insert_citations(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        message_id: UUID,
        citations: Sequence[VerifiedCitation],
        now: datetime,
    ) -> list[ChatCitationRow]:
        rows: list[ChatCitationRow] = []
        for cit in citations:
            crow = ChatCitationRow(
                id=new_id(),
                user_id=scope.user_id,
                message_id=message_id,
                record_id=cit.record_id,
                citation_index=cit.index,
                created_at=now,
            )
            session.add(crow)
            rows.append(crow)
        if rows:
            await session.flush()
        return rows

    def decrypt_message_row(
        self,
        row: ChatMessageRow,
        *,
        scope: PersonScope,
        dek: bytes,
        citations: list[HydratedCitation],
    ) -> HydratedChatMessage:
        tier_snap = Tier.from_any(row.tier_snapshot)
        content = self._cipher.decrypt_str(
            dek,
            row.content_enc,
            user_id=scope.user_id,
            table="chat_messages",
            column="content_enc",
            row_id=row.id,
        )
        whisper: str | None = None
        if row.whisper_enc is not None:
            whisper = self._cipher.decrypt_str(
                dek,
                row.whisper_enc,
                user_id=scope.user_id,
                table="chat_messages",
                column="whisper_enc",
                row_id=row.id,
            )

        perspectives: list[str] = []
        if row.perspectives_enc is not None and tier_snap is Tier.GOLD:
            raw_p = self._cipher.decrypt_json(
                dek,
                row.perspectives_enc,
                user_id=scope.user_id,
                table="chat_messages",
                column="perspectives_enc",
                row_id=row.id,
            )
            if isinstance(raw_p, list):
                perspectives = [str(x) for x in raw_p if isinstance(x, str)]

        suggestions: list[str] = []
        if row.suggestions_enc is not None:
            raw_s = self._cipher.decrypt_json(
                dek,
                row.suggestions_enc,
                user_id=scope.user_id,
                table="chat_messages",
                column="suggestions_enc",
                row_id=row.id,
            )
            if isinstance(raw_s, list):
                suggestions = [str(x) for x in raw_s if isinstance(x, str)]

        return HydratedChatMessage(
            id=row.id,
            person_id=row.person_id,
            thread_id=row.thread_id,
            role=ChatRole(row.role),
            content=content,
            whisper_note=whisper,
            perspectives=perspectives,
            suggestions=suggestions,
            citations=citations,
            referenced_material_ids=list(row.referenced_record_ids or ()),
            tier_snapshot=tier_snap,
            safety_category=SafetyCategory(row.safety_category),
            has_unrecorded_gap=row.has_unrecorded_gap,
            client_message_id=row.client_message_id,
            created_at=row.created_at,
        )

    async def list_messages(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        dek: bytes,
        cursor: str | None = None,
        limit: int = 20,
        order: Literal["asc", "desc"] = "asc",
    ) -> tuple[list[HydratedChatMessage], str | None, bool]:
        await self.verify_person(session, scope)

        stmt = select(ChatMessageRow).where(
            ChatMessageRow.user_id == scope.user_id,
            ChatMessageRow.person_id == scope.person_id,
        )

        if cursor is not None:
            decoded = decode_cursor(cursor)
            try:
                cur_ts = datetime.fromisoformat(str(decoded["createdAt"]))
                cur_id = UUID(str(decoded["id"]))
            except (KeyError, ValueError) as exc:
                raise ValidationFailedError("Invalid pagination cursor") from exc

            if order == "asc":
                stmt = stmt.where(
                    or_(
                        ChatMessageRow.created_at > cur_ts,
                        and_(ChatMessageRow.created_at == cur_ts, ChatMessageRow.id > cur_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        ChatMessageRow.created_at < cur_ts,
                        and_(ChatMessageRow.created_at == cur_ts, ChatMessageRow.id < cur_id),
                    )
                )

        if order == "asc":
            stmt = stmt.order_by(ChatMessageRow.created_at.asc(), ChatMessageRow.id.asc())
        else:
            stmt = stmt.order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())

        rows = list((await session.scalars(stmt.limit(limit + 1))).all())
        has_more = len(rows) > limit
        page_rows = rows[:limit]

        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = encode_cursor(
                {"createdAt": last.created_at.isoformat(), "id": str(last.id)}
            )

        citations_by_msg: dict[UUID, list[HydratedCitation]] = defaultdict(list)
        if page_rows:
            msg_ids = [r.id for r in page_rows]
            cit_stmt = (
                select(ChatCitationRow, RecordRow)
                .join(
                    RecordRow,
                    and_(
                        ChatCitationRow.record_id == RecordRow.id,
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                    ),
                )
                .where(
                    ChatCitationRow.user_id == scope.user_id,
                    ChatCitationRow.message_id.in_(msg_ids),
                    RecordRow.deleted_at.is_(None),
                    RecordRow.review_state != RecordReviewState.HELD.value,
                )
                .order_by(ChatCitationRow.message_id.asc(), ChatCitationRow.citation_index.asc())
            )
            cit_pairs = (await session.execute(cit_stmt)).all()
            for crow, rrow in cit_pairs:
                title = self._cipher.decrypt_str(
                    dek,
                    rrow.title_enc,
                    user_id=scope.user_id,
                    table="records",
                    column="title_enc",
                    row_id=rrow.id,
                )
                if rrow.snippet_enc is not None:
                    snippet = self._cipher.decrypt_str(
                        dek,
                        rrow.snippet_enc,
                        user_id=scope.user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=rrow.id,
                    )
                else:
                    body = self._cipher.decrypt_str(
                        dek,
                        rrow.body_enc,
                        user_id=scope.user_id,
                        table="records",
                        column="body_enc",
                        row_id=rrow.id,
                    )
                    snippet = " ".join(body.split())[:80]

                citations_by_msg[crow.message_id].append(
                    HydratedCitation(
                        index=crow.citation_index,
                        material_id=rrow.id,
                        title=sanitize_text(title, allow_empty=True).cleaned_text or "手帐便签",
                        snippet=sanitize_text(snippet, allow_empty=True).cleaned_text,
                        happened_on=(
                            rrow.happened_on.isoformat() if rrow.happened_on is not None else None
                        ),
                    )
                )

        hydrated = [
            self.decrypt_message_row(
                row,
                scope=scope,
                dek=dek,
                citations=citations_by_msg.get(row.id, []),
            )
            for row in page_rows
        ]
        return hydrated, next_cursor, has_more

    async def clear_history(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        now: datetime,
    ) -> int:
        await self.verify_person(session, scope)

        msg_ids = list(
            (
                await session.scalars(
                    select(ChatMessageRow.id).where(
                        ChatMessageRow.user_id == scope.user_id,
                        ChatMessageRow.person_id == scope.person_id,
                    )
                )
            ).all()
        )
        if msg_ids:
            await session.execute(
                delete(ChatCitationRow).where(
                    ChatCitationRow.user_id == scope.user_id,
                    ChatCitationRow.message_id.in_(msg_ids),
                )
            )
            await session.execute(
                delete(ChatMessageRow).where(
                    ChatMessageRow.user_id == scope.user_id,
                    ChatMessageRow.person_id == scope.person_id,
                )
            )

        thread = await session.scalar(
            select(ChatThreadRow)
            .where(
                ChatThreadRow.user_id == scope.user_id,
                ChatThreadRow.person_id == scope.person_id,
            )
            .with_for_update()
        )
        if thread is not None:
            thread.rolling_summary_enc = None
            thread.message_count = 0
            thread.session_started_at = None
            thread.last_user_message_at = None
            thread.updated_at = now
            await session.flush()

        return len(msg_ids)
