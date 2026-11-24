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

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    ContextFactItem,
    ContextMaterialItem,
    ContextReminderItem,
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
from mindweave.modules.memory.domain.temporal_intent import parse_temporal_intent
from mindweave.modules.memory.domain.zh_search import search_inverted_index
from mindweave.modules.memory.domain.zh_synonyms import expand_query_concepts
from mindweave.modules.memory.infra.search_cache import (
    PersonSearchIndexCache,
    build_person_search_index,
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
_WEEKDAY_ZH: tuple[str, ...] = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

DEFAULT_RECALL_CANDIDATE_LIMIT = 3000
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
    """Ranked raw record recall result with its BM25 + synonym + temporal + pinned score."""

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
    """Rank decrypted person-scoped records via Chinese BM25 + concept synonyms + temporal + pinned boost.

    Defensively excludes soft-deleted (`deleted_at IS NOT NULL`) and venting quarantine
    (`review_state == 'held'`) records before computing corpus statistics, and returns only
    records with strictly positive lexical/synonym overlap (`score > 0`) not in `exclude_record_ids`.
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
    expanded_synonyms = expand_query_concepts(query_strong_terms, query) - set(query_tokens)
    excluded = set(exclude_record_ids)
    ref_date = reference_date or date.today()
    inferred_from, inferred_to = parse_temporal_intent(query, ref_date)

    scored: list[tuple[float, int, str, RecalledRecord]] = []
    for idx, cand in enumerate(eligible):
        if cand.record_id in excluded:
            continue
        dl = len(tokenized_docs[idx])
        if dl == 0:
            continue
        tf_map = doc_tfs[idx]

        has_exact_strong = bool(
            not query_strong_terms or any(st in tf_map for st in query_strong_terms)
        )
        has_synonym_hit = bool(
            expanded_synonyms and any(syn in tf_map for syn in expanded_synonyms)
        )
        if not has_exact_strong and not has_synonym_hit:
            continue

        bm25_score = 0.0
        if has_exact_strong:
            for term, q_cnt in query_tf.items():
                tf_val = tf_map.get(term, 0)
                if tf_val <= 0:
                    continue
                df_val = doc_freq.get(term, 0)
                idf = math.log(1.0 + (n_docs - df_val + 0.5) / (df_val + 0.5))
                denom = tf_val + k1 * (1.0 - b + b * (dl / avgdl))
                term_weight = 2.0 if len(term) >= 2 else 1.0
                bm25_score += term_weight * q_cnt * idf * (tf_val * (k1 + 1.0)) / denom

        if has_synonym_hit:
            for syn in expanded_synonyms:
                tf_val = tf_map.get(syn, 0)
                if tf_val <= 0:
                    continue
                df_val = doc_freq.get(syn, 0)
                idf = math.log(1.0 + (n_docs - df_val + 0.5) / (df_val + 0.5))
                denom = tf_val + k1 * (1.0 - b + b * (dl / avgdl))
                bm25_score += 0.90 * idf * (tf_val * (k1 + 1.0)) / denom

        if bm25_score <= 0.0:
            continue

        rec_date = cand.happened_on or (
            cand.created_at.date() if cand.created_at is not None else None
        )
        in_temporal_win = (
            rec_date is not None
            and inferred_from is not None
            and inferred_to is not None
            and inferred_from <= rec_date <= inferred_to
        )
        if rec_date is not None:
            age_days = max(0, (ref_date - rec_date).days)
            # Volatility-aware decay: pinned slips & explicitly queried past windows never decay
            recency_factor = (
                1.0 if (cand.pinned or in_temporal_win) else math.exp(-age_days / 180.0)
            )
            date_ord = rec_date.toordinal()
        else:
            recency_factor = 0.0
            date_ord = 0

        pinned_boost = 1.15 if cand.pinned else 1.0
        temporal_boost = 1.55 if in_temporal_win else 1.0
        final_score = (
            bm25_score * (1.0 + 0.15 * recency_factor) * pinned_boost * temporal_boost
        )
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
    alias_to_record_id: dict[str, UUID] = field(default_factory=dict)
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
        search_cache: PersonSearchIndexCache | None = None,
    ) -> None:
        self._cipher = cipher
        self._default_timezone = default_timezone
        self._max_fact_tokens = max_fact_tokens
        self._max_fact_chars = max_fact_chars
        self._recent_turn_limit = recent_turn_limit
        self._recall_top_k = recall_top_k
        self._recall_candidate_limit = recall_candidate_limit
        self._search_cache = search_cache

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

        try:
            tz_info: ZoneInfo | None = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz_info = None

        if now_utc is not None:
            local_now = now_utc.astimezone(tz_info) if tz_info is not None else now_utc
        else:
            local_now = datetime.now(tz_info) if tz_info is not None else datetime.now()
        local_today = local_now.date()
        weekday_zh = _WEEKDAY_ZH[local_today.weekday()]
        local_time_str = local_now.strftime("%H:%M")

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
                ref_happened_str: str | None = (
                    rec.happened_on.isoformat() if rec.happened_on is not None else None
                )
                record_catalog[rec.id] = CitationRecordMeta(
                    record_id=rec.id,
                    kind=rec.kind,
                    title=clean_title,
                    snippet=clean_snippet,
                    happened_on=ref_happened_str,
                )
                referenced_materials.append(
                    ContextMaterialItem(
                        record_id=rec.id,
                        kind=rec.kind,
                        title=mask_hard_pii(clean_title).masked_text,
                        body=mask_hard_pii(body).masked_text,
                        happened_on=ref_happened_str,
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
        fact_id_to_record_id: dict[UUID, UUID] = {}

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

            is_unlinked = rec_row is None
            effective_rec_id = rec_row.id if rec_row is not None else fact_row.id
            if rec_row is not None:
                fact_backing_records[rec_row.id] = rec_row
                fact_id_to_record_id[fact_row.id] = rec_row.id

            fact_type_enum = FactType(fact_row.type)
            is_expired_one_off = bool(
                fact_type_enum is FactType.DATE
                and fact_row.trigger_date is not None
                and fact_row.trigger_date < local_today
                and not fact_row.is_lunar
            )

            candidate_facts.append(
                BudgetCandidateFact(
                    fact_id=fact_row.id,
                    record_id=effective_rec_id,
                    fact_type=fact_type_enum,
                    content=content,
                    happened_on=fact_happened_str,
                    is_expired_one_off=is_expired_one_off,
                    is_unlinked=is_unlinked,
                )
            )

        # 4. Token-budget trimming: active taboo/date/promise capped so preferences never starve (BUG-CC-7)
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
                    is_unlinked_fact=tf.is_unlinked,
                )
            )
            backing_rec = fact_backing_records.get(tf.record_id)
            if not tf.is_unlinked and backing_rec is not None and backing_rec.id not in record_catalog:
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

        scanned_record_ids: set[UUID] = set(record_catalog.keys())

        # 4b. Long-horizon Raw Record Recall (up to 3,000 slips, zero 200-slip cliff!).
        #     Uses shared watermark-gated `PersonSearchIndexCache` when available (O(1) on hit,
        #     decrypting ONLY the top-K <= 5 matched slips!), while strictly enforcing I1 & I4
        #     (excluding soft-deleted, held venting, and pending-only records).
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
            if self._search_cache is not None:
                eligible_id_rows = list(
                    (
                        await session.scalars(
                            select(RecordRow.id)
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
                )
                eligible_id_set = set(eligible_id_rows)
                scanned_record_ids.update(eligible_id_set)

                if eligible_id_set:
                    # 1ms watermark matching MemoryService.search_person_materials (`include_held=False`)
                    wm_row = (
                        await session.execute(
                            select(func.count(RecordRow.id), func.max(RecordRow.updated_at)).where(
                                RecordRow.user_id == scope.user_id,
                                RecordRow.person_id == scope.person_id,
                                RecordRow.deleted_at.is_(None),
                                RecordRow.review_state != RecordReviewState.HELD.value,
                            )
                        )
                    ).one()
                    watermark = (int(wm_row[0] or 0), wm_row[1])
                    entry = self._search_cache.get(
                        user_id=scope.user_id,
                        person_id=scope.person_id,
                        include_held=False,
                        watermark=watermark,
                    )
                    if entry is None:
                        raw_index_rows = (
                            await session.execute(
                                select(
                                    RecordRow.id,
                                    RecordRow.kind,
                                    RecordRow.title_enc,
                                    RecordRow.body_enc,
                                    RecordRow.happened_on,
                                    RecordRow.created_at,
                                    RecordRow.pinned,
                                    RecordRow.review_state,
                                )
                                .where(
                                    RecordRow.user_id == scope.user_id,
                                    RecordRow.person_id == scope.person_id,
                                    RecordRow.deleted_at.is_(None),
                                    RecordRow.review_state != RecordReviewState.HELD.value,
                                )
                                .order_by(
                                    RecordRow.happened_on.desc(),
                                    RecordRow.created_at.desc(),
                                    RecordRow.id.desc(),
                                )
                                .limit(self._recall_candidate_limit)
                            )
                        ).all()
                        entry = build_person_search_index(
                            user_id=scope.user_id,
                            person_id=scope.person_id,
                            watermark=watermark,
                            cipher=self._cipher,
                            dek=dek,
                            rows=[
                                (r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), str(r[7]))
                                for r in raw_index_rows
                            ],
                        )
                        self._search_cache.put(
                            user_id=scope.user_id,
                            person_id=scope.person_id,
                            include_held=False,
                            entry=entry,
                        )

                    compact_hits, _expanded, _inferred = search_inverted_index(
                        vocab=entry.vocab,
                        inv_postings=entry.inv_postings,
                        docs=entry.docs,
                        avgdl=entry.avgdl,
                        query=user_message,
                        today=local_today,
                        person_names=[person_name],
                        top_k=self._recall_top_k,
                        eligible_record_ids=eligible_id_set,
                        exclude_record_ids=set(dedup_ref_ids),
                    )
                    if compact_hits:
                        hit_ids = [h.record_id for h in compact_hits]
                        matched_rows = (
                            await session.scalars(
                                select(RecordRow).where(
                                    RecordRow.user_id == scope.user_id,
                                    RecordRow.person_id == scope.person_id,
                                    RecordRow.id.in_(hit_ids),
                                    RecordRow.deleted_at.is_(None),
                                    RecordRow.review_state != RecordReviewState.HELD.value,
                                )
                            )
                        ).all()
                        matched_by_id = {r.id: r for r in matched_rows}
                        for hit_id in hit_ids:
                            matched_rec = matched_by_id.get(hit_id)
                            if matched_rec is None:
                                continue
                            clean_title, body, clean_snippet = self._decrypt_record_meta(
                                scope=scope, dek=dek, rec=matched_rec
                            )
                            hit_happened_str: str | None = (
                                matched_rec.happened_on.isoformat()
                                if matched_rec.happened_on is not None
                                else None
                            )
                            if matched_rec.id not in record_catalog:
                                record_catalog[matched_rec.id] = CitationRecordMeta(
                                    record_id=matched_rec.id,
                                    kind=matched_rec.kind,
                                    title=clean_title,
                                    snippet=clean_snippet,
                                    happened_on=hit_happened_str,
                                )
                            referenced_materials.append(
                                ContextMaterialItem(
                                    record_id=matched_rec.id,
                                    kind=matched_rec.kind,
                                    title=mask_hard_pii(clean_title).masked_text,
                                    body=mask_hard_pii(body).masked_text,
                                    happened_on=hit_happened_str,
                                )
                            )
            else:
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
                scanned_record_ids.update(r.id for r in recall_rows)
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
                        cand_happened_str: str | None = (
                            cand.happened_on.isoformat() if cand.happened_on is not None else None
                        )
                        if cand.record_id not in record_catalog:
                            record_catalog[cand.record_id] = CitationRecordMeta(
                                record_id=cand.record_id,
                                kind=cand.kind,
                                title=cand.title,
                                snippet=cand.snippet,
                                happened_on=cand_happened_str,
                            )
                        referenced_materials.append(
                            ContextMaterialItem(
                                record_id=cand.record_id,
                                kind=cand.kind,
                                title=mask_hard_pii(cand.title).masked_text,
                                body=mask_hard_pii(cand.body).masked_text,
                                happened_on=cand_happened_str,
                            )
                        )

        # Assign deterministic short citation aliases `S1..SN` strictly for citable records
        record_to_alias: dict[UUID, str] = {}
        alias_to_record_id: dict[str, UUID] = {}

        def _ensure_alias(rid: UUID) -> str:
            existing = record_to_alias.get(rid)
            if existing is not None:
                return existing
            alias = f"S{len(record_to_alias) + 1}"
            record_to_alias[rid] = alias
            alias_to_record_id[alias] = rid
            return alias

        referenced_materials = [
            mat.model_copy(update={"alias": _ensure_alias(mat.record_id)})
            if mat.record_id in record_catalog
            else mat
            for mat in referenced_materials
        ]
        confirmed_fact_items = [
            fact.model_copy(
                update={
                    "alias": (
                        _ensure_alias(fact.record_id)
                        if (not fact.is_unlinked_fact and fact.record_id in record_catalog)
                        else None
                    )
                }
            )
            for fact in confirmed_fact_items
        ]

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
            turn_local = (
                mrow.created_at.astimezone(tz_info) if tz_info is not None else mrow.created_at
            )
            recent_turns.append(
                ConversationTurn(
                    role=mrow.role,
                    content=mask_hard_pii(turn_text).masked_text,
                    created_at=turn_local.strftime("%Y-%m-%d %H:%M"),
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
        prompt_reminders: list[ContextReminderItem] = []
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
                linked_rid = (
                    fact_id_to_record_id.get(rem.fact_id) if rem.fact_id is not None else None
                )
                src_alias = record_to_alias.get(linked_rid) if linked_rid is not None else None
                rem_dict: dict[str, Any] = {
                    "id": str(rem.id),
                    "title": clean_rem_title,
                    "triggerDate": event_date.isoformat() if event_date is not None else None,
                    "daysUntil": days_until,
                    "isLunar": bool(rem.is_lunar),
                    "rule": rem.rule,
                    "status": rem.status,
                    "sourceAlias": src_alias,
                }
                upcoming_reminders.append(rem_dict)

        upcoming_reminders.sort(
            key=lambda item: (
                item["daysUntil"] is None,
                item["daysUntil"] if item["daysUntil"] is not None else 999999,
            )
        )
        for item in upcoming_reminders[:10]:
            prompt_reminders.append(
                ContextReminderItem(
                    title=mask_hard_pii(str(item["title"])).masked_text,
                    trigger_date=item.get("triggerDate"),
                    days_until=item.get("daysUntil"),
                    is_lunar=bool(item.get("isLunar", False)),
                    source_alias=item.get("sourceAlias"),
                )
            )

        chat_input = CompanionChatInput(
            person_id=scope.person_id,
            person_name=mask_hard_pii(person_name).masked_text,
            tier=tier,
            role_tag=mask_hard_pii(role_tag).masked_text,
            traits=[mask_hard_pii(t).masked_text for t in traits],
            local_date=local_today.isoformat(),
            weekday_zh=weekday_zh,
            local_time=local_time_str,
            timezone_name=tz_name,
            upcoming_reminders=prompt_reminders,
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
            alias_to_record_id=alias_to_record_id,
            total_records_scanned=len(scanned_record_ids),
            confirmed_facts_count=len(confirmed_fact_items),
            active_reminders_count=len(active_reminder_rows),
            recalled_records_count=len(referenced_materials),
            recalled_dates=recalled_dates,
            upcoming_reminders=upcoming_reminders,
        )
