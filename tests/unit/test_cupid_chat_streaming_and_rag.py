"""V2 Cupid Chat Streaming, Long-Horizon RAG, Citations, Guardrails & Silver Tier unit tests.

Covers:
- G-CC-1: Zero DB session held during LLM/SSE streaming (`session=None`) +
          `: keep-alive\\n\\n` SSE heartbeat on slow streams.
- G-CC-2: 300+ slip long-horizon recall (Year-1 pinned + concept synonyms + temporal intent) +
          `trim_facts_to_token_budget` expired one-off `DATE` demotion preserving `PREFERENCE`.
- G-CC-3: Short alias `[S1, S2]` multi-citation chunk-split invariance + hallucinated `[S99]`
          stripping + unlinked fact non-citation.
- G-CC-4: `StreamingReplyExtractor` stripping `THOUGHT:` lines, preambles before `<reply>`,
          and sidecar tags (`<whisper>`/`<perspectives>`/`<suggestions>`/`<action>`) in BOTH XML
          and guarded plain fallback modes + Silver clause-scoped excusing scrubber.
- G-CC-5: Gold -> Silver retroactive demotion strips `perspectives: []` on historical messages +
          zero canned/legacy copy leaks in `tier_policy.py` & `FakeAdapter`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    SafetyClassifyOutput,
)
from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import PersonRow
from mindweave.main import create_app
from mindweave.modules.companion.application.service import CompanionService
from mindweave.modules.companion.domain.citation_processor import (
    StreamingCitationProcessor,
    process_complete_citations,
)
from mindweave.modules.companion.domain.output_guard import (
    SentenceBufferedStreamGuard,
    StreamingReplyExtractor,
    enforce_tier_output_constraints,
)
from mindweave.modules.companion.domain.tier_policy import (
    BudgetCandidateFact,
    derive_stream_supplements,
    trim_facts_to_token_budget,
)
from mindweave.modules.companion.infra.context_builder import (
    RecallCandidateRecord,
    recall_relevant_records,
)
from mindweave.shared.enums import FactType, SafetyCategory, Tier
from mindweave.shared.scopes import PersonScope, UserScope


async def _signup_and_user(
    client: AsyncClient, email: str
) -> tuple[dict[str, str], UUID]:
    otp_req = await client.post("/v1/auth/email/otp:request", json={"email": email})
    assert otp_req.status_code == 200
    body = otp_req.json()
    verify = await client.post(
        "/v1/auth/email/otp:verify",
        json={"challengeId": body["challengeId"], "email": email, "code": body["devOtpCode"]},
    )
    assert verify.status_code == 200
    vjson = verify.json()
    return {"Authorization": f"Bearer {vjson['accessToken']}"}, UUID(vjson["userId"])


# ============================================================================
# G-CC-1: Zero DB Session Held During LLM/SSE + `: keep-alive\n\n` Heartbeat
# ============================================================================


@pytest.mark.asyncio
async def test_gcc1_zero_db_session_held_and_sse_heartbeat(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, f"gcc1-{new_id().hex[:8]}@example.com")
        p = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "林屿", "tier": "gold"},
        )
        assert p.status_code == 201
        person_id = UUID(p.json()["id"])

    observed_sessions: list[Any] = []

    class _HeartbeatSpyGateway:
        async def classify_safety(
            self, *, scope: UserScope, text: str, session: Any = None
        ) -> SafetyClassifyOutput:
            observed_sessions.append(("safety", session))
            return SafetyClassifyOutput(category=SafetyCategory.NORMAL, reason="ok")

        async def stream_companion_reply(
            self, *, scope: PersonScope, payload: Any, session: Any = None
        ) -> AsyncIterator[str]:
            observed_sessions.append(("stream", session))
            await asyncio.sleep(0.09)
            yield "<reply>今天天气转凉，记得提醒林屿带件外套。</reply>"

    db_container.extras["sse_heartbeat_seconds"] = 0.03
    try:
        svc = CompanionService(db_container, model_gateway=_HeartbeatSpyGateway())  # type: ignore[arg-type]
        scope = PersonScope(user_id=user_id, person_id=person_id)
        gen = await svc.prepare_sse_stream(
            scope=scope,
            message="周末约林屿散步要注意什么？",
            idem_key=f"gcc1-{new_id().hex[:8]}",
            idem_path=f"/v1/persons/{person_id}/chat/stream",
        )
        frames = [frame async for frame in gen]
    finally:
        db_container.extras.pop("sse_heartbeat_seconds", None)

    assert any(f == ": keep-alive\n\n" for f in frames), f"Expected SSE keep-alive, got: {frames}"
    assert any("event: delta" in f for f in frames)
    assert any("event: done" in f for f in frames)
    # CRITICAL: BOTH safety classify and LLM stream ran with session=None (zero DB pool hold!)
    assert observed_sessions
    assert all(sess is None for _, sess in observed_sessions), observed_sessions


# ============================================================================
# G-CC-2: 300+ Slip Long-Horizon Recall + Fact Budget Partitioning
# ============================================================================


def test_gcc2_long_horizon_320_slip_recall_and_budget_partitioning() -> None:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    year1_pinned_id = new_id()
    year1_allergy_id = new_id()

    candidates: list[RecallCandidateRecord] = [
        RecallCandidateRecord(
            record_id=year1_pinned_id,
            kind="voice",
            title="去年冬至聊天",
            body="妈妈去年冬至随口提过想要一条软和的纯羊绒围巾，说真丝会痒。",
            snippet="去年冬至想要纯羊绒围巾",
            happened_on=date(2025, 12, 21),
            created_at=now - timedelta(days=337),
            pinned=True,
        ),
        RecallCandidateRecord(
            record_id=year1_allergy_id,
            kind="note",
            title="饮食忌口",
            body="妈妈对芒果和海鲜都有点过敏，点甜品要避开。",
            snippet="对芒果海鲜过敏",
            happened_on=date(2025, 10, 5),
            created_at=now - timedelta(days=414),
            pinned=False,
        ),
    ]
    # Add 320 newer filler slips so naive recency-only recall would miss Year-1 memories
    for idx in range(320):
        candidates.append(
            RecallCandidateRecord(
                record_id=new_id(),
                kind="note",
                title=f"日常散步 #{idx}",
                body=f"今天下午去公园走了三圈，顺路买了青菜和豆腐 #{idx}",
                snippet=f"去公园散步买菜 #{idx}",
                happened_on=(now - timedelta(days=idx % 90)).date(),
                created_at=now - timedelta(days=idx % 90, minutes=idx),
                pinned=False,
            )
        )

    # Query 1: Temporal intent ("去年冬至") + synonym ("礼物" -> 围巾) hits Year-1 pinned slip
    hits_gift = recall_relevant_records(
        query="妈妈去年冬至提过想要什么礼物？",
        candidates=candidates,
        top_k=5,
        reference_date=now.date(),
    )
    assert hits_gift
    assert hits_gift[0].candidate.record_id == year1_pinned_id

    # Query 2: Concept synonym ("忌口" -> 过敏) hits 414-day-old allergy slip
    hits_allergy = recall_relevant_records(
        query="点甜品要注意什么忌口？",
        candidates=candidates,
        top_k=5,
        reference_date=now.date(),
    )
    assert any(h.candidate.record_id == year1_allergy_id for h in hits_allergy)

    # Fact budget partitioning: expired one-off DATE facts MUST NOT starve active PREFERENCE facts
    expired_dates = [
        BudgetCandidateFact(
            fact_id=new_id(),
            record_id=new_id(),
            fact_type=FactType.DATE,
            content=f"2025年3月{1 + (i % 28)}日去医院拿了一次检验单（已结束很久的历史安排）" * 2,
            is_expired_one_off=True,
        )
        for i in range(40)
    ]
    core_pref = BudgetCandidateFact(
        fact_id=new_id(),
        record_id=new_id(),
        fact_type=FactType.PREFERENCE,
        content="平时最爱喝温热的桂花乌龙茶，不加糖",
        is_expired_one_off=False,
    )
    trimmed = trim_facts_to_token_budget(
        [*expired_dates, core_pref],
        max_tokens=320,
        max_chars=500,
    )
    assert any(f.fact_id == core_pref.fact_id for f in trimmed)


# ============================================================================
# G-CC-3: Short Alias `[S1, S2]` Chunk-Split Invariance & Hallucination Stripping
# ============================================================================


def test_gcc3_short_alias_citations_chunk_invariance_and_hallucination_strip() -> None:
    rec1 = new_id()
    rec2 = new_id()
    alias_map = {"S1": rec1, "S2": rec2}
    allowed = frozenset({rec1, rec2})

    full_text = "她喜欢桂花乌龙[S1, S2]，但不吃香菜[S99]。还有未闭合尾标[S1"

    # Batch processing (`[S99]` hallucinated + unclosed `[S1` tail = 2 stripped)
    summary = process_complete_citations(
        full_text,
        allowed_record_ids=allowed,
        marker_style="bracket",
        alias_to_record_id=alias_map,
    )
    assert summary.cleaned_text == "她喜欢桂花乌龙[1][2]，但不吃香菜。还有未闭合尾标"
    assert [c.record_id for c in summary.citations] == [rec1, rec2]
    assert summary.stripped_hallucinated_count == 2

    # Character-by-character streaming MUST match batch output byte-for-byte!
    proc = StreamingCitationProcessor(
        allowed_record_ids=allowed,
        marker_style="bracket",
        alias_to_record_id=alias_map,
    )
    streamed_parts: list[str] = []
    for ch in full_text:
        step = proc.feed(ch)
        if step.delta_text:
            streamed_parts.append(step.delta_text)
    fin = proc.finalize()
    if fin.delta_text:
        streamed_parts.append(fin.delta_text)

    assert "".join(streamed_parts) == summary.cleaned_text
    assert [c.record_id for c in proc.verified_citations] == [rec1, rec2]
    assert proc.stripped_hallucinated_count == 2


# ============================================================================
# G-CC-4: StreamingReplyExtractor + Silver Clause-Scoped Excusing Scrubber
# ============================================================================


def test_gcc4_streaming_extractor_thought_preamble_and_silver_scrubber() -> None:
    # 1. XML mode with THOUGHT: lines + preamble before <reply> + sidecars
    ext = StreamingReplyExtractor()
    raw_stream = (
        "THOUGHT: checking facts first\n"
        "好的！\n"
        "<reply>她喜欢喝热桂花乌龙，对芒果过敏。</reply>"
        "<whisper>记得避开芒果</whisper>"
        "<perspectives>可能只是最近忙</perspectives>"
        "<suggestions>「周末我带你去那家桂花乌龙茶馆吧」</suggestions>"
        '<action type="create_reminder" title="提醒买茶" date="2026-12-01" />'
    )
    visible: list[str] = []
    for idx in range(0, len(raw_stream), 3):
        visible.append(ext.feed(raw_stream[idx : idx + 3]))
    visible.append(ext.finalize())

    assert "".join(visible) == "她喜欢喝热桂花乌龙，对芒果过敏。"
    assert "<suggestions>「周末我带你去那家桂花乌龙茶馆吧」</suggestions>" in ext.tail_text
    assert '<action type="create_reminder" title="提醒买茶" date="2026-12-01" />' in ext.tail_text

    # 2. Guarded plain-text fallback STILL intercepts trailing sidecar tags!
    ext_plain = StreamingReplyExtractor()
    plain_raw = (
        "这是一段没有写 reply 开始标签的直接回答，长度足够超过四十八个字所以会平稳进入纯文本模式。"
        "<suggestions>「直接邮件书面确认需求变更」</suggestions>"
    )
    plain_out = ext_plain.feed(plain_raw) + ext_plain.finalize()
    assert "<suggestions>" not in plain_out
    assert "直接邮件书面确认需求变更" not in plain_out
    assert "「直接邮件书面确认需求变更」" in ext_plain.tail_text

    # 3. Silver tier clause-scoped excusing scrubber removes excusing clause while keeping dates/facts
    dirty_silver = (
        "他在11月10日周会甩锅，15日又半夜口头改需求，也许他最近压力大。建议直接邮件书面留痕。"
    )
    cleaned = enforce_tier_output_constraints(dirty_silver, tier=Tier.SILVER)
    assert "压力大" not in cleaned
    assert "11月10日周会甩锅" in cleaned
    assert "建议直接邮件书面留痕。" in cleaned

    # Streaming sentence guard on Silver strips excusing clause cleanly too
    guard = SentenceBufferedStreamGuard(tier=Tier.SILVER)
    streamed = guard.feed(dirty_silver) + guard.finalize()
    assert "压力大" not in streamed
    assert "11月10日周会甩锅" in streamed


# ============================================================================
# G-CC-5: Gold -> Silver Retroactive Perspective Mask + Zero Canned Copy Leaks
# ============================================================================


@pytest.mark.asyncio
async def test_gcc5_gold_to_silver_retroactive_mask_and_zero_canned_copy(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 10, 0, tzinfo=UTC))
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, f"gcc5-{new_id().hex[:8]}@example.com")

        p = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "阿哲", "tier": "gold"},
        )
        assert p.status_code == 201
        person_id = UUID(p.json()["id"])

        # Send a Gold conflict message so perspectives are persisted
        r_chat = await client.post(
            f"/v1/persons/{person_id}/chat/messages",
            headers={**headers, "Idempotency-Key": str(new_id()), "Accept": "application/json"},
            json={"message": "阿哲今天冷脸回应让我好委屈吵架了"},
        )
        assert r_chat.status_code == 200
        hist_before = await client.get(f"/v1/persons/{person_id}/chat/messages", headers=headers)
        assert hist_before.status_code == 200
        before_assistants = [m for m in hist_before.json()["items"] if m["role"] == "assistant"]
        assert before_assistants
        assert len(before_assistants[-1]["perspectives"]) >= 1

        # Demote person from Gold -> Silver
        async with db_container.db.user_session(UserScope(user_id=user_id)) as sess:
            p_row = await sess.scalar(select(PersonRow).where(PersonRow.id == person_id))
            assert p_row is not None
            p_row.tier = Tier.SILVER.value
            await sess.flush()

        # Re-read chat history: historical perspectives MUST be retroactively masked to []!
        hist_after = await client.get(f"/v1/persons/{person_id}/chat/messages", headers=headers)
        assert hist_after.status_code == 200
        assistant_msgs = [m for m in hist_after.json()["items"] if m["role"] == "assistant"]
        assert assistant_msgs
        assert all(m["perspectives"] == [] for m in assistant_msgs)

    # Zero canned fallback copy in derive_stream_supplements
    ci = CompanionChatInput(
        person_id=new_id(),
        person_name="林屿",
        tier=Tier.GOLD,
        user_message="聊聊近况",
    )
    sup = derive_stream_supplements(ci, cleaned_reply="林屿最近忙完项目啦。", verified_record_ids=[])
    assert sup.whisper_note is None
    assert sup.actionable_suggestions == []
