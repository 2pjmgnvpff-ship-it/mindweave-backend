"""Person-isolated AI ContextBuilder for Cupid Companion Chat (Invariant I1)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, or_, select
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
    ReminderRow,
    UserPreferenceRow,
)
from mindweave.modules.companion.domain.output_guard import sanitize_text
from mindweave.modules.companion.domain.tier_policy import (
    BudgetCandidateFact,
    trim_facts_to_token_budget,
)
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    FactStatus,
    FactType,
    PendingReason,
    PersonState,
    RecordReviewState,
    ReminderStatus,
    SafetyCategory,
    Tier,
)
from mindweave.shared.pii import mask_hard_pii
from mindweave.shared.scopes import PersonScope

# Chinese grammatical and meta-action stop-phrases stripped before CJK bigram generation
_ZH_STOP_PHRASES_RE = re.compile(
    r"(?:什么|怎么|怎样|如何|一下子|一下|一点|有没有|是不是|能不能|可不可以|为什么|可以|"
    r"这个|那个|这些|那些|我们|你们|他们|她们|帮我|请问|觉得|感觉|知道|告诉|"
    r"设一个|设个|提前|提醒|准备|另外|清单|手帐|记录|留意|注意|梳理|整理)"
)

# Chinese grammatical particles, pronouns, and copulas excluded from standalone tokens & bigrams
_ZH_STOP_CHARS: frozenset[str] = frozenset(
    "的了呢吗啊吧呀哦嗯嘛呗啦哇哈我你她他它咱您是在有和与跟把被让给这那哪谁也都很太就才又着"
)

_TOKEN_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+|[a-z0-9]+")

DEFAULT_RECALL_CANDIDATE_LIMIT = 200
DEFAULT_RECALL_TOP_K = 5


def _make_snippet(body: str, max_len: int = 80) -> str:
    compact = " ".join(body.split()).strip()
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


def _extract_bm25_terms(text: str) -> tuple[list[str], set[str]]:
    """Return `(all_tokens, strong_terms)` for Chinese-aware BM25 scoring.

    `all_tokens` includes ASCII/digit tokens, CJK bigrams, and meaningful non-stop CJK
    single characters. `strong_terms` contains multi-character tokens (CJK bigrams and
    ASCII/digit tokens) plus standalone 1-character CJK segments so multi-char Chinese
    words (e.g. `量子`, `火星`, `恐龙`) never falsely match unrelated words via incidental
    single-character overlap (`饺子`, `大火`, `乌龙`).
    """
    if not text:
        return [], set()
    cleaned = _ZH_STOP_PHRASES_RE.sub(" ", text.lower())
    tokens: list[str] = []
    strong_terms: set[str] = set()

    def _flush_cjk_segment(seg_chars: list[str]) -> None:
        if not seg_chars:
            return
        if len(seg_chars) == 1:
            ch = seg_chars[0]
            tokens.append(ch)
            strong_terms.add(ch)
        else:
            seg = "".join(seg_chars)
            tokens.extend(seg_chars)
            for idx in range(len(seg) - 1):
                bg = seg[idx : idx + 2]
                tokens.append(bg)
                strong_terms.add(bg)
        seg_chars.clear()

    for match in _TOKEN_RUN_RE.finditer(cleaned):
        run = match.group(0)
        if run[0].isascii():
            tok = (run.lstrip("0") or "0") if run.isdigit() else run
            tokens.append(tok)
            strong_terms.add(tok)
            continue

        # Split CJK ideograph run on stop-characters so stop-chars never pollute bigrams
        seg_chars: list[str] = []
        for ch in run:
            if ch in _ZH_STOP_CHARS:
                _flush_cjk_segment(seg_chars)
            else:
                seg_chars.append(ch)
        _flush_cjk_segment(seg_chars)

    return tokens, strong_terms


def tokenize_zh_bm25(text: str) -> list[str]:
    """Tokenize mixed Chinese/ASCII text into CJK bigrams, meaningful single chars & ASCII/digit tokens."""
    tokens, _strong = _extract_bm25_terms(text)
    return tokens


@dataclass(frozen=True, slots=True)
class RecallCandidateRecord:
    """Decrypted record candidate scoped strictly to a single `PersonScope` for in-memory BM25 recall."""

    record_id: UUID
    kind: str
    title: str
    body: str
    snippet: str
    happened_on: date | None = None
    pinned: bool = False
    created_at: datetime | None = None
    review_state: str = RecordReviewState.NORMAL.value
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecalledRecord:
    """Ranked raw record recall result with its BM25 + recency + pinned score."""

    candidate: RecallCandidateRecord
    score: float


def recall_relevant_records(
    query: str,
    candidates: Sequence[RecallCandidateRecord],
    *,
    top_k: int = DEFAULT_RECALL_TOP_K,
    exclude_record_ids: Collection[UUID] = (),
    reference_date: date | None = None,
    k1: float = 1.2,
    b: float = 0.75,
) -> list[RecalledRecord]:
    """Rank decrypted person-scoped records via Chinese-aware BM25 (`k1=1.2, b=0.75`) + recency + pinned boost.

    Defensively excludes soft-deleted (`deleted_at IS NOT NULL`) and venting quarantine
    (`review_state == 'held'`) records before computing corpus statistics, and returns only
    records with strictly positive lexical overlap (`score > 0`) not in `exclude_record_ids`.
    """
    if top_k <= 0:
        return []
    query_tokens, query_strong_terms = _extract_bm25_terms(query)
    if not query_tokens:
        return []

    # Defense-in-depth I4 exclusion BEFORE computing corpus IDF/avgdl
    eligible = [
        c
        for c in candidates
        if c.deleted_at is None and c.review_state != RecordReviewState.HELD.value
    ]
    if not eligible:
        return []

    tokenized_docs: list[list[str]] = []
    doc_tfs: list[Counter[str]] = []
    doc_freq: Counter[str] = Counter()

    for cand in eligible:
        happened_str = cand.happened_on.isoformat() if cand.happened_on is not None else ""
        doc_text = f"{cand.title}\n{cand.body}\n{happened_str}"
        toks = tokenize_zh_bm25(doc_text)
        tokenized_docs.append(toks)
        tf = Counter(toks)
        doc_tfs.append(tf)
        for unique_term in tf:
            doc_freq[unique_term] += 1

    n_docs = len(eligible)
    total_len = sum(len(toks) for toks in tokenized_docs)
    if total_len == 0:
        return []
    avgdl = total_len / n_docs

    query_tf = Counter(query_tokens)
    excluded = set(exclude_record_ids)
    ref_date = reference_date or date.today()

    scored: list[tuple[float, int, str, RecalledRecord]] = []
    for idx, cand in enumerate(eligible):
        if cand.record_id in excluded:
            continue
        dl = len(tokenized_docs[idx])
        if dl == 0:
            continue
        tf_map = doc_tfs[idx]

        if query_strong_terms and not any(st in tf_map for st in query_strong_terms):
            continue

        bm25_score = 0.0
        for term, q_cnt in query_tf.items():
            tf_val = tf_map.get(term, 0)
            if tf_val <= 0:
                continue
            df_val = doc_freq.get(term, 0)
            # Non-negative Lucene BM25 IDF (safe even when corpus has N=1 or N=2 records)
            idf = math.log(1.0 + (n_docs - df_val + 0.5) / (df_val + 0.5))
            denom = tf_val + k1 * (1.0 - b + b * (dl / avgdl))
            term_weight = 2.0 if len(term) >= 2 else 1.0
            bm25_score += term_weight * q_cnt * idf * (tf_val * (k1 + 1.0)) / denom

        if bm25_score <= 0.0:
            continue

        rec_date = cand.happened_on or (
            cand.created_at.date() if cand.created_at is not None else None
        )
        if rec_date is not None:
            age_days = max(0, (ref_date - rec_date).days)
            recency_factor = math.exp(-age_days / 180.0)
            date_ord = rec_date.toordinal()
        else:
            recency_factor = 0.0
            date_ord = 0

        pinned_boost = 1.12 if cand.pinned else 1.0
        final_score = bm25_score * (1.0 + 0.15 * recency_factor) * pinned_boost
        scored.append(
            (
                -final_score,
                -date_ord,
                str(cand.record_id),
                RecalledRecord(candidate=cand, score=final_score),
            )
        )

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored[:top_k]]


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
    total_records_scanned: int = 0
    confirmed_facts_count: int = 0
    active_reminders_count: int = 0
    recalled_records_count: int = 0
    recalled_dates: list[str] = field(default_factory=list)
    upcoming_reminders: list[dict[str, Any]] = field(default_factory=list)


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
        recall_top_k: int = DEFAULT_RECALL_TOP_K,
        recall_candidate_limit: int = DEFAULT_RECALL_CANDIDATE_LIMIT,
    ) -> None:
        self._cipher = cipher
        self._default_timezone = default_timezone
        self._max_fact_tokens = max_fact_tokens
        self._max_fact_chars = max_fact_chars
        self._recent_turn_limit = recent_turn_limit
        self._recall_top_k = recall_top_k
        self._recall_candidate_limit = recall_candidate_limit

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
        exclude_message_id: UUID | None = None,
        now_utc: datetime | None = None,
    ) -> BuiltCompanionContext:
        # 1. Verify PersonRow strictly belongs to (scope.user_id, scope.person_id), not deleted & not unassigned
        person_row = await session.scalar(
            select(PersonRow).where(
                PersonRow.user_id == scope.user_id,
                PersonRow.id == scope.person_id,
                PersonRow.deleted_at.is_(None),
                PersonRow.slug.is_distinct_from(UNASSIGNED_PERSON_SLUG),
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

        try:
            tz_info = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz_info = None

        if now_utc is not None:
            local_today = (
                now_utc.astimezone(tz_info).date() if tz_info is not None else now_utc.date()
            )
        else:
            local_today = datetime.now(tz_info).date() if tz_info is not None else date.today()

        scanned_record_ids: set[UUID] = set(record_catalog.keys())

        # 4b. D4 Raw Record Recall: in-memory decrypted BM25 + time-decay over person-scoped slips.
        #     Strictly scoped to (user_id, person_id), physically excluding soft-deleted records,
        #     held venting records, and records whose only facts are pending/rejected/superseded (I1 & I4).
        if self._recall_top_k > 0 and user_message.strip():
            has_any_fact = exists(
                select(1).where(
                    FactRow.user_id == scope.user_id,
                    FactRow.record_id == RecordRow.id,
                )
            )
            has_active_confirmed_fact = exists(
                select(1).where(
                    FactRow.user_id == scope.user_id,
                    FactRow.record_id == RecordRow.id,
                    FactRow.status == FactStatus.CONFIRMED.value,
                    FactRow.invalid_at.is_(None),
                )
            )
            recall_rows = (
                await session.scalars(
                    select(RecordRow)
                    .where(
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                        RecordRow.deleted_at.is_(None),
                        RecordRow.review_state != RecordReviewState.HELD.value,
                        or_(
                            RecordRow.review_state == RecordReviewState.NOTE_ONLY.value,
                            ~has_any_fact,
                            has_active_confirmed_fact,
                        ),
                    )
                    .order_by(RecordRow.created_at.desc(), RecordRow.id.desc())
                    .limit(self._recall_candidate_limit)
                )
            ).all()

            scanned_record_ids.update(rec.id for rec in recall_rows)

            if recall_rows:
                recall_candidates: list[RecallCandidateRecord] = []
                for rec in recall_rows:
                    clean_title, body, clean_snippet = self._decrypt_record_meta(
                        scope=scope, dek=dek, rec=rec
                    )
                    recall_candidates.append(
                        RecallCandidateRecord(
                            record_id=rec.id,
                            kind=rec.kind,
                            title=clean_title,
                            body=body,
                            snippet=clean_snippet,
                            happened_on=rec.happened_on,
                            pinned=bool(rec.pinned),
                            created_at=rec.created_at,
                            review_state=rec.review_state,
                            deleted_at=rec.deleted_at,
                        )
                    )

                recalled_hits = recall_relevant_records(
                    user_message,
                    recall_candidates,
                    top_k=self._recall_top_k,
                    exclude_record_ids=dedup_ref_ids,
                    reference_date=local_today,
                )
                for hit in recalled_hits:
                    cand = hit.candidate
                    happened_str = (
                        cand.happened_on.isoformat() if cand.happened_on is not None else None
                    )
                    if cand.record_id not in record_catalog:
                        record_catalog[cand.record_id] = CitationRecordMeta(
                            record_id=cand.record_id,
                            kind=cand.kind,
                            title=cand.title,
                            snippet=cand.snippet,
                            happened_on=happened_str,
                        )
                    referenced_materials.append(
                        ContextMaterialItem(
                            record_id=cand.record_id,
                            kind=cand.kind,
                            title=mask_hard_pii(cand.title).masked_text,
                            body=mask_hard_pii(cand.body).masked_text,
                            happened_on=happened_str,
                        )
                    )

        # Collect unique chronological/ranked dates for recalled & attached raw materials
        recalled_dates: list[str] = []
        seen_dates: set[str] = set()
        for mat in referenced_materials:
            if mat.happened_on and mat.happened_on not in seen_dates:
                seen_dates.add(mat.happened_on)
                recalled_dates.append(mat.happened_on)

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

        recent_stmt = select(ChatMessageRow).where(
            ChatMessageRow.user_id == scope.user_id,
            ChatMessageRow.person_id == scope.person_id,
        )
        if exclude_message_id is not None:
            recent_stmt = recent_stmt.where(ChatMessageRow.id != exclude_message_id)

        recent_msg_rows = list(
            (
                await session.scalars(
                    recent_stmt.order_by(
                        ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc()
                    ).limit(self._recent_turn_limit)
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

        # 6. Load active person-scoped ReminderRow items (strictly excluding venting/held/deleted/rejected/superseded backing facts)
        active_reminder_rows = (
            await session.scalars(
                select(ReminderRow)
                .outerjoin(
                    FactRow,
                    and_(
                        FactRow.id == ReminderRow.fact_id,
                        FactRow.user_id == scope.user_id,
                        FactRow.person_id == scope.person_id,
                    ),
                )
                .outerjoin(
                    RecordRow,
                    and_(
                        RecordRow.id == FactRow.record_id,
                        RecordRow.user_id == scope.user_id,
                        RecordRow.person_id == scope.person_id,
                    ),
                )
                .where(
                    ReminderRow.user_id == scope.user_id,
                    ReminderRow.person_id == scope.person_id,
                    ReminderRow.status == ReminderStatus.ACTIVE.value,
                    or_(
                        ReminderRow.fact_id.is_(None),
                        and_(
                            FactRow.id.is_not(None),
                            FactRow.invalid_at.is_(None),
                            FactRow.status != FactStatus.REJECTED.value,
                            FactRow.pending_reason.is_distinct_from(PendingReason.VENTING.value),
                            or_(
                                FactRow.record_id.is_(None),
                                and_(
                                    RecordRow.id.is_not(None),
                                    RecordRow.deleted_at.is_(None),
                                    RecordRow.review_state != RecordReviewState.HELD.value,
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(
                    ReminderRow.next_fire_at.asc().nullslast(),
                    ReminderRow.created_at.desc(),
                )
                .limit(50)
            )
        ).all()

        upcoming_reminders: list[dict[str, Any]] = []
        for rem in active_reminder_rows:
            raw_rem_title = self._cipher.decrypt_str(
                dek,
                rem.title_enc,
                user_id=scope.user_id,
                table="reminders",
                column="title_enc",
                row_id=rem.id,
            )
            clean_rem_title = sanitize_text(raw_rem_title, allow_empty=True).cleaned_text
            if not clean_rem_title:
                continue

            event_date: date | None = rem.once_date
            if event_date is None and rem.next_fire_at is not None:
                local_fire_date = (
                    rem.next_fire_at.astimezone(tz_info).date()
                    if tz_info is not None
                    else rem.next_fire_at.date()
                )
                event_date = local_fire_date + timedelta(days=int(rem.next_lead_day or 0))

            days_until = (event_date - local_today).days if event_date is not None else None
            if days_until is None or days_until >= 0:
                upcoming_reminders.append(
                    {
                        "id": str(rem.id),
                        "title": clean_rem_title,
                        "triggerDate": event_date.isoformat() if event_date is not None else None,
                        "daysUntil": days_until,
                        "isLunar": bool(rem.is_lunar),
                        "rule": rem.rule,
                        "status": rem.status,
                    }
                )

        upcoming_reminders.sort(
            key=lambda item: (
                item["daysUntil"] is None,
                item["daysUntil"] if item["daysUntil"] is not None else 999999,
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
            total_records_scanned=len(scanned_record_ids),
            confirmed_facts_count=len(confirmed_fact_items),
            active_reminders_count=len(active_reminder_rows),
            recalled_records_count=len(referenced_materials),
            recalled_dates=recalled_dates,
            upcoming_reminders=upcoming_reminders,
        )
