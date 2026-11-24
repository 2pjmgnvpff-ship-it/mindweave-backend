"""Bounded, DEK-gated, multi-instance watermark-safe inverted postings cache for encrypted search.

Security & Memory Invariants:
- Never stores full decrypted body or snippet plaintext in RAM (only integer inverted postings).
- Always gated behind live `load_user_dek(session, scope)` BEFORE cache lookup so `DELETE /v1/me`
  on any Cloud Run instance immediately blocks cache reads across ALL instances.
- Wired via `EnvelopeCipher.register_eviction_hook(cache.evict_user)` in `wiring.py` (preserving
  `.importlinter` rule that `mindweave.core` never imports `mindweave.modules`).
- Enforces `MAX_TOTAL_CACHED_SLIPS = 12_000` LRU cap (~2.6 MB tracemalloc / +6 MB RSS).
"""

from __future__ import annotations

import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from mindweave.core.crypto import EnvelopeCipher
from mindweave.modules.memory.domain.zh_search import CompactDocMeta, extract_zh_terms

MAX_TOTAL_CACHED_SLIPS: int = 12_000
DEFAULT_SEARCH_CACHE_TTL_SECONDS: int = 300


@dataclass(slots=True)
class PersonSearchIndexEntry:
    user_id: UUID
    person_id: UUID
    watermark: tuple[int, datetime | None]
    expires_at: float
    vocab: dict[str, int]
    inv_postings: dict[int, tuple[tuple[int, int], ...]]
    docs: list[CompactDocMeta]
    avgdl: float

    @property
    def slip_count(self) -> int:
        return len(self.docs)

    def clear(self) -> None:
        self.vocab.clear()
        self.inv_postings.clear()
        self.docs.clear()


class PersonSearchIndexCache:
    """Thread-safe slip-count-capped LRU inverted postings cache."""

    def __init__(
        self,
        *,
        max_total_slips: int = MAX_TOTAL_CACHED_SLIPS,
        ttl_seconds: int = DEFAULT_SEARCH_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_total_slips = max(1, int(max_total_slips))
        self._ttl = max(1, int(ttl_seconds))
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[UUID, UUID, bool], PersonSearchIndexEntry] = OrderedDict()
        self._total_slips: int = 0

    @property
    def total_slips(self) -> int:
        with self._lock:
            return self._total_slips

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(
        self,
        *,
        user_id: UUID,
        person_id: UUID,
        include_held: bool,
        watermark: tuple[int, datetime | None],
    ) -> PersonSearchIndexEntry | None:
        key = (user_id, person_id, include_held)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now or entry.watermark != watermark:
                self._remove_key_locked(key)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(
        self,
        *,
        user_id: UUID,
        person_id: UUID,
        include_held: bool,
        entry: PersonSearchIndexEntry,
    ) -> None:
        key = (user_id, person_id, include_held)
        with self._lock:
            if key in self._entries:
                self._remove_key_locked(key)
            self._entries[key] = entry
            self._total_slips += entry.slip_count
            while self._total_slips > self._max_total_slips and len(self._entries) > 1:
                oldest_key = next(iter(self._entries))
                self._remove_key_locked(oldest_key)

    def evict_person(self, user_id: UUID, person_id: UUID) -> int:
        removed = 0
        with self._lock:
            for include_held in (False, True):
                key = (user_id, person_id, include_held)
                if key in self._entries:
                    self._remove_key_locked(key)
                    removed += 1
        return removed

    def evict_user(self, user_id: UUID | None) -> int:
        """Evict all cached entries for a user, or clear all entries when `user_id is None`."""
        if user_id is None:
            return self.clear()
        with self._lock:
            keys = [k for k in self._entries if k[0] == user_id]
            for key in keys:
                self._remove_key_locked(key)
            return len(keys)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            for entry in self._entries.values():
                entry.clear()
            self._entries.clear()
            self._total_slips = 0
            return count

    def _remove_key_locked(self, key: tuple[UUID, UUID, bool]) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._total_slips = max(0, self._total_slips - old.slip_count)
            old.clear()


def build_person_search_index(
    *,
    user_id: UUID,
    person_id: UUID,
    watermark: tuple[int, datetime | None],
    cipher: EnvelopeCipher,
    dek: bytes,
    rows: Sequence[tuple[UUID, str, bytes, bytes, date, datetime, bool, str]],
    ttl_seconds: int = DEFAULT_SEARCH_CACHE_TTL_SECONDS,
) -> PersonSearchIndexEntry:
    """Decrypt ONLY `title_enc` & `body_enc` into compact integer inverted postings (zero body RAM)."""
    vocab: dict[str, int] = {}
    inv_lists: dict[int, list[tuple[int, int]]] = {}
    docs: list[CompactDocMeta] = []
    total_len = 0

    for doc_idx, (
        rid,
        kind,
        title_enc,
        body_enc,
        happened_on,
        created_at,
        pinned,
        review_state,
    ) in enumerate(rows):
        title = cipher.decrypt_str(
            dek,
            title_enc,
            user_id=user_id,
            table="records",
            column="title_enc",
            row_id=rid,
        )
        body = cipher.decrypt_str(
            dek,
            body_enc,
            user_id=user_id,
            table="records",
            column="body_enc",
            row_id=rid,
        )
        toks, _ = extract_zh_terms(f"{title}\n{body}\n{happened_on.isoformat()}")
        tf = Counter(toks)
        for term, cnt in tf.items():
            tid = vocab.get(term)
            if tid is None:
                tid = len(vocab)
                vocab[term] = tid
                inv_lists[tid] = [(doc_idx, cnt)]
            else:
                inv_lists[tid].append((doc_idx, cnt))
        dl = len(toks)
        total_len += dl
        docs.append(
            CompactDocMeta(
                record_id=rid,
                kind=kind,
                happened_on=happened_on,
                created_at=created_at,
                pinned=pinned,
                review_state=review_state,
                doc_len=dl,
            )
        )

    avgdl = (total_len / len(docs)) if docs else 1.0
    inv_postings = {tid: tuple(plist) for tid, plist in inv_lists.items()}
    return PersonSearchIndexEntry(
        user_id=user_id,
        person_id=person_id,
        watermark=watermark,
        expires_at=time.monotonic() + ttl_seconds,
        vocab=vocab,
        inv_postings=inv_postings,
        docs=docs,
        avgdl=avgdl,
    )
