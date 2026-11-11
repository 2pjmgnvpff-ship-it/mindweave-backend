"""Person-isolated AI ContextBuilder for Cupid Companion Chat (Invariant I1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    ContextFactItem,
    ContextMaterialItem,
    ConversationTurn,
)
from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.errors import NotFoundError
from mindweave.core.orm import (
    ChatMessageRow,
    ChatThreadRow,
    FactRow,
    PersonRow,
    RecordRow,
    UserPreferenceRow,
)
from mindweave.modules.companion.domain.output_guard import sanitize_text
from mindweave.modules.companion.domain.tier_policy import (
    BudgetCandidateFact,
    trim_facts_to_token_budget,
)
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    PersonState,
    RecordReviewState,
    SafetyCategory,
    Tier,
)
from mindweave.shared.pii import mask_hard_pii
from mindweave.shared.scopes import PersonScope


def _make_snippet(body: str, max_len: int = 80) -> str:
    compact = " ".join(body.split()).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class CitationRecordMeta:
    """Decrypted metadata for a single citable `RecordRow` within this turn's `PersonScope`."""

    record_id: UUID
    kind: str
    title: str
    snippet: str
    happened_on: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltCompanionContext:
    """Verified, tenant- & person-isolated companion prompt context + allowed citation catalog."""

    scope: PersonScope
    person_name: str
    tier: Tier
    person_state: PersonState
    role_tag: str
    traits: list[str]
    chat_input: CompanionChatInput
    allowed_record_ids: frozenset[UUID]
    record_catalog: dict[UUID, CitationRecordMeta]
    timezone: str
    late_night_care_enabled: bool


class ContextBuilder:
    """Builds `CompanionChatInput` strictly scoped to `PersonScope(user_id, person_id)`.

    Every SQL query explicitly binds `user_id == scope.user_id` AND `person_id == scope.person_id`
    (on top of PostgreSQL RLS) and physically excludes pending/rejected/superseded facts,
    soft-deleted records, and held venting records (`review_state == 'held'`).
    """

    def __init__(
        self,
        cipher: EnvelopeCipher,
        *,
        default_timezone: str = "Asia/Shanghai",
        max_fact_tokens: int = 4000,
        max_fact_chars: int = 6000,
        recent_turn_limit: int = 12,
    ) -> None:
        self._cipher = cipher
        self._default_timezone = default_timezone
        self._max_fact_tokens = max_fact_tokens
        self._max_fact_chars = max_fact_chars
        self._recent_turn_limit = recent_turn_limit

    def _decrypt_record_meta(
        self,
        *,
        scope: PersonScope,
        dek: bytes,
        rec: RecordRow,
    ) -> tuple[str, str, str]:
        title = self._cipher.decrypt_str(
            dek,
            rec.title_enc,
            user_id=scope.user_id,
            table="records",
            column="title_enc",
            row_id=rec.id,
        )
        body = self._cipher.decrypt_str(
            dek,
            rec.body_enc,
            user_id=scope.user_id,
            table="records",
            column="body_enc",
            row_id=rec.id,
        )
        if rec.snippet_enc is not None:
            snippet = self._cipher.decrypt_str(
                dek,
                rec.snippet_enc,
                user_id=scope.user_id,
                table="records",
                column="snippet_enc",
                row_id=rec.id,
            )
        else:
            snippet = _make_snippet(body)

        clean_title = sanitize_text(title, allow_empty=True).cleaned_text or "手帐便签"
        clean_snippet = sanitize_text(snippet, allow_empty=True).cleaned_text
        return clean_title, body, clean_snippet

    async def build(
        self,
        session: AsyncSession,
        *,
        scope: PersonScope,
        dek: bytes,
        user_message: str,
        referenced_material_ids: Sequence[UUID] = (),
        safety_override: SafetyCategory = SafetyCategory.NORMAL,
    ) -> BuiltCompanionContext:
        # 1. Verify PersonRow strictly belongs to (scope.user_id, scope.person_id) & not deleted
        person_row = await session.scalar(
            select(PersonRow).where(
                PersonRow.user_id == scope.user_id,
                PersonRow.id == scope.person_id,
                PersonRow.deleted_at.is_(None),
            )
        )
        if person_row is None:
            raise NotFoundError("Person not found")

        tier = Tier.from_any(person_row.tier)
        person_state = PersonState(person_row.state)
        person_name = self._cipher.decrypt_str(
            dek,
            person_row.display_name_enc,
            user_id=scope.user_id,
            table="persons",
            column="display_name_enc",
            row_id=person_row.id,
        )
        role_tag = (
            self._cipher.decrypt_str(
                dek,
                person_row.role_tag_enc,
                user_id=scope.user_id,
                table="persons",
                column="role_tag_enc",
                row_id=person_row.id,
            )
            if person_row.role_tag_enc is not None
            else ""
        )
        traits: list[str] = []
        if person_row.traits_enc is not None:
            raw_traits = self._cipher.decrypt_json(
                dek,
                person_row.traits_enc,
                user_id=scope.user_id,
                table="persons",
                column="traits_enc",
                row_id=person_row.id,
            )
            if isinstance(raw_traits, list):
                traits = [
                    str(item) for item in raw_traits if isinstance(item, str) and item.strip()
                ]

        # Load user preferences (timezone & late-night care flag)
        pref_row = await session.scalar(
            select(UserPreferenceRow).where(UserPreferenceRow.user_id == scope.user_id)
        )
        tz_name = pref_row.timezone if pref_row is not None else self._default_timezone
        late_night_care_enabled = pref_row.late_night_care_enabled if pref_row is not None else True

        # 2. Verify EVERY requested referenced_material_id belongs to (scope.user_id, scope.person_id)
        #    and is neither soft-deleted nor held in venting quarantine (blocks cross-person injection!)
        dedup_ref_ids = list(dict.fromkeys(referenced_material_ids))
        referenced_materials: list[ContextMaterialItem] = []
        record_catalog: dict[UUID, CitationRecordMeta] = {}

        if dedup_ref_ids:
            ref_rows = (
                await session.scalars(
                    select(RecordRow).where(
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                        RecordRow.id.in_(dedup_ref_ids),
                        RecordRow.deleted_at.is_(None),
                        RecordRow.review_state != RecordReviewState.HELD.value,
                    )
                )
            ).all()
            ref_by_id = {r.id: r for r in ref_rows}
            if len(ref_by_id) != len(dedup_ref_ids):
                raise NotFoundError(
                    "One or more referenced materials do not exist or do not belong to this person"
                )

            for mid in dedup_ref_ids:
                rec = ref_by_id[mid]
                clean_title, body, clean_snippet = self._decrypt_record_meta(
                    scope=scope, dek=dek, rec=rec
                )
                happened_str = rec.happened_on.isoformat() if rec.happened_on is not None else None
                record_catalog[rec.id] = CitationRecordMeta(
                    record_id=rec.id,
                    kind=rec.kind,
                    title=clean_title,
                    snippet=clean_snippet,
                    happened_on=happened_str,
                )
                referenced_materials.append(
                    ContextMaterialItem(
                        record_id=rec.id,
                        kind=rec.kind,
                        title=mask_hard_pii(clean_title).masked_text,
                        body=mask_hard_pii(body).masked_text,
                        happened_on=happened_str,
                    )
                )

        # 3. Query ONLY confirmed, active (`invalid_at IS NULL`) facts for (scope.user_id, scope.person_id),
        #    physically excluding pending/rejected/superseded facts and held/deleted records!
        fact_stmt = (
            select(FactRow, RecordRow)
            .outerjoin(
                RecordRow,
                and_(
                    FactRow.record_id == RecordRow.id,
                    RecordRow.user_id == scope.user_id,
                    RecordRow.person_id == scope.person_id,
                ),
            )
            .where(
                FactRow.user_id == scope.user_id,
                FactRow.person_id == scope.person_id,
                FactRow.status == FactStatus.CONFIRMED.value,
                FactRow.invalid_at.is_(None),
                or_(
                    FactRow.record_id.is_(None),
                    and_(
                        RecordRow.id.is_not(None),
                        RecordRow.deleted_at.is_(None),
                        RecordRow.review_state != RecordReviewState.HELD.value,
                    ),
                ),
            )
            .order_by(FactRow.created_at.desc(), FactRow.id.desc())
        )
        fact_pairs = (await session.execute(fact_stmt)).all()

        candidate_facts: list[BudgetCandidateFact] = []
        fact_backing_records: dict[UUID, RecordRow] = {}

        for fact_row, rec_row in fact_pairs:
            content = self._cipher.decrypt_str(
                dek,
                fact_row.content_enc,
                user_id=scope.user_id,
                table="facts",
                column="content_enc",
                row_id=fact_row.id,
            )
            fact_happened_str: str | None = None
            if rec_row is not None and rec_row.happened_on is not None:
                fact_happened_str = rec_row.happened_on.isoformat()
            elif fact_row.trigger_date is not None:
                fact_happened_str = fact_row.trigger_date.isoformat()

            effective_rec_id = rec_row.id if rec_row is not None else fact_row.id
            if rec_row is not None:
                fact_backing_records[rec_row.id] = rec_row

            candidate_facts.append(
                BudgetCandidateFact(
                    fact_id=fact_row.id,
                    record_id=effective_rec_id,
                    fact_type=FactType(fact_row.type),
                    content=content,
                    happened_on=fact_happened_str,
                )
            )

        # 4. Token-budget trimming (~4,000 tokens / ~6,000 chars): taboo/date/promise ALWAYS first!
        trimmed_facts = trim_facts_to_token_budget(
            candidate_facts,
            max_tokens=self._max_fact_tokens,
            max_chars=self._max_fact_chars,
        )

        confirmed_fact_items: list[ContextFactItem] = []
        for tf in trimmed_facts:
            confirmed_fact_items.append(
                ContextFactItem(
                    record_id=tf.record_id,
                    fact_id=tf.fact_id,
                    fact_type=tf.fact_type,
                    content=mask_hard_pii(tf.content).masked_text,
                    happened_on=tf.happened_on,
                )
            )
            backing_rec = fact_backing_records.get(tf.record_id)
            if backing_rec is not None and backing_rec.id not in record_catalog:
                clean_title, _body, clean_snippet = self._decrypt_record_meta(
                    scope=scope, dek=dek, rec=backing_rec
                )
                record_catalog[backing_rec.id] = CitationRecordMeta(
                    record_id=backing_rec.id,
                    kind=backing_rec.kind,
                    title=clean_title,
                    snippet=clean_snippet,
                    happened_on=(
                        backing_rec.happened_on.isoformat()
                        if backing_rec.happened_on is not None
                        else tf.happened_on
                    ),
                )

        # 5. Load ChatThreadRow rolling summary + last 12 ChatMessageRow turns for (user_id, person_id)
        thread_row = await session.scalar(
            select(ChatThreadRow).where(
                ChatThreadRow.user_id == scope.user_id,
                ChatThreadRow.person_id == scope.person_id,
            )
        )
        rolling_summary: str | None = None
        if thread_row is not None and thread_row.rolling_summary_enc is not None:
            raw_summary = self._cipher.decrypt_str(
                dek,
                thread_row.rolling_summary_enc,
                user_id=scope.user_id,
                table="chat_threads",
                column="rolling_summary_enc",
                row_id=thread_row.id,
            )
            rolling_summary = mask_hard_pii(raw_summary).masked_text

        recent_msg_rows = list(
            (
                await session.scalars(
                    select(ChatMessageRow)
                    .where(
                        ChatMessageRow.user_id == scope.user_id,
                        ChatMessageRow.person_id == scope.person_id,
                    )
                    .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
                    .limit(self._recent_turn_limit)
                )
            ).all()
        )
        recent_msg_rows.reverse()

        recent_turns: list[ConversationTurn] = []
        for mrow in recent_msg_rows:
            turn_text = self._cipher.decrypt_str(
                dek,
                mrow.content_enc,
                user_id=scope.user_id,
                table="chat_messages",
                column="content_enc",
                row_id=mrow.id,
            )
            recent_turns.append(
                ConversationTurn(
                    role=mrow.role,
                    content=mask_hard_pii(turn_text).masked_text,
                )
            )

        chat_input = CompanionChatInput(
            person_id=scope.person_id,
            person_name=mask_hard_pii(person_name).masked_text,
            tier=tier,
            role_tag=mask_hard_pii(role_tag).masked_text,
            traits=[mask_hard_pii(t).masked_text for t in traits],
            confirmed_facts=confirmed_fact_items,
            referenced_materials=referenced_materials,
            rolling_summary=rolling_summary,
            recent_turns=recent_turns,
            user_message=mask_hard_pii(user_message).masked_text,
            safety_override=safety_override,
        )

        return BuiltCompanionContext(
            scope=scope,
            person_name=person_name,
            tier=tier,
            person_state=person_state,
            role_tag=role_tag,
            traits=traits,
            chat_input=chat_input,
            allowed_record_ids=frozenset(record_catalog.keys()),
            record_catalog=record_catalog,
            timezone=tz_name,
            late_night_care_enabled=late_night_care_enabled,
        )
