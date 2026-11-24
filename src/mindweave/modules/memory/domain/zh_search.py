"""Pure-domain zero-dict CJK bigram + Latin BM25+ hybrid search & highlight extraction.

Strictly zero framework, database, HTTP, or ORM imports (enforced by `.importlinter`).
Shared Single Source of Truth between `GET /v1/persons/{id}/search` and Cupid `ContextBuilder`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from mindweave.modules.memory.domain.temporal_intent import parse_temporal_intent
from mindweave.modules.memory.domain.zh_synonyms import expand_query_concepts

_ZH_STOP_PHRASES_RE = re.compile(
    r"(?:什么|怎么|怎样|如何|一下子|一下|一点|有没有|是不是|能不能|可不可以|为什么|可以|"
    r"这个|那个|这些|那些|我们|你们|他们|她们|帮我|请问|觉得|感觉|知道|告诉|"
    r"设一个|设个|提前|提醒|准备|另外|清单|手帐|记录|留意|注意|梳理|整理)"
)
_ZH_STOP_CHARS: frozenset[str] = frozenset(
    "的了呢吗啊吧呀哦嗯嘛呗啦哇哈我你她他它咱您是在有和与跟把被让给这那哪谁也都很太就才又着"
)
_TOKEN_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+|[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class CompactDocMeta:
    """Compact non-plaintext metadata stored per slip inside the bounded inverted cache."""

    record_id: UUID
    kind: str
    happened_on: date
    created_at: datetime
    pinned: bool
    review_state: str
    doc_len: int


@dataclass(frozen=True, slots=True)
class CompactSearchHit:
    record_id: UUID
    score: float
    match_kind: str


def extract_zh_terms(text: str) -> tuple[list[str], set[str]]:
    """Zero-dict CJK bigram + meaningful unigram + ASCII/digit tokenizer (0 MB static RAM)."""
    if not text:
        return [], set()
    cleaned = _ZH_STOP_PHRASES_RE.sub(" ", text.lower())
    tokens: list[str] = []
    strong: set[str] = set()

    def _flush(chars: list[str]) -> None:
        if not chars:
            return
        if len(chars) == 1:
            ch = chars[0]
            tokens.append(ch)
            strong.add(ch)
        else:
            seg = "".join(chars)
            tokens.extend(chars)
            for i in range(len(seg) - 1):
                bg = seg[i : i + 2]
                tokens.append(bg)
                strong.add(bg)
        chars.clear()

    for match in _TOKEN_RUN_RE.finditer(cleaned):
        run = match.group(0)
        if run[0].isascii():
            tok = (run.lstrip("0") or "0") if run.isdigit() else run
            tokens.append(tok)
            strong.add(tok)
            continue
        seg: list[str] = []
        for ch in run:
            if ch in _ZH_STOP_CHARS:
                _flush(seg)
            else:
                seg.append(ch)
        _flush(seg)
    return tokens, strong


def _collect_self_name_terms(person_names: Sequence[str]) -> set[str]:
    terms: set[str] = set()
    for name in person_names:
        if not name:
            continue
        ntoks, nstrong = extract_zh_terms(name)
        terms.update(ntoks)
        terms.update(nstrong)
    return terms


def search_inverted_index(
    *,
    vocab: Mapping[str, int],
    inv_postings: Mapping[int, tuple[tuple[int, int], ...]],
    docs: Sequence[CompactDocMeta],
    avgdl: float,
    query: str,
    today: date,
    person_names: Sequence[str] = (),
    top_k: int = 20,
    kind: str | None = None,
    pinned: bool | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    eligible_record_ids: set[UUID] | None = None,
    exclude_record_ids: set[UUID] | None = None,
    fact_boosts: Mapping[UUID, float] | None = None,
    vector_ranked: Sequence[UUID] | None = None,
    k1: float = 1.2,
    b: float = 0.75,
    delta: float = 0.5,
) -> tuple[list[CompactSearchHit], list[str], tuple[date | None, date | None]]:
    """Execute O(matched postings) BM25+ + synonym expansion + temporal boost + RRF fusion."""
    q_tokens, q_strong = extract_zh_terms(query)
    if not q_tokens or not docs:
        return [], [], (None, None)

    self_name_terms = _collect_self_name_terms(person_names)
    non_name_strong = q_strong - self_name_terms
    effective_strong = non_name_strong if non_name_strong else q_strong

    expanded = expand_query_concepts(effective_strong, query)
    inferred_from, inferred_to = parse_temporal_intent(query, today)

    def _doc_allowed(d: CompactDocMeta) -> bool:
        if kind is not None and d.kind != kind:
            return False
        if pinned is not None and d.pinned is not pinned:
            return False
        if date_from is not None and d.happened_on < date_from:
            return False
        if date_to is not None and d.happened_on > date_to:
            return False
        if eligible_record_ids is not None and d.record_id not in eligible_record_ids:
            return False
        if exclude_record_ids is not None and d.record_id in exclude_record_ids:
            return False
        return True

    n_docs = len(docs)
    safe_avgdl = avgdl if avgdl > 0.0 else 1.0
    doc_idx_by_id = {d.record_id: idx for idx, d in enumerate(docs)}

    # 1. Exact BM25+ gated strictly on `effective_strong` (blocks noisy 1-char unigram fallback!)
    exact_scores: dict[int, float] = {}
    strong_tids = [vocab[t] for t in effective_strong if t in vocab]
    if effective_strong:
        eligible_exact_docs: set[int] = set()
        for stid in strong_tids:
            for doc_idx, _ in inv_postings.get(stid, ()):
                if _doc_allowed(docs[doc_idx]):
                    eligible_exact_docs.add(doc_idx)
    else:
        eligible_exact_docs = {idx for idx, d in enumerate(docs) if _doc_allowed(d)}

    if eligible_exact_docs:
        q_tf = Counter(q_tokens)
        for term, q_cnt in q_tf.items():
            tid = vocab.get(term)
            if tid is None:
                continue
            plist = inv_postings.get(tid)
            if not plist:
                continue
            weight = (
                0.20
                if (term in self_name_terms and non_name_strong)
                else (2.2 if len(term) >= 2 else 0.8)
            )
            df = len(plist)
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            for doc_idx, tf_val in plist:
                if doc_idx not in eligible_exact_docs:
                    continue
                dl = docs[doc_idx].doc_len
                if dl == 0:
                    continue
                denom = tf_val + k1 * (1.0 - b + b * (dl / safe_avgdl))
                exact_scores[doc_idx] = exact_scores.get(doc_idx, 0.0) + (
                    weight * q_cnt * idf * (((tf_val * (k1 + 1.0)) / denom) + delta)
                )

    # 2. Concept / Synonym BM25+ (weight 0.95 vs exact 2.2 so exact matches outrank synonyms)
    syn_scores: dict[int, float] = {}
    q_token_set = set(q_tokens)
    for st in expanded:
        if st in q_token_set:
            continue
        tid = vocab.get(st)
        if tid is None:
            continue
        plist = inv_postings.get(tid)
        if not plist:
            continue
        df = len(plist)
        idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        for doc_idx, tf_val in plist:
            if not _doc_allowed(docs[doc_idx]):
                continue
            dl = docs[doc_idx].doc_len
            if dl == 0:
                continue
            denom = tf_val + k1 * (1.0 - b + b * (dl / safe_avgdl))
            syn_scores[doc_idx] = syn_scores.get(doc_idx, 0.0) + (
                0.95 * idf * (((tf_val * (k1 + 1.0)) / denom) + delta)
            )

    bm25_ranked: list[tuple[float, UUID]] = []
    temp_bonus: dict[UUID, float] = {}
    for doc_idx, raw_score in exact_scores.items():
        d = docs[doc_idx]
        in_win = (
            inferred_from is not None
            and inferred_to is not None
            and (inferred_from <= d.happened_on <= inferred_to)
        )
        if in_win:
            temp_bonus[d.record_id] = 0.012
        bm25_ranked.append(
            (raw_score * (1.10 if d.pinned else 1.0) * (1.65 if in_win else 1.0), d.record_id)
        )

    syn_ranked: list[tuple[float, UUID]] = []
    for doc_idx, raw_score in syn_scores.items():
        d = docs[doc_idx]
        in_win = (
            inferred_from is not None
            and inferred_to is not None
            and (inferred_from <= d.happened_on <= inferred_to)
        )
        if in_win:
            temp_bonus[d.record_id] = 0.012
        syn_ranked.append((raw_score * (1.65 if in_win else 1.0), d.record_id))

    bm25_ranked.sort(key=lambda x: (-x[0], str(x[1])))
    syn_ranked.sort(key=lambda x: (-x[0], str(x[1])))

    # 3. Reciprocal Rank Fusion (k=60)
    rrf: dict[UUID, float] = {}
    kind_map: dict[UUID, str] = {}
    for rank, (_s, rid) in enumerate(bm25_ranked, start=1):
        rrf[rid] = rrf.get(rid, 0.0) + 1.0 / (60.0 + rank)
        kind_map[rid] = "exact_lexical"
    for rank, (_s, rid) in enumerate(syn_ranked, start=1):
        rrf[rid] = rrf.get(rid, 0.0) + 0.65 / (60.0 + rank)
        kind_map.setdefault(rid, "semantic_synonym")

    if vector_ranked:
        v_rank = 0
        for rid in vector_ranked:
            idx = doc_idx_by_id.get(rid)
            if idx is None or not _doc_allowed(docs[idx]):
                continue
            v_rank += 1
            rrf[rid] = rrf.get(rid, 0.0) + 0.85 / (60.0 + v_rank)
            kind_map.setdefault(rid, "semantic_vector")

    if fact_boosts:
        fact_ranked = sorted(
            (
                (score, rid)
                for rid, score in fact_boosts.items()
                if score > 0.0
                and rid in doc_idx_by_id
                and _doc_allowed(docs[doc_idx_by_id[rid]])
            ),
            key=lambda x: (-x[0], str(x[1])),
        )
        for rank, (_s, rid) in enumerate(fact_ranked, start=1):
            rrf[rid] = rrf.get(rid, 0.0) + 0.50 / (60.0 + rank)
            kind_map.setdefault(rid, "fact_match")

    for rid, bonus in temp_bonus.items():
        if rid in rrf:
            rrf[rid] += bonus

    merged = sorted(
        (
            CompactSearchHit(
                record_id=rid,
                score=round(score, 6),
                match_kind=kind_map.get(rid, "exact_lexical"),
            )
            for rid, score in rrf.items()
        ),
        key=lambda h: (-h.score, -docs[doc_idx_by_id[h.record_id]].happened_on.toordinal(), str(h.record_id)),
    )
    expanded_sorted = sorted(expanded)
    return merged[:top_k], expanded_sorted, (inferred_from, inferred_to)


def extract_matched_terms_and_snippet(
    *,
    title: str,
    body: str,
    query: str,
    expanded_terms: Sequence[str],
    person_names: Sequence[str] = (),
    max_chars: int = 120,
) -> tuple[list[str], str]:
    """Return ordered deduplicated `matchedTerms` (literal + expanded synonyms) and a focused snippet."""
    haystack = f"{title}\n{body}"
    haystack_low = haystack.lower()
    _q_toks, q_strong = extract_zh_terms(query)
    self_name_terms = _collect_self_name_terms(person_names)
    non_name_strong = q_strong - self_name_terms
    ordered_literal = sorted(
        non_name_strong if non_name_strong else q_strong,
        key=lambda t: (-len(t), t),
    )

    matched: list[str] = []
    seen: set[str] = set()
    first_pos: int | None = None

    for term in (*ordered_literal, *expanded_terms):
        t_low = term.lower()
        if not t_low or t_low in seen:
            continue
        pos = haystack_low.find(t_low)
        if pos >= 0:
            seen.add(t_low)
            matched.append(term)
            if first_pos is None or pos < first_pos:
                first_pos = pos

    clean_body = " ".join(body.split())
    if not clean_body:
        return matched, title[:max_chars]

    if len(clean_body) <= max_chars:
        return matched, clean_body

    body_low = clean_body.lower()
    body_hit_pos: int | None = None
    for term in matched:
        idx = body_low.find(term.lower())
        if idx >= 0 and (body_hit_pos is None or idx < body_hit_pos):
            body_hit_pos = idx

    if body_hit_pos is None:
        return matched, clean_body[: max_chars - 1].rstrip() + "…"

    start = max(0, body_hit_pos - max_chars // 3)
    end = min(len(clean_body), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean_body) else ""
    return matched, f"{prefix}{clean_body[start:end].strip()}{suffix}"
