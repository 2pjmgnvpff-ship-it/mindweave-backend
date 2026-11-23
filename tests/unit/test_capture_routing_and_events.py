"""Pure unit tests for history-grounded routing, spoken self-correction,
encrypted recent-context loading, zero-bleed multi-person & multi-event archiving,
and atomic 5-second undo."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from mindweave.ai.schemas.contracts import (
    ExtractedFactItem,
    ExtractRecordInput,
    ExtractRecordOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.core.clock import ManualClock
from mindweave.core.config import Settings
from mindweave.core.crypto import EnvelopeCipher, EnvKekProvider
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    FactRow,
    IdempotencyKeyRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    ReminderRow,
)
from mindweave.modules.capture.application.service import CapturePipelineService
from mindweave.modules.capture.domain.routing import (
    KnownPersonProfile,
    apply_spoken_self_correction,
    partition_by_event_group,
    resolve_by_history_clues,
    resolve_mention_for_fact,
)
from mindweave.modules.capture.infra.repository import load_recent_person_context
from mindweave.modules.memory.infra.repository import decrypt_record_fields
from mindweave.shared.enums import (
    CaptureSource,
    CaptureStatus,
    FactStatus,
    FactType,
    PersonState,
    Polarity,
    Presentation,
    RecordReviewState,
    RouteMode,
    Tier,
)
from mindweave.shared.scopes import UserScope


def _build_cipher_and_dek(user_id: UUID) -> tuple[EnvelopeCipher, bytes]:
    settings = Settings(environment="test")
    cipher = EnvelopeCipher(EnvKekProvider(kek_hex=settings.kek_hex))
    wrapped_dek, kek_id = cipher.generate_wrapped_dek(user_id)
    dek = cipher.load_dek(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id)
    return cipher, dek


# ============================================================================
# Helpers: Fact factory & lightweight in-memory session
# ============================================================================


def _make_fact(
    *,
    person_mention: str,
    fact_type: FactType,
    content: str,
    person_id: UUID | None = None,
    polarity: Polarity = Polarity.NEUTRAL,
    title: str = "",
    trigger_date: str | None = None,
    confidence: float = 0.95,
    event_group: str | None = None,
) -> ExtractedFactItem:
    return ExtractedFactItem(
        person_mention=person_mention,
        person_id=person_id,
        fact_type=fact_type,
        polarity=polarity,
        title=title,
        content=content,
        trigger_date=trigger_date,
        confidence=confidence,
        event_group=event_group,
    )


class _ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def all(self) -> list[Any]:
        return list(self._items)


class _InMemoryCaptureSession:
    """Minimal async SQLAlchemy session double for pure unit tests of CapturePipelineService."""

    def __init__(self) -> None:
        self.captures: dict[UUID, CaptureRow] = {}
        self.persons: dict[UUID, PersonRow] = {}
        self.aliases: dict[UUID, PersonAliasRow] = {}
        self.records: dict[UUID, RecordRow] = {}
        self.facts: dict[UUID, FactRow] = {}
        self.inbox_items: dict[UUID, InboxItemRow] = {}
        self.reminders: dict[UUID, ReminderRow] = {}
        self.idempotency_keys: dict[UUID, IdempotencyKeyRow] = {}
        self.user_tz: str = "Asia/Shanghai"
        self.last_statements: list[Any] = []

    def add(self, obj: Any) -> None:
        if isinstance(obj, CaptureRow):
            self.captures[obj.id] = obj
        elif isinstance(obj, PersonRow):
            self.persons[obj.id] = obj
        elif isinstance(obj, PersonAliasRow):
            self.aliases[obj.id] = obj
        elif isinstance(obj, RecordRow):
            self.records[obj.id] = obj
        elif isinstance(obj, FactRow):
            self.facts[obj.id] = obj
        elif isinstance(obj, InboxItemRow):
            self.inbox_items[obj.id] = obj
        elif isinstance(obj, ReminderRow):
            self.reminders[obj.id] = obj
        elif isinstance(obj, IdempotencyKeyRow):
            self.idempotency_keys[obj.id] = obj

    async def flush(self) -> None:
        return None

    async def get(self, model: type[Any], ident: Any) -> Any:
        if model is CaptureRow:
            return self.captures.get(ident)
        if model is IdempotencyKeyRow:
            return self.idempotency_keys.get(ident)
        if model is PersonRow:
            return self.persons.get(ident)
        return None

    def _compile(self, stmt: Any) -> tuple[str, dict[str, Any]]:
        compiled = stmt.compile(dialect=postgresql.dialect())
        return str(compiled), dict(compiled.params)

    async def scalar(self, stmt: Any) -> Any:
        self.last_statements.append(stmt)
        sql, params = self._compile(stmt)
        if "FROM user_preferences" in sql:
            return self.user_tz
        if "FROM captures" in sql:
            cap_id = next((v for k, v in params.items() if k.startswith("id")), None)
            if cap_id is not None:
                return self.captures.get(cap_id)
            return next(iter(self.captures.values()), None)
        if "FROM persons" in sql:
            pid = next((v for k, v in params.items() if k.startswith("id")), None)
            if pid is not None:
                p = self.persons.get(pid)
                if p is not None and p.deleted_at is None:
                    return p
            return None
        if "FROM reminders" in sql:
            fid = next((v for k, v in params.items() if k.startswith("fact_id")), None)
            for rem in self.reminders.values():
                if rem.fact_id == fid:
                    return rem
            return None
        return None

    async def scalars(self, stmt: Any) -> _ScalarResult:
        self.last_statements.append(stmt)
        sql, params = self._compile(stmt)
        if "FROM persons" in sql and "FROM facts" not in sql:
            rows = [
                p
                for p in self.persons.values()
                if p.deleted_at is None and p.state != PersonState.ARCHIVED.value
            ]
            rows.sort(key=lambda p: p.created_at)
            return _ScalarResult(rows)
        if "FROM person_aliases" in sql:
            return _ScalarResult(list(self.aliases.values()))
        if "FROM records" in sql and "FROM facts" not in sql:
            cap_id = next((v for k, v in params.items() if k.startswith("capture_id")), None)
            pid = next((v for k, v in params.items() if k.startswith("person_id")), None)
            rows_rec = [
                r
                for r in self.records.values()
                if r.deleted_at is None
                and (cap_id is None or r.capture_id == cap_id)
                and (pid is None or r.person_id == pid)
            ]
            return _ScalarResult(rows_rec)
        if "FROM inbox_items" in sql:
            cap_id = next((v for k, v in params.items() if k.startswith("capture_id")), None)
            rows_inb = [
                i
                for i in self.inbox_items.values()
                if i.status == "open" and (cap_id is None or i.capture_id == cap_id)
            ]
            return _ScalarResult(rows_inb)
        if "FROM reminders" in sql:
            fids = next(
                (v for k, v in params.items() if isinstance(v, (list, tuple, set))),
                None,
            )
            rows_rem = [
                r
                for r in self.reminders.values()
                if fids is None or (r.fact_id is not None and r.fact_id in fids)
            ]
            return _ScalarResult(rows_rem)
        if "FROM facts" in sql:
            cap_id = next((v for k, v in params.items() if k.startswith("capture_id")), None)
            sup_ids = next(
                (
                    v
                    for k, v in params.items()
                    if "superseded_by_id" in k and isinstance(v, (list, tuple))
                ),
                None,
            )
            if sup_ids is not None:
                return _ScalarResult(
                    [f for f in self.facts.values() if f.superseded_by_id in sup_ids]
                )
            if cap_id is not None:
                return _ScalarResult([f for f in self.facts.values() if f.capture_id == cap_id])
            # Confirmed active facts (for recent context / polarity flip / supersession)
            pid_single = next(
                (v for k, v in params.items() if k.startswith("person_id") and isinstance(v, UUID)),
                None,
            )
            pid_list = next(
                (
                    v
                    for k, v in params.items()
                    if k.startswith("person_id") and isinstance(v, (list, tuple))
                ),
                None,
            )
            excl_id = next((v for k, v in params.items() if k.startswith("id")), None)
            out_facts: list[FactRow] = []
            for f in self.facts.values():
                if f.status != FactStatus.CONFIRMED.value or f.invalid_at is not None:
                    continue
                if pid_single is not None and f.person_id != pid_single:
                    continue
                if pid_list is not None and f.person_id not in pid_list:
                    continue
                if excl_id is not None and f.id == excl_id:
                    continue
                if f.record_id is not None:
                    rec = self.records.get(f.record_id)
                    if (
                        rec is None
                        or rec.deleted_at is not None
                        or rec.review_state == RecordReviewState.HELD.value
                    ):
                        continue
                out_facts.append(f)
            out_facts.sort(key=lambda f: (f.created_at, f.id), reverse=True)
            return _ScalarResult(out_facts)
        return _ScalarResult([])


class _FakeDb:
    def __init__(self, session: _InMemoryCaptureSession) -> None:
        self.session = session

    @asynccontextmanager
    async def user_session(self, _scope: UserScope) -> AsyncIterator[_InMemoryCaptureSession]:
        yield self.session


class _FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put_encrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
        plaintext: bytes,
        content_type: str,
    ) -> None:
        self.blobs[object_key] = (plaintext, content_type)

    async def get_decrypted(
        self,
        *,
        user_id: UUID,
        dek: bytes,
        object_key: str,
    ) -> tuple[bytes, str]:
        return self.blobs[object_key]

    async def delete_object(self, *, object_key: str) -> None:
        self.blobs.pop(object_key, None)


class _FakeIdempotency:
    async def claim_or_replay(
        self,
        session: _InMemoryCaptureSession,
        *,
        scope: UserScope,
        idem_key: str,
        method: str,
        path: str,
        request_hash: str,
    ) -> tuple[IdempotencyKeyRow, None]:
        row = IdempotencyKeyRow(
            id=new_id(),
            user_id=scope.user_id,
            idem_key=idem_key,
            method=method,
            path=path,
            request_hash=request_hash,
            status="in_progress",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(row)
        return row, None

    async def complete(
        self,
        session: _InMemoryCaptureSession,
        row: IdempotencyKeyRow,
        *,
        status_code: int,
        response_body: dict[str, Any],
    ) -> None:
        row.status = "completed"
        row.response_status = status_code
        row.response_body = response_body


@dataclass
class _FakeGateway:
    transcript: str = ""
    duration_ms: int = 5000
    extract_fn: Callable[[ExtractRecordInput], ExtractRecordOutput] | None = None
    transcribe_calls: list[TranscribeInput] = field(default_factory=list)
    extract_calls: list[ExtractRecordInput] = field(default_factory=list)

    async def transcribe(
        self, *, scope: UserScope, payload: TranscribeInput, session: Any = None
    ) -> TranscribeOutput:
        self.transcribe_calls.append(payload)
        return TranscribeOutput(
            raw_transcript=self.transcript,
            corrected_transcript=self.transcript,
            duration_ms=self.duration_ms,
        )

    async def extract_record(
        self, *, scope: UserScope, payload: ExtractRecordInput, session: Any = None
    ) -> tuple[ExtractRecordOutput, str, str]:
        self.extract_calls.append(payload)
        assert self.extract_fn is not None
        return self.extract_fn(payload), "extract_record_v2.0", "fake-unit"


class _FakeContainer:
    def __init__(
        self,
        *,
        session: _InMemoryCaptureSession,
        cipher: EnvelopeCipher,
        dek: bytes,
        clock: ManualClock,
        gateway: _FakeGateway,
    ) -> None:
        self.settings = Settings(environment="test")
        self.db = _FakeDb(session)
        self.cipher = cipher
        self._dek = dek
        self.clock = clock
        self.storage = _FakeStorage()
        self.idempotency = _FakeIdempotency()
        self.extras: dict[str, Any] = {"gateway": gateway}

    async def load_user_dek(self, _session: Any, _scope: UserScope) -> bytes:
        return self._dek


def _seed_in_memory_person(
    session: _InMemoryCaptureSession,
    cipher: EnvelopeCipher,
    dek: bytes,
    scope: UserScope,
    *,
    name: str,
    tier: Tier,
    now: datetime,
    aliases: tuple[str, ...] = (),
) -> UUID:
    pid = new_id()
    p_row = PersonRow(
        id=pid,
        user_id=scope.user_id,
        slug=None,
        tier=tier.value,
        rank="n",
        state=PersonState.ACTIVE.value,
        display_name_enc=cipher.encrypt_str(
            dek,
            name,
            user_id=scope.user_id,
            table="persons",
            column="display_name_enc",
            row_id=pid,
        ),
        monogram=name[:1],
        has_photo=False,
        visual_tokens={},
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(p_row)
    for idx, alias in enumerate((name, *aliases)):
        aid = new_id()
        session.add(
            PersonAliasRow(
                id=aid,
                user_id=scope.user_id,
                person_id=pid,
                alias_hmac=cipher.compute_blind_index(
                    dek,
                    user_id=scope.user_id,
                    domain="alias",
                    normalized_value=alias.strip().lower(),
                ),
                alias_enc=cipher.encrypt_str(
                    dek,
                    alias,
                    user_id=scope.user_id,
                    table="person_aliases",
                    column="alias_enc",
                    row_id=aid,
                ),
                kind="name" if idx == 0 else "nickname",
                is_primary=(idx == 0),
                created_at=now,
            )
        )
    return pid


# ============================================================================
# 1. History-Clue Unique Resolution & Multi-Person Ambiguity (Zero-Bleed)
# ============================================================================


def test_history_clue_unique_resolution_matches_mom_for_knee_recheck() -> None:
    mom = KnownPersonProfile(
        person_id=new_id(),
        display_name="妈妈",
        aliases=("老妈",),
        tier=Tier.SILVER,
        recent_facts=("右膝盖最近总疼，预约了医院复查", "降压药快吃完了"),
    )
    linxi = KnownPersonProfile(
        person_id=new_id(),
        display_name="林溪",
        aliases=("溪溪",),
        tier=Tier.GOLD,
        recent_facts=("喜欢喝桂花乌龙", "严重香菜过敏"),
    )
    ayuan = KnownPersonProfile(
        person_id=new_id(),
        display_name="阿远",
        tier=Tier.CRYSTAL,
        recent_facts=("下个月去北海道滑雪",),
    )
    known = [mom, linxi, ayuan]

    # Direct helper check: "膝盖复查改周六" uniquely overlaps 妈妈 ("膝盖" + "复查")
    direct = resolve_by_history_clues("上次那个膝盖复查改周六了", known)
    assert direct is not None
    assert direct.matched_person_id == mom.person_id
    assert direct.candidate_person_ids == (mom.person_id,)
    assert direct.clause_person_unambiguous is True

    # Full resolve_mention_for_fact in RouteMode.GLOBAL with empty/pronoun mention
    res = resolve_mention_for_fact(
        mention="",
        fact_person_id_hint=None,
        fact_content="膝盖复查改周六",
        full_transcript="上次那个膝盖复查改周六了",
        route_mode=RouteMode.GLOBAL,
        scoped_person_id=None,
        known_people=known,
    )
    assert res.matched_person_id == mom.person_id
    assert res.mention == "妈妈"
    assert res.clause_person_unambiguous is True
    assert res.is_new_person is False


def test_history_clue_moderate_single_token_match_requests_gentle_confirmation() -> None:
    mom = KnownPersonProfile(
        person_id=new_id(),
        display_name="妈妈",
        tier=Tier.SILVER,
        recent_facts=("右膝盖最近总疼",),
    )
    linxi = KnownPersonProfile(
        person_id=new_id(),
        display_name="林溪",
        tier=Tier.GOLD,
        recent_facts=("喜欢喝桂花乌龙",),
    )
    res = resolve_by_history_clues("膝盖记得贴膏药", [mom, linxi])
    assert res is not None
    assert res.matched_person_id == mom.person_id
    assert res.clause_person_unambiguous is False


def test_history_clue_multi_person_ambiguity_never_cross_bleeds() -> None:
    linxi = KnownPersonProfile(
        person_id=new_id(),
        display_name="林溪",
        tier=Tier.GOLD,
        recent_facts=("平时爱喝手冲咖啡", "不能吃香菜"),
    )
    ayuan = KnownPersonProfile(
        person_id=new_id(),
        display_name="阿远",
        tier=Tier.CRYSTAL,
        recent_facts=("早上必喝美式咖啡", "喜欢打羽毛球"),
    )
    mom = KnownPersonProfile(
        person_id=new_id(),
        display_name="妈妈",
        tier=Tier.SILVER,
        recent_facts=("右膝盖复查",),
    )
    known = [linxi, ayuan, mom]

    # Both 林溪 and 阿远 mention "咖啡" -> MUST return ambiguous candidates, never guess!
    amb = resolve_by_history_clues("下次见面记得带杯咖啡", known)
    assert amb is not None
    assert amb.matched_person_id is None
    assert set(amb.candidate_person_ids) == {linxi.person_id, ayuan.person_id}
    assert mom.person_id not in amb.candidate_person_ids
    assert amb.clause_person_unambiguous is False

    # Even if an LLM passes a guess in fact_person_id_hint, deterministic ambiguity wins!
    res = resolve_mention_for_fact(
        mention="她",
        fact_person_id_hint=linxi.person_id,
        fact_content="下次见面记得带杯咖啡",
        full_transcript="她下次见面记得带杯咖啡",
        route_mode=RouteMode.GLOBAL,
        scoped_person_id=None,
        known_people=known,
    )
    assert res.matched_person_id is None
    assert set(res.candidate_person_ids) == {linxi.person_id, ayuan.person_id}


def test_history_clue_ignores_generic_stopwords_and_never_runs_in_current_mode() -> None:
    linxi = KnownPersonProfile(
        person_id=new_id(),
        display_name="林溪",
        tier=Tier.GOLD,
        recent_facts=("最近喜欢吃东西", "明天有事情"),
    )
    mom = KnownPersonProfile(
        person_id=new_id(),
        display_name="妈妈",
        tier=Tier.SILVER,
        recent_facts=("右膝盖医院复查",),
    )
    # Generic stopwords alone never match anyone
    assert resolve_by_history_clues("今天喜欢吃东西，明天还有事情", [linxi, mom]) is None

    # In RouteMode.CURRENT, scoped_person_id stays locked even if another person's clue appears
    scoped_res = resolve_mention_for_fact(
        mention="她",
        fact_person_id_hint=None,
        fact_content="膝盖复查改周六",
        full_transcript="她上次那个膝盖复查改周六了",
        route_mode=RouteMode.CURRENT,
        scoped_person_id=linxi.person_id,
        known_people=[linxi, mom],
    )
    assert scoped_res.matched_person_id == linxi.person_id
    assert scoped_res.scoped_locked is True


# ============================================================================
# 2. Spoken Self-Correction Hygiene (`apply_spoken_self_correction`)
# ============================================================================


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("妈妈周五膝盖复查，不对，是周六", "妈妈周六膝盖复查"),
        ("妈妈下周五复查，不对，是周六", "妈妈下周六复查"),
        ("林溪5月12日生日，记错了，是5月15日", "林溪5月15日生日"),
        ("小陈不能吃香菜，说错了，是阿远", "阿远不能吃香菜"),
        ("明天陪小陈吃饭，说错了，是阿远", "明天陪阿远吃饭"),
        ("给林溪买草莓蛋糕，不对，是抹茶蛋糕", "给林溪买抹茶蛋糕"),
        ("林溪不能吃香菜，不对，是折耳根", "林溪不能吃折耳根"),
        (
            "林溪不吃香菜，妈妈周五复查，不对，是周六，记得提前挂号",
            "林溪不吃香菜，妈妈周六复查，记得提前挂号",
        ),
        # Clean text must NEVER be mutated
        ("今天天气很好，林溪喜欢喝桂花乌龙", "今天天气很好，林溪喜欢喝桂花乌龙"),
        ("他觉得这个方案不对，需要再改一下", "他觉得这个方案不对，需要再改一下"),
        ("她觉得不对，周六再去看看", "她觉得不对，周六再去看看"),
        ("", ""),
    ],
)
def test_apply_spoken_self_correction(spoken: str, expected: str) -> None:
    assert apply_spoken_self_correction(spoken) == expected


# ============================================================================
# 3. Event-Group Partitioning Helper
# ============================================================================


def test_partition_by_event_group_preserves_single_slip_and_splits_distinct_groups() -> None:
    assert partition_by_event_group([], lambda x: None) == [[]]

    # None or single identical group -> exact single-RecordRow behavior
    items_none = [("a", None), ("b", None)]
    assert partition_by_event_group(items_none, lambda x: x[1]) == [items_none]

    items_same = [("a", "g1"), ("b", "g1")]
    assert partition_by_event_group(items_same, lambda x: x[1]) == [items_same]

    items_one_named = [("a", "g1"), ("b", None)]
    assert partition_by_event_group(items_one_named, lambda x: x[1]) == [items_one_named]

    # 2+ distinct non-empty groups -> clean per-event split preserving first-seen order
    items_multi = [("a", "tea"), ("b", "knee"), ("c", "tea")]
    groups = partition_by_event_group(items_multi, lambda x: x[1])
    assert groups == [[("a", "tea"), ("c", "tea")], [("b", "knee")]]


# ============================================================================
# 4. Encrypted Per-Person Context Loader & Contract Builder
# ============================================================================


@pytest.mark.asyncio
async def test_load_recent_person_context_decrypts_dedups_and_enforces_scope_filters() -> None:
    scope = UserScope(user_id=new_id())
    other_scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    session = _InMemoryCaptureSession()
    mom_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="妈妈", tier=Tier.SILVER, now=now
    )
    linxi_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="林溪", tier=Tier.GOLD, now=now
    )

    # Normal record + Held record + Soft-deleted record
    normal_rec = new_id()
    held_rec = new_id()
    deleted_rec = new_id()
    for rid, rev_state, del_at in (
        (normal_rec, RecordReviewState.NORMAL.value, None),
        (held_rec, RecordReviewState.HELD.value, None),
        (deleted_rec, RecordReviewState.NORMAL.value, now),
    ):
        session.add(
            RecordRow(
                id=rid,
                user_id=scope.user_id,
                person_id=mom_id,
                kind="note",
                title_enc=cipher.encrypt_str(
                    dek, "t", user_id=scope.user_id, table="records", column="title_enc", row_id=rid
                ),
                body_enc=cipher.encrypt_str(
                    dek, "b", user_id=scope.user_id, table="records", column="body_enc", row_id=rid
                ),
                review_state=rev_state,
                deleted_at=del_at,
                created_at=now,
                updated_at=now,
            )
        )

    def _add_fact(
        pid: UUID,
        text: str,
        *,
        rec_id: UUID | None = normal_rec,
        status: str = FactStatus.CONFIRMED.value,
        invalid_at: datetime | None = None,
        uid: UUID = scope.user_id,
        offset_sec: int = 0,
    ) -> None:
        fid = new_id()
        session.add(
            FactRow(
                id=fid,
                user_id=uid,
                person_id=pid,
                record_id=rec_id,
                type=FactType.PREFERENCE.value,
                polarity=Polarity.NEUTRAL.value,
                time_status="current",
                source_speaker="narrator",
                content_enc=cipher.encrypt_str(
                    dek, text, user_id=uid, table="facts", column="content_enc", row_id=fid
                ),
                status=status,
                invalid_at=invalid_at,
                created_at=now + timedelta(seconds=offset_sec),
                updated_at=now + timedelta(seconds=offset_sec),
            )
        )

    _add_fact(mom_id, "右膝盖复查", offset_sec=10)
    _add_fact(mom_id, "右膝盖复查", offset_sec=11)  # duplicate -> deduped
    _add_fact(mom_id, "低盐饮食", offset_sec=12)
    _add_fact(mom_id, "未确认待审核", status=FactStatus.PENDING.value, offset_sec=13)
    _add_fact(mom_id, "已失效旧事实", invalid_at=now, offset_sec=14)
    _add_fact(mom_id, "冷静区暂存内容", rec_id=held_rec, offset_sec=15)
    _add_fact(mom_id, "已删除记录内容", rec_id=deleted_rec, offset_sec=16)
    _add_fact(linxi_id, "喜欢桂花乌龙", rec_id=None, offset_sec=20)
    # Cross-tenant row should never leak even if returned
    other_dek_wrapped, other_kek_id = cipher.generate_wrapped_dek(other_scope.user_id)
    other_dek = cipher.load_dek(
        user_id=other_scope.user_id, wrapped_dek=other_dek_wrapped, kek_id=other_kek_id
    )
    fid_other = new_id()
    session.add(
        FactRow(
            id=fid_other,
            user_id=other_scope.user_id,
            person_id=mom_id,
            record_id=None,
            type=FactType.NOTE.value,
            polarity=Polarity.NEUTRAL.value,
            time_status="current",
            source_speaker="narrator",
            content_enc=cipher.encrypt_str(
                other_dek,
                "其他用户秘密",
                user_id=other_scope.user_id,
                table="facts",
                column="content_enc",
                row_id=fid_other,
            ),
            status=FactStatus.CONFIRMED.value,
            created_at=now + timedelta(seconds=30),
            updated_at=now + timedelta(seconds=30),
        )
    )

    facts_by_person, recent_snippets = await load_recent_person_context(
        session,  # type: ignore[arg-type]
        scope,
        dek,
        cipher,
        [mom_id, linxi_id],
        max_facts_per_person=5,
        max_snippets_total=12,
    )

    assert facts_by_person[mom_id] == ["低盐饮食", "右膝盖复查"]
    assert facts_by_person[linxi_id] == ["喜欢桂花乌龙"]
    assert recent_snippets == ["喜欢桂花乌龙", "低盐饮食", "右膝盖复查"]

    # Verify the compiled SQL enforces every scoping/exclusion predicate
    sql = str(session.last_statements[-1].compile(dialect=postgresql.dialect()))
    assert "facts.user_id =" in sql
    assert "facts.status =" in sql
    assert "facts.invalid_at IS NULL" in sql
    assert "persons.deleted_at IS NULL" in sql
    assert "records.deleted_at IS NULL" in sql
    assert "records.review_state !=" in sql


@pytest.mark.asyncio
async def test_scoped_voice_passes_active_person_name_and_prioritized_snippets() -> None:
    clock = ManualClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)
    session = _InMemoryCaptureSession()
    now = clock.now()

    linxi_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="林溪", tier=Tier.GOLD, now=now
    )
    mom_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="妈妈", tier=Tier.SILVER, now=now
    )

    f1 = new_id()
    session.add(
        FactRow(
            id=f1,
            user_id=scope.user_id,
            person_id=linxi_id,
            record_id=None,
            type=FactType.PREFERENCE.value,
            polarity=Polarity.POSITIVE.value,
            time_status="current",
            source_speaker="narrator",
            content_enc=cipher.encrypt_str(
                dek,
                "平时最爱喝桂花乌龙",
                user_id=scope.user_id,
                table="facts",
                column="content_enc",
                row_id=f1,
            ),
            status=FactStatus.CONFIRMED.value,
            version=1,
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=2),
        )
    )
    f2 = new_id()
    session.add(
        FactRow(
            id=f2,
            user_id=scope.user_id,
            person_id=mom_id,
            record_id=None,
            type=FactType.DATE.value,
            polarity=Polarity.NEUTRAL.value,
            time_status="current",
            source_speaker="narrator",
            content_enc=cipher.encrypt_str(
                dek,
                "右膝盖复查",
                user_id=scope.user_id,
                table="facts",
                column="content_enc",
                row_id=f2,
            ),
            status=FactStatus.CONFIRMED.value,
            version=1,
            created_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        )
    )

    capture_id = new_id()
    audio_key = f"{scope.user_id}/captures/{capture_id}/audio.webm"
    session.add(
        CaptureRow(
            id=capture_id,
            user_id=scope.user_id,
            source=CaptureSource.VOICE.value,
            route_mode=RouteMode.CURRENT.value,
            scoped_person_id=linxi_id,
            status=CaptureStatus.RECEIVED.value,
            audio_object_key=audio_key,
            idempotency_key="scoped-voice-asr-hints",
            created_at=now,
        )
    )
    gateway = _FakeGateway(
        transcript="她还是喜欢喝热桂花乌龙",
        extract_fn=lambda _inp: ExtractRecordOutput(
            facts=[
                _make_fact(
                    person_mention="她",
                    person_id=linxi_id,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="茶饮偏好",
                    content="喜欢喝热桂花乌龙",
                )
            ]
        ),
    )
    container = _FakeContainer(
        session=session, cipher=cipher, dek=dek, clock=clock, gateway=gateway
    )
    await container.storage.put_encrypted(
        user_id=scope.user_id,
        dek=dek,
        object_key=audio_key,
        plaintext=b"\x1a\x45\xdf\xa3webm",
        content_type="audio/webm",
    )
    svc = CapturePipelineService(container)  # type: ignore[arg-type]
    await svc.process_capture_by_id(scope=scope, capture_id=capture_id)

    trans_call = gateway.transcribe_calls[0]
    assert trans_call.active_person_name == "林溪"
    # Active person's facts are prioritized first in ASR recent_context_snippets
    assert trans_call.recent_context_snippets[0] == "平时最爱喝桂花乌龙"
    assert "右膝盖复查" in trans_call.recent_context_snippets


# ============================================================================
# 5. Full Pipeline Unit Tests: Multi-Person Split, Multi-Event Split, Undo,
#    and History-Grounded Voice Self-Correction
# ============================================================================


@pytest.mark.asyncio
async def test_multi_person_split_three_people_zero_cross_person_text_leakage() -> None:
    clock = ManualClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)

    session = _InMemoryCaptureSession()
    now = clock.now()
    linxi_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="林溪", tier=Tier.GOLD, now=now
    )
    mom_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="妈妈", tier=Tier.SILVER, now=now
    )
    ayuan_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="阿远", tier=Tier.CRYSTAL, now=now
    )

    utterance = "林溪不能吃香菜，妈妈下周六膝盖复查，阿远爱喝桂花乌龙"
    capture_id = new_id()
    session.add(
        CaptureRow(
            id=capture_id,
            user_id=scope.user_id,
            source=CaptureSource.TEXT.value,
            route_mode=RouteMode.GLOBAL.value,
            status=CaptureStatus.RECEIVED.value,
            raw_text_enc=cipher.encrypt_str(
                dek,
                utterance,
                user_id=scope.user_id,
                table="captures",
                column="raw_text_enc",
                row_id=capture_id,
            ),
            idempotency_key="multi-person-3",
            created_at=now,
        )
    )

    gateway = _FakeGateway(
        extract_fn=lambda _inp: ExtractRecordOutput(
            facts=[
                _make_fact(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="饮食忌口",
                    content="不能吃香菜",
                ),
                _make_fact(
                    person_mention="妈妈",
                    person_id=mom_id,
                    fact_type=FactType.DATE,
                    polarity=Polarity.NEUTRAL,
                    title="膝盖复查",
                    content="下周六去医院做膝盖复查",
                    trigger_date="2026-11-28",
                ),
                _make_fact(
                    person_mention="阿远",
                    person_id=ayuan_id,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="茶饮偏好",
                    content="爱喝桂花乌龙",
                ),
            ]
        )
    )
    container = _FakeContainer(
        session=session, cipher=cipher, dek=dek, clock=clock, gateway=gateway
    )
    svc = CapturePipelineService(container)  # type: ignore[arg-type]

    resp = await svc.process_capture_by_id(scope=scope, capture_id=capture_id)
    assert resp.status is CaptureStatus.COMPLETED
    assert len(resp.splits) == 3
    assert len(session.records) == 3

    by_name = {s.person_name: s for s in resp.splits}
    assert by_name["林溪"].body == "不能吃香菜"
    assert by_name["妈妈"].body == "下周六去医院做膝盖复查"
    assert by_name["阿远"].body == "爱喝桂花乌龙"

    # Zero cross-person text leakage across decrypted DB RecordRows, and newly captured pinned=False
    for split in resp.splits:
        rec = session.records[split.material_id]
        assert rec.pinned is False
        _, body, _ = decrypt_record_fields(cipher, dek, rec)
        if split.person_id == linxi_id:
            assert "香菜" in body
            assert "膝盖" not in body and "桂花乌龙" not in body
        elif split.person_id == mom_id:
            assert "膝盖复查" in body
            assert "香菜" not in body and "桂花乌龙" not in body
        elif split.person_id == ayuan_id:
            assert "桂花乌龙" in body
            assert "香菜" not in body and "膝盖" not in body


@pytest.mark.asyncio
async def test_multi_event_split_for_single_person_and_atomic_5s_undo() -> None:
    clock = ManualClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)

    session = _InMemoryCaptureSession()
    now = clock.now()
    mom_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="妈妈", tier=Tier.SILVER, now=now
    )

    utterance = "妈妈平时最爱喝桂花乌龙，另外下周六要去医院做膝盖复查"
    capture_id = new_id()
    session.add(
        CaptureRow(
            id=capture_id,
            user_id=scope.user_id,
            source=CaptureSource.TEXT.value,
            route_mode=RouteMode.CURRENT.value,
            scoped_person_id=mom_id,
            status=CaptureStatus.RECEIVED.value,
            raw_text_enc=cipher.encrypt_str(
                dek,
                utterance,
                user_id=scope.user_id,
                table="captures",
                column="raw_text_enc",
                row_id=capture_id,
            ),
            idempotency_key="multi-event-mom",
            created_at=now,
        )
    )

    gateway = _FakeGateway(
        extract_fn=lambda _inp: ExtractRecordOutput(
            facts=[
                _make_fact(
                    person_mention="妈妈",
                    person_id=mom_id,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="茶饮喜好",
                    content="平时最爱喝桂花乌龙",
                    event_group="tea_preference",
                ),
                _make_fact(
                    person_mention="妈妈",
                    person_id=mom_id,
                    fact_type=FactType.DATE,
                    polarity=Polarity.NEUTRAL,
                    title="膝盖复查",
                    content="下周六去医院做膝盖复查",
                    trigger_date="2026-11-28",
                    event_group="knee_checkup",
                ),
            ]
        )
    )
    container = _FakeContainer(
        session=session, cipher=cipher, dek=dek, clock=clock, gateway=gateway
    )
    svc = CapturePipelineService(container)  # type: ignore[arg-type]

    resp = await svc.process_capture_by_id(scope=scope, capture_id=capture_id)
    assert resp.status is CaptureStatus.COMPLETED
    # 2 distinct event_groups for 妈妈 -> 2 separate RecordRow slips!
    assert len(resp.splits) == 2
    assert len(session.records) == 2
    assert len(session.facts) == 2
    assert len({s.material_id for s in resp.splits}) == 2
    assert all(s.person_id == mom_id for s in resp.splits)

    tea_split, knee_split = resp.splits[0], resp.splits[1]
    assert tea_split.body == "平时最爱喝桂花乌龙"
    assert "膝盖" not in tea_split.body
    assert tea_split.presentation is Presentation.INK
    assert len(tea_split.fact_ids) == 1

    assert knee_split.body == "下周六去医院做膝盖复查"
    assert "桂花乌龙" not in knee_split.body
    assert knee_split.presentation is Presentation.PENCIL
    assert knee_split.glance_hint == "11月28日 · 点一下确认日期"
    assert len(knee_split.fact_ids) == 1

    for rec in session.records.values():
        assert rec.capture_id == capture_id
        assert rec.pinned is False

    # Atomic 5-second undo reverts BOTH RecordRow slips, BOTH FactRows, and cancels reminders!
    clock.advance(seconds=2)
    undo_resp, _ = await svc.undo_capture(
        scope=scope,
        capture_id=capture_id,
        idem_key="undo-multi-event",
        route_path=f"/v1/captures/{capture_id}:undo",
    )
    assert undo_resp is not None
    assert undo_resp.status is CaptureStatus.REVERTED
    assert undo_resp.reverted_record_count == 2
    assert undo_resp.rejected_fact_count == 2
    assert undo_resp.cancelled_reminder_count == 1
    assert all(r.deleted_at is not None for r in session.records.values())
    assert all(f.status == FactStatus.REJECTED.value for f in session.facts.values())


@pytest.mark.asyncio
async def test_single_event_group_or_none_preserves_single_record_row() -> None:
    clock = ManualClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)

    session = _InMemoryCaptureSession()
    now = clock.now()
    linxi_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="林溪", tier=Tier.GOLD, now=now
    )

    capture_id = new_id()
    session.add(
        CaptureRow(
            id=capture_id,
            user_id=scope.user_id,
            source=CaptureSource.TEXT.value,
            route_mode=RouteMode.CURRENT.value,
            scoped_person_id=linxi_id,
            status=CaptureStatus.RECEIVED.value,
            raw_text_enc=cipher.encrypt_str(
                dek,
                "林溪不吃香菜也不吃折耳根",
                user_id=scope.user_id,
                table="captures",
                column="raw_text_enc",
                row_id=capture_id,
            ),
            idempotency_key="single-group-compat",
            created_at=now,
        )
    )
    gateway = _FakeGateway(
        extract_fn=lambda _inp: ExtractRecordOutput(
            facts=[
                _make_fact(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="忌口",
                    content="不吃香菜",
                    event_group=None,
                ),
                _make_fact(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="忌口",
                    content="不吃折耳根",
                    event_group=None,
                ),
            ]
        )
    )
    container = _FakeContainer(
        session=session, cipher=cipher, dek=dek, clock=clock, gateway=gateway
    )
    svc = CapturePipelineService(container)  # type: ignore[arg-type]

    resp = await svc.process_capture_by_id(scope=scope, capture_id=capture_id)
    assert len(resp.splits) == 1
    assert len(session.records) == 1
    assert resp.splits[0].body == "不吃香菜；不吃折耳根"
    assert len(resp.splits[0].fact_ids) == 2


@pytest.mark.asyncio
async def test_voice_pipeline_loads_history_masks_pii_self_corrects_and_routes_by_history() -> None:
    clock = ManualClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC))
    scope = UserScope(user_id=new_id())
    cipher, dek = _build_cipher_and_dek(scope.user_id)

    session = _InMemoryCaptureSession()
    now = clock.now()
    mom_id = _seed_in_memory_person(
        session, cipher, dek, scope, name="妈妈", tier=Tier.SILVER, now=now
    )
    _seed_in_memory_person(session, cipher, dek, scope, name="林溪", tier=Tier.GOLD, now=now)

    # Seed historical confirmed fact for 妈妈 containing both knee clue and a hard-PII secret
    hist_fid = new_id()
    session.add(
        FactRow(
            id=hist_fid,
            user_id=scope.user_id,
            person_id=mom_id,
            record_id=None,
            type=FactType.DATE.value,
            polarity=Polarity.NEUTRAL.value,
            time_status="current",
            source_speaker="narrator",
            content_enc=cipher.encrypt_str(
                dek,
                "右膝盖最近总疼，预约医院复查，银行卡密码是 88889999",
                user_id=scope.user_id,
                table="facts",
                column="content_enc",
                row_id=hist_fid,
            ),
            status=FactStatus.CONFIRMED.value,
            version=1,
            created_at=now - timedelta(days=3),
            updated_at=now - timedelta(days=3),
        )
    )

    capture_id = new_id()
    audio_key = f"{scope.user_id}/captures/{capture_id}/audio.webm"
    session.add(
        CaptureRow(
            id=capture_id,
            user_id=scope.user_id,
            source=CaptureSource.VOICE.value,
            route_mode=RouteMode.GLOBAL.value,
            status=CaptureStatus.RECEIVED.value,
            audio_object_key=audio_key,
            idempotency_key="voice-history-clue",
            created_at=now,
        )
    )

    # Extractor deliberately leaves person_mention="" and person_id=None so deterministic history clues resolve it!
    gateway = _FakeGateway(
        transcript="上次那个膝盖复查改周五了，不对，是周六",
        duration_ms=4200,
        extract_fn=lambda inp: ExtractRecordOutput(
            facts=[
                _make_fact(
                    person_mention="",
                    person_id=None,
                    fact_type=FactType.DATE,
                    polarity=Polarity.NEUTRAL,
                    title="复查改期",
                    content=inp.text,
                    trigger_date="2026-11-28",
                )
            ]
        ),
    )
    container = _FakeContainer(
        session=session, cipher=cipher, dek=dek, clock=clock, gateway=gateway
    )
    await container.storage.put_encrypted(
        user_id=scope.user_id,
        dek=dek,
        object_key=audio_key,
        plaintext=b"\x1a\x45\xdf\xa3webm",
        content_type="audio/webm",
    )
    svc = CapturePipelineService(container)  # type: ignore[arg-type]

    resp = await svc.process_capture_by_id(scope=scope, capture_id=capture_id)

    # Spoken self-correction repaired "周五 -> 周六" before extraction & persistence
    assert resp.transcript == "上次那个膝盖复查改周六了"
    assert len(gateway.extract_calls) == 1
    assert gateway.extract_calls[0].text == "上次那个膝盖复查改周六了"

    # Verify real contract fields carry PII-masked historical context to ASR and Extractor
    trans_call = gateway.transcribe_calls[0]
    assert trans_call.active_person_name is None
    joined_asr = " ".join(trans_call.recent_context_snippets)
    assert "右膝盖" in joined_asr
    assert "88889999" not in joined_asr
    assert "[REDACTED_SECRET]" in joined_asr

    ext_call = gateway.extract_calls[0]
    joined_hints = " ".join(ext_call.recent_history_hints)
    assert "妈妈：" in joined_hints
    assert "右膝盖" in joined_hints
    assert "88889999" not in joined_hints
    assert "[REDACTED_SECRET]" in joined_hints

    mom_ref = next(kp for kp in ext_call.known_persons if kp.person_id == mom_id)
    joined_pf = " ".join(mom_ref.recent_facts)
    assert "右膝盖" in joined_pf
    assert "88889999" not in joined_pf
    assert "[REDACTED_SECRET]" in joined_pf

    # History-clue resolution deterministically routed unnamed global voice memo to 妈妈!
    assert len(resp.splits) == 1
    assert resp.splits[0].person_id == mom_id
    assert resp.splits[0].person_name == "妈妈"
    assert resp.splits[0].body == "上次那个膝盖复查改周六了"
    assert resp.splits[0].presentation is Presentation.PENCIL
    assert resp.splits[0].glance_hint == "11月28日 · 点一下确认日期"
    # Bi-temporal supersession preserved history by marking the old fact past/invalidated
    assert session.facts[hist_fid].invalid_at == now
    assert session.facts[hist_fid].superseded_by_id == resp.splits[0].fact_ids[0]
