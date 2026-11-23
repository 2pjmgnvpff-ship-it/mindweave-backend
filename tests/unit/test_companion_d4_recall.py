"""Unit tests for Companion D4 BM25 raw record recall, SSE writeback receipt, runtime AI status, and demo seeder."""

from __future__ import annotations

import io
import json
import wave
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport
from scripts.seed_demo import (
    FORBIDDEN_PROTOTYPE_NAMES,
    build_demo_personas,
    build_demo_wav_bytes,
)

from mindweave.core.config import DEFAULT_DEV_KEK_HEX, Settings
from mindweave.core.crypto import EnvelopeCipher, EnvKekProvider
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.core.ids import new_id
from mindweave.core.orm import PersonRow, RecordRow
from mindweave.main import create_app
from mindweave.modules.companion.application.dto import (
    ChatMessageDto,
    ChatTurnResponse,
    CrisisCardDto,
)
from mindweave.modules.companion.application.service import CompanionService
from mindweave.modules.companion.domain.tier_policy import CRISIS_SELF_HARM_CARD_V1
from mindweave.modules.companion.infra.context_builder import (
    ContextBuilder,
    RecallCandidateRecord,
    recall_relevant_records,
    tokenize_zh_bm25,
)
from mindweave.shared.enums import (
    ChatRole,
    PersonState,
    RecordKind,
    RecordReviewState,
    SafetyCategory,
    Tier,
)
from mindweave.shared.scopes import PersonScope


def _parse_sse_frames(raw_frames: list[str]) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in raw_frames:
        event_name = "message"
        data_str = "{}"
        for line in block.strip().splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_str = line.removeprefix("data:").strip()
        parsed.append((event_name, json.loads(data_str)))
    return parsed


def _build_22_record_corpus() -> tuple[list[RecallCandidateRecord], dict[str, UUID]]:
    """Build 22+ realistic records (including all 16 mom demo slips + 6 decoys) for BM25 ranking tests."""
    personas = build_demo_personas()
    mom = next(p for p in personas if p.slug == "demo-mom")

    candidates: list[RecallCandidateRecord] = []
    ids_by_tag: dict[str, UUID] = {}

    for slip in mom.slips:
        rid = new_id()
        if "浅驼色羊绒围巾" in slip.body:
            ids_by_tag["scarf"] = rid
        if "糖醋排骨" in slip.title:
            ids_by_tag["ribs"] = rid
        if "骨科复查" in slip.title:
            ids_by_tag["knee"] = rid
        candidates.append(
            RecallCandidateRecord(
                record_id=rid,
                kind=slip.kind.value,
                title=slip.title,
                body=slip.body,
                snippet=slip.body[:60],
                happened_on=slip.happened_on,
                pinned=slip.pinned,
                created_at=datetime(
                    slip.happened_on.year, slip.happened_on.month, slip.happened_on.day, tzinfo=UTC
                ),
            )
        )

    # Add 6 realistic decoy slips (including one mentioning "冬至" without scarf & one mentioning "排骨汤")
    decoys = [
        (
            date(2026, 11, 19),
            "冬至吃饺子还是汤圆",
            "今年冬至家里商量包韭菜鸡蛋饺子，顺便煮一锅醪糟小圆子。",
            False,
        ),
        (
            date(2026, 11, 18),
            "冬天保暖帽和手套清单",
            "换季整理衣柜发现去年买的手套少了一只，周末顺路再补一副深灰色毛线手套。",
            False,
        ),
        (
            date(2026, 11, 12),
            "莲藕排骨汤炖煮火候",
            "周末炖了一锅粉藕排骨汤，只放姜片和少许盐，汤色奶白鲜甜。",
            False,
        ),
        (
            date(2026, 10, 15),
            "小区门口干洗店取大衣",
            "把驼色羊毛大衣送到西门干洗店护理，周五凭小票去取。",
            False,
        ),
        (
            date(2026, 9, 2),
            "老家快递地址更新",
            "小区新建了东门驿站，以后寄水果和茶叶直接写东门快递柜更方便。",
            False,
        ),
        (
            date(2026, 8, 5),
            "客厅换了暖光护眼落地灯",
            "给沙发旁边添了一盏3000K暖光落地灯，晚上织毛衣看报纸不费眼睛。",
            False,
        ),
    ]
    for d_on, title, body, pinned in decoys:
        candidates.append(
            RecallCandidateRecord(
                record_id=new_id(),
                kind=RecordKind.NOTE.value,
                title=title,
                body=body,
                snippet=body[:60],
                happened_on=d_on,
                pinned=pinned,
                created_at=datetime(d_on.year, d_on.month, d_on.day, tzinfo=UTC),
            )
        )

    assert len(candidates) == len(mom.slips) + len(decoys)
    assert len(candidates) >= 22
    return candidates, ids_by_tag


def test_bm25_recalls_camel_cashmere_scarf_ranked_first_out_of_22_records() -> None:
    candidates, ids = _build_22_record_corpus()

    recalled = recall_relevant_records(
        "她去年冬至提过想要什么围巾",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )

    assert recalled, "Expected scarf wish slip to be recalled"
    assert len(recalled) <= 5
    assert recalled[0].candidate.record_id == ids["scarf"]
    assert "浅驼色羊绒围巾" in recalled[0].candidate.body
    assert recalled[0].score > 0.0


def test_bm25_recalls_sweet_and_sour_pork_ribs_recipe_ranked_first() -> None:
    candidates, ids = _build_22_record_corpus()

    recalled = recall_relevant_records(
        "糖醋排骨怎么调汁",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )

    assert recalled, "Expected sweet-and-sour pork ribs slip to be recalled"
    assert recalled[0].candidate.record_id == ids["ribs"]
    assert "1勺料酒、2勺生抽、3勺白糖、4勺陈醋、5勺清水" in recalled[0].candidate.body


def test_bm25_zero_overlap_and_stopword_only_queries_return_empty() -> None:
    candidates, _ = _build_22_record_corpus()

    # Zero lexical overlap -> strictly empty (no random noise)
    assert (
        recall_relevant_records(
            "量子纠缠与火星殖民飞船",
            candidates,
            top_k=5,
            reference_date=date(2026, 11, 23),
        )
        == []
    )
    # Stop-characters & punctuation only -> empty token list -> strictly empty
    assert tokenize_zh_bm25("的她了吗？什么怎么一下！") == []
    assert recall_relevant_records("的她了吗？什么怎么一下！", candidates) == []
    assert recall_relevant_records("   ", candidates) == []


def test_bm25_excludes_held_venting_and_soft_deleted_records() -> None:
    normal_id = new_id()
    held_id = new_id()
    deleted_id = new_id()
    now_utc = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)

    candidates = [
        RecallCandidateRecord(
            record_id=held_id,
            kind=RecordKind.NOTE.value,
            title="浅驼色羊绒围巾吵架气话暂存",
            body="浅驼色羊绒围巾 浅驼色羊绒围巾 气话暂存绝对不能进AI",
            snippet="气话暂存",
            happened_on=date(2026, 11, 22),
            pinned=True,
            review_state=RecordReviewState.HELD.value,
            deleted_at=None,
        ),
        RecallCandidateRecord(
            record_id=deleted_id,
            kind=RecordKind.NOTE.value,
            title="已删除浅驼色羊绒围巾",
            body="浅驼色羊绒围巾 已软删除记录绝不可召回",
            snippet="已删除",
            happened_on=date(2026, 11, 22),
            pinned=True,
            review_state=RecordReviewState.NORMAL.value,
            deleted_at=now_utc,
        ),
        RecallCandidateRecord(
            record_id=normal_id,
            kind=RecordKind.NOTE.value,
            title="冬至浅驼色羊绒围巾心愿",
            body="去年冬至路过橱窗她摸着浅驼色羊绒围巾说软软的真暖和",
            snippet="冬至围巾",
            happened_on=date(2025, 12, 21),
            pinned=False,
            review_state=RecordReviewState.NORMAL.value,
            deleted_at=None,
        ),
    ]

    recalled = recall_relevant_records(
        "浅驼色羊绒围巾",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )

    recalled_ids = [r.candidate.record_id for r in recalled]
    assert recalled_ids == [normal_id]
    assert held_id not in recalled_ids
    assert deleted_id not in recalled_ids


def test_bm25_respects_exclude_ids_mixed_ascii_dates_and_recency_pinned_tiebreakers() -> None:
    old_unpinned_id = new_id()
    recent_unpinned_id = new_id()
    recent_pinned_id = new_id()
    email_id = new_id()

    candidates = [
        RecallCandidateRecord(
            record_id=old_unpinned_id,
            kind=RecordKind.NOTE.value,
            title="桂花乌龙备忘",
            body="喜欢清香桂花乌龙茶",
            snippet="桂花乌龙",
            happened_on=date(2025, 12, 1),
            pinned=False,
        ),
        RecallCandidateRecord(
            record_id=recent_unpinned_id,
            kind=RecordKind.NOTE.value,
            title="桂花乌龙备忘",
            body="喜欢清香桂花乌龙茶",
            snippet="桂花乌龙",
            happened_on=date(2026, 11, 20),
            pinned=False,
        ),
        RecallCandidateRecord(
            record_id=recent_pinned_id,
            kind=RecordKind.NOTE.value,
            title="桂花乌龙备忘",
            body="喜欢清香桂花乌龙茶",
            snippet="桂花乌龙",
            happened_on=date(2026, 11, 20),
            pinned=True,
        ),
        RecallCandidateRecord(
            record_id=email_id,
            kind=RecordKind.NOTE.value,
            title="Q3需求冻结邮件",
            body="10月9日16:22收到陈经理Q3交付范围冻结确认邮件",
            snippet="Q3邮件",
            happened_on=date(2026, 10, 9),
            pinned=False,
        ),
    ]

    # Identical text tie-break: pinned+recent > recent > old
    ranked = recall_relevant_records(
        "桂花乌龙茶",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )
    assert [r.candidate.record_id for r in ranked] == [
        recent_pinned_id,
        recent_unpinned_id,
        old_unpinned_id,
    ]

    # Excluding already-attached record omits it cleanly
    ranked_excl = recall_relevant_records(
        "桂花乌龙茶",
        candidates,
        top_k=5,
        exclude_record_ids={recent_pinned_id},
        reference_date=date(2026, 11, 23),
    )
    assert [r.candidate.record_id for r in ranked_excl] == [
        recent_unpinned_id,
        old_unpinned_id,
    ]

    # Mixed ASCII + digit date normalization ("09" -> "9", "Q3" -> "q3")
    email_hits = recall_relevant_records(
        "10月9日Q3确认邮件",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )
    assert email_hits and email_hits[0].candidate.record_id == email_id


def _make_turn(
    *,
    tier: Tier,
    user_text: str,
    asst_text: str = "手帐里记着这些小细节。",
    perspectives: list[str] | None = None,
    safety_category: SafetyCategory = SafetyCategory.NORMAL,
    crisis_card: CrisisCardDto | None = None,
) -> ChatTurnResponse:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    person_id = new_id()
    thread_id = new_id()
    user_msg = ChatMessageDto(
        id=new_id(),
        person_id=person_id,
        thread_id=thread_id,
        role=ChatRole.USER,
        content=user_text,
        tier_snapshot=tier,
        safety_category=safety_category,
        created_at=now,
    )
    asst_msg = ChatMessageDto(
        id=new_id(),
        person_id=person_id,
        thread_id=thread_id,
        role=ChatRole.ASSISTANT,
        content=asst_text,
        whisper_note="留好记录最安心。",
        perspectives=list(perspectives or []),
        suggestions=["发一条简短确认消息"],
        tier_snapshot=tier,
        safety_category=safety_category,
        created_at=now + timedelta(seconds=1),
    )
    return ChatTurnResponse(
        message_id=asst_msg.id,
        user_message=user_msg,
        assistant_message=asst_msg,
        tier_snapshot=tier,
        has_unrecorded_gap=False,
        cited_material_ids=[],
        crisis_card=crisis_card,
    )


@pytest.mark.asyncio
async def test_sse_replay_emits_writeback_queued_and_enforces_silver_crystal_no_perspective() -> (
    None
):
    # 1. Declarative GOLD turn -> writebackQueued=True AND perspective emitted
    gold_declarative = _make_turn(
        tier=Tier.GOLD,
        user_text="顺便记一下妈妈最近改喝桂花乌龙茶了",
        perspectives=["也许她最近偏好清淡不刮胃的茶饮。"],
    )
    gold_frames = _parse_sse_frames(
        [frame async for frame in CompanionService._replay_turn_as_sse(gold_declarative)]
    )
    assert any(name == "perspective" for name, _ in gold_frames)
    gold_done = dict(gold_frames)["done"]
    assert gold_done["writebackQueued"] is True

    # 2. Pure question GOLD turn -> no writebackQueued
    gold_question = _make_turn(
        tier=Tier.GOLD,
        user_text="她平时爱喝什么茶？",
    )
    q_frames = _parse_sse_frames(
        [frame async for frame in CompanionService._replay_turn_as_sse(gold_question)]
    )
    q_done = dict(q_frames)["done"]
    assert "writebackQueued" not in q_done

    # 3. SILVER & CRYSTAL tiers -> NEVER emit `perspective` SSE event even if DTO holds items!
    for restricted_tier in (Tier.SILVER, Tier.CRYSTAL):
        turn = _make_turn(
            tier=restricted_tier,
            user_text="陈经理今天周五17:45又发邮件临时加需求了",
            perspectives=["换位思考一下对方可能也很忙。"],
        )
        frames = _parse_sse_frames(
            [frame async for frame in CompanionService._replay_turn_as_sse(turn)]
        )
        assert all(name != "perspective" for name, _ in frames)
        assert dict(frames)["done"]["writebackQueued"] is True

    # 4. Crisis turn -> emits crisis + done, NEVER writebackQueued
    crisis_turn = _make_turn(
        tier=Tier.GOLD,
        user_text="我觉得活着没有意义",
        safety_category=SafetyCategory.CRISIS,
        crisis_card=CrisisCardDto.from_payload(CRISIS_SELF_HARM_CARD_V1),
    )
    c_frames = _parse_sse_frames(
        [frame async for frame in CompanionService._replay_turn_as_sse(crisis_turn)]
    )
    assert [name for name, _ in c_frames] == ["crisis", "done"]
    assert "writebackQueued" not in c_frames[1][1]


@pytest.mark.asyncio
async def test_meta_ai_status_endpoint_is_zero_secret_and_reports_live_provider_readiness() -> None:
    # Case A: fake provider -> liveModelConfigured=False
    fake_settings = Settings(
        environment="test",
        ai_primary_provider="fake",
        ai_fallback_provider="none",
        gemini_api_key="",
        deepseek_api_key="",
    )
    app_fake = create_app(settings=fake_settings)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_fake),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/v1/meta/ai-status")
        assert resp.status_code == 200
        assert resp.json() == {
            "environment": "test",
            "aiPrimaryProvider": "fake",
            "aiFallbackProvider": "none",
            "liveModelConfigured": False,
            "voiceAsrReady": True,
        }
        # Alias /api/v1/meta/ai-status and /healthz remain intact
        alias_resp = await client.get("/api/v1/meta/ai-status")
        assert alias_resp.status_code == 200
        assert alias_resp.json() == resp.json()
        assert (await client.get("/healthz")).status_code == 200

    # Case B: live provider WITH API key -> liveModelConfigured=True and NEVER leaks any secret!
    secret_key = "AIza-SUPER-SECRET-NEVER-LEAK-987654321"
    live_settings = Settings(
        environment="test",
        ai_primary_provider="gemini",
        ai_fallback_provider="deepseek",
        gemini_api_key=secret_key,
        deepseek_api_key="",
    )
    app_live = create_app(settings=live_settings)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_live),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/v1/meta/ai-status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "environment": "test",
            "aiPrimaryProvider": "gemini",
            "aiFallbackProvider": "deepseek",
            "liveModelConfigured": True,
            "voiceAsrReady": True,
        }
        raw_text = resp.text
        assert secret_key not in raw_text
        assert live_settings.jwt_secret not in raw_text
        assert live_settings.kek_hex not in raw_text
        assert "127.0.0.1" not in raw_text

    # Case C: live provider WITHOUT API key -> liveModelConfigured=False
    unconfigured_live = Settings(
        environment="test",
        ai_primary_provider="deepseek",
        ai_fallback_provider="none",
        deepseek_api_key="   ",
    )
    app_unconf = create_app(settings=unconfigured_live)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_unconf),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/v1/meta/ai-status")
        assert resp.status_code == 200
        assert resp.json()["liveModelConfigured"] is False


def test_seed_demo_personas_and_wav_blob_invariants() -> None:
    personas = build_demo_personas()
    by_slug = {p.slug: p for p in personas}

    assert set(by_slug.keys()) == {
        "demo-mom",
        "demo-ayuan",
        "demo-mgr-chen",
        "demo-dr-lin",
        "demo-zhang-shifu",
        "demo-designer-zhou",
        "demo-teahouse-owner",
    }

    # Verify mom: 18 slips (2025-12 to 2026-11), live lunar D1 pencil draft, voice slip, pinned slips
    mom = by_slug["demo-mom"]
    assert mom.name_cn == "妈妈"
    assert mom.tier is Tier.GOLD
    assert len(mom.slips) == 18
    assert min(s.happened_on for s in mom.slips) == date(2025, 12, 21)
    assert max(s.happened_on for s in mom.slips) >= date(2026, 11, 20)
    assert any(s.kind is RecordKind.VOICE and s.has_demo_audio for s in mom.slips)
    assert any(s.pinned and "浅驼色羊绒围巾" in s.body for s in mom.slips)
    assert any(s.pinned and "糖醋排骨" in s.title for s in mom.slips)
    mom_pencils = [f for s in mom.slips for f in s.facts if f.needs_glance]
    assert len(mom_pencils) == 1
    assert mom_pencils[0].is_lunar is True
    assert (mom_pencils[0].lunar_month, mom_pencils[0].lunar_day) == (5, 12)

    # Verify 阿远: 6 slips, pinned beach film trip, March 14 birthday D1 pencil draft
    ayuan = by_slug["demo-ayuan"]
    assert ayuan.name_cn == "阿远"
    assert ayuan.tier is Tier.GOLD
    assert len(ayuan.slips) == 6
    assert any(s.pinned and "12月6日" in s.title for s in ayuan.slips)
    ayuan_pencils = [f for s in ayuan.slips for f in s.facts if f.needs_glance]
    assert len(ayuan_pencils) == 1
    assert ayuan_pencils[0].trigger_date == date(2027, 3, 14)

    # Verify 陈经理: 6 strictly factual workplace slips, ZERO photo slips
    chen = by_slug["demo-mgr-chen"]
    assert chen.name_cn == "陈经理"
    assert chen.tier is Tier.SILVER
    assert len(chen.slips) == 6
    assert all(s.kind is not RecordKind.PHOTO for s in chen.slips)
    assert any("16:22" in s.body for s in chen.slips)
    assert any("Q3复盘" in s.body for s in chen.slips)
    assert any(s.pinned and "17:45" in s.body for s in chen.slips)

    # Verify 4 crystal contacts each with 1-2 crisp notes
    crystal_personas = [p for p in personas if p.tier is Tier.CRYSTAL]
    assert {p.name_cn for p in crystal_personas} == {
        "林医生",
        "物业张师傅",
        "设计师小周",
        "茶室老板娘",
    }
    assert all(1 <= len(p.slips) <= 2 for p in crystal_personas)

    # Verify ZERO forbidden prototype names or dev jargon anywhere
    all_blob = json.dumps(
        [
            (
                p.name_cn,
                p.role_tag,
                p.context_sub,
                p.short_memo,
                p.traits,
                [(s.title, s.body, [f.content for f in s.facts]) for s in p.slips],
            )
            for p in personas
        ],
        ensure_ascii=False,
    )
    for bad_name in FORBIDDEN_PROTOTYPE_NAMES:
        assert bad_name not in all_blob
    for bad_phrase in FORBIDDEN_UI_DEV_PHRASES:
        assert bad_phrase not in all_blob
    assert "铅笔草稿" not in all_blob

    # Verify real valid PCM WAV blob
    wav_bytes = build_demo_wav_bytes()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000
        assert wf.getnframes() >= 16000


def test_bm25_single_char_tea_query_recalls_osmanthus_oolong() -> None:
    candidates, _ = _build_22_record_corpus()

    recalled = recall_relevant_records(
        "她平时爱喝什么茶？",
        candidates,
        top_k=5,
        reference_date=date(2026, 11, 23),
    )

    assert recalled
    assert any("桂花乌龙" in r.candidate.body for r in recalled[:2])


class _FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


class _FakeContextSession:
    """Minimal AsyncSession stub for pure unit-testing `ContextBuilder.build()` encryption & recall."""

    def __init__(
        self,
        *,
        person_row: PersonRow,
        attached_rows: list[RecordRow],
        recall_rows: list[RecordRow],
    ) -> None:
        self._person_row = person_row
        self._attached_rows = attached_rows
        self._recall_rows = recall_rows
        self._scalar_calls = 0
        self._scalars_calls = 0

    async def scalar(self, _stmt: Any) -> Any:
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            return self._person_row
        # Pref row & thread row -> None (defaults)
        return None

    async def scalars(self, _stmt: Any) -> _FakeScalarResult:
        self._scalars_calls += 1
        if self._scalars_calls == 1 and self._attached_rows:
            return _FakeScalarResult(self._attached_rows)
        if (self._scalars_calls == 2 and self._attached_rows) or (
            self._scalars_calls == 1 and not self._attached_rows
        ):
            return _FakeScalarResult(self._recall_rows)
        # Recent chat messages -> empty
        return _FakeScalarResult([])

    async def execute(self, _stmt: Any) -> _FakeScalarResult:
        # Confirmed fact pairs -> empty (proves raw slip recall works without facts!)
        return _FakeScalarResult([])


@pytest.mark.asyncio
async def test_context_builder_build_decrypts_recalls_and_preserves_attached_priority() -> None:
    kek = EnvKekProvider(kek_hex=DEFAULT_DEV_KEK_HEX)
    cipher = EnvelopeCipher(kek)
    user_id = new_id()
    person_id = new_id()
    scope = PersonScope(user_id=user_id, person_id=person_id)
    wrapped, kek_id = cipher.generate_wrapped_dek(user_id)
    dek = cipher.load_dek(user_id=user_id, wrapped_dek=wrapped, kek_id=kek_id)
    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)

    person_row = PersonRow(
        id=person_id,
        user_id=user_id,
        slug="demo-mom",
        tier=Tier.GOLD.value,
        rank="a0",
        state=PersonState.ACTIVE.value,
        display_name_enc=cipher.encrypt_str(
            dek,
            "妈妈",
            user_id=user_id,
            table="persons",
            column="display_name_enc",
            row_id=person_id,
        ),
        monogram="妈",
        role_tag_enc=cipher.encrypt_str(
            dek,
            "家人",
            user_id=user_id,
            table="persons",
            column="role_tag_enc",
            row_id=person_id,
        ),
        traits_enc=cipher.encrypt_json(
            dek,
            ["香菜过敏"],
            user_id=user_id,
            table="persons",
            column="traits_enc",
            row_id=person_id,
        ),
        has_photo=False,
        visual_tokens={},
        version=1,
        created_at=now,
        updated_at=now,
    )

    def _enc_record(
        title: str,
        body: str,
        *,
        happened: date,
        pinned: bool = False,
        review_state: RecordReviewState = RecordReviewState.NORMAL,
    ) -> RecordRow:
        rid = new_id()
        return RecordRow(
            id=rid,
            user_id=user_id,
            person_id=person_id,
            kind=RecordKind.NOTE.value,
            title_enc=cipher.encrypt_str(
                dek, title, user_id=user_id, table="records", column="title_enc", row_id=rid
            ),
            body_enc=cipher.encrypt_str(
                dek, body, user_id=user_id, table="records", column="body_enc", row_id=rid
            ),
            snippet_enc=cipher.encrypt_str(
                dek, body[:50], user_id=user_id, table="records", column="snippet_enc", row_id=rid
            ),
            happened_on=happened,
            pinned=pinned,
            review_state=review_state.value,
            version=1,
            created_at=now,
            updated_at=now,
        )

    attached_rec = _enc_record(
        "右膝盖复查安排",
        "11月28日上午陪妈妈去市一院骨科复查右膝盖。",
        happened=date(2026, 11, 16),
        pinned=True,
    )
    scarf_rec = _enc_record(
        "冬至橱窗前的浅驼色羊绒围巾",
        "去年冬至路过橱窗她摸着浅驼色羊绒围巾说软软的真暖和。",
        happened=date(2025, 12, 21),
        pinned=True,
    )
    unrelated_rec = _enc_record(
        "南阳台茉莉换盆",
        "清明回家给南阳台的茉莉换了透气陶盆。",
        happened=date(2026, 4, 6),
    )

    fake_session = _FakeContextSession(
        person_row=person_row,
        attached_rows=[attached_rec],
        recall_rows=[attached_rec, scarf_rec, unrelated_rec],
    )
    builder = ContextBuilder(cipher)
    built = await builder.build(
        fake_session,  # type: ignore[arg-type]
        scope=scope,
        dek=dek,
        user_message="她去年冬至提过想要什么围巾",
        referenced_material_ids=[attached_rec.id],
    )

    # User-attached material MUST remain FIRST; recalled scarf slip appended right after without dupes!
    mat_ids = [m.record_id for m in built.chat_input.referenced_materials]
    assert mat_ids == [attached_rec.id, scarf_rec.id]
    assert "浅驼色羊绒围巾" in built.chat_input.referenced_materials[1].body
    # Both user-attached AND recalled raw slip are registered in record_catalog & allowed_record_ids!
    assert scarf_rec.id in built.allowed_record_ids
    assert scarf_rec.id in built.record_catalog
    assert unrelated_rec.id not in built.allowed_record_ids
