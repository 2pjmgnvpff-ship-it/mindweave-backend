"""Unit tests for Companion pure domain policies, StreamingCitationProcessor & OutputGuard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    ContextFactItem,
)
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.core.ids import new_id
from mindweave.modules.companion.domain import (
    FORBIDDEN_DEV_JARGON_PHRASES,
    ILLEGAL_INTENT_ALLY_PULLBACK,
    LATE_NIGHT_REST_WHISPER_SILVER,
    PUBLIC_SHAMING_BOUNDARY_GUIDANCE,
    BudgetCandidateFact,
    SentenceBufferedStreamGuard,
    StreamingCitationProcessor,
    StreamingOutputGuard,
    StreamingReplyExtractor,
    VerifiedCitation,
    apply_tier_policy,
    decide_chat_writeback_gate,
    derive_stream_supplements,
    enforce_tier_output_constraints,
    evaluate_session_window,
    is_public_shaming_request,
    process_complete_citations,
    sanitize_companion_output,
    sanitize_text,
    should_writeback_chat_message,
    trim_facts_to_token_budget,
    validate_cited_record_ids,
)
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    PendingReason,
    PersonState,
    SafetyCategory,
    SourceSpeaker,
    Tier,
)


def test_streaming_citation_processor_strips_raw_ids_and_verifies_split_chunks() -> None:
    rec_valid_1 = UUID("01931b2c-4d5e-7000-8000-000000000001")
    rec_valid_2 = UUID("01931b2c-4d5e-7000-8000-000000000002")
    rec_other_person = UUID("01931b2c-4d5e-7000-8000-999999999999")

    proc = StreamingCitationProcessor({rec_valid_1, rec_valid_2})

    # Deliberately split across `[`, `rec_`, UUID middle, full-width `【...】`, and punctuation
    chunks = [
        "清秋对芒果严重过敏 [rec_",
        "01931b2c-4d5e-7000-",
        "8000-000000000001]，记得避开！另外她很喜欢热燕麦拿铁【rec_01931b2c-4d5e-7000-8000-000000000002",
        "】；至于乱编的 [rec_01931b2c-4d5e-7000-8000-999999999999] 和 [rec_1, rec_",
        "2] 都会被剥离，重复引用 [rec_01931b2c-4d5e-7000-8000-000000000001] 不会重复编号。",
    ]

    emitted_text_parts: list[str] = []
    emitted_citations: list[VerifiedCitation] = []
    for ch in chunks:
        step = proc.feed(ch)
        emitted_text_parts.append(step.delta_text)
        emitted_citations.extend(step.new_citations)

    fin = proc.finalize()
    emitted_text_parts.append(fin.delta_text)
    emitted_citations.extend(fin.new_citations)

    final_text = "".join(emitted_text_parts)
    assert "[rec_" not in final_text
    assert "【rec_" not in final_text
    assert str(rec_valid_1) not in final_text
    assert str(rec_other_person) not in final_text
    assert "过敏，记得避开" in final_text
    assert emitted_citations == [
        VerifiedCitation(index=1, record_id=rec_valid_1),
        VerifiedCitation(index=2, record_id=rec_valid_2),
    ]
    assert proc.verified_record_ids == [rec_valid_1, rec_valid_2]
    # 1 out-of-scope UUID + 2 bogus short IDs (`rec_1`, `rec_2`) = 3 stripped hallucinations
    assert proc.stripped_hallucinated_count == 3


def test_streaming_citation_preserves_fenced_and_inline_backtick_code_spans() -> None:
    rec_valid = UUID("01931b2c-4d5e-7000-8000-000000000010")
    proc = StreamingCitationProcessor({rec_valid})

    # Split triple backticks AND single/double inline backticks across chunk boundaries
    chunks = [
        "下面是示例代码：\n``",
        "`python\npayload = [rec_1, rec_2]\nraw = '[rec_01931b2c-4d5e-7000-8000-000000000010]'\n``",
        "`\n行内单反引号 `[rec_",
        "01931b2c-4d5e-7000-8000-000000000010]` 与双反引号 ``[rec_99",
        "9]`` 同样原样保留，真实手帐引用在这里 [rec_01931b2c-4d5e-7000-8000-",
        "000000000010]！",
    ]

    assembled = "".join(proc.feed(c).delta_text for c in chunks) + proc.finalize().delta_text
    assert "payload = [rec_1, rec_2]" in assembled
    assert "raw = '[rec_01931b2c-4d5e-7000-8000-000000000010]'" in assembled
    assert "`[rec_01931b2c-4d5e-7000-8000-000000000010]`" in assembled
    assert "``[rec_999]``" in assembled
    assert assembled.endswith("真实手帐引用在这里！")
    assert proc.verified_citations == (VerifiedCitation(index=1, record_id=rec_valid),)
    assert proc.stripped_hallucinated_count == 0


def test_nested_brackets_and_truncated_rec_tail() -> None:
    rec_valid = UUID("01931b2c-4d5e-7000-8000-000000000022")
    full = f"【重点提醒：林表姑海鲜过敏 [rec_{rec_valid}]】[捂脸] [2026-11-12] [rec_truncated_eof"

    # Character-by-character streaming
    proc = StreamingCitationProcessor({rec_valid})
    out = "".join(proc.feed(ch).delta_text for ch in full) + proc.finalize().delta_text

    assert out == "【重点提醒：林表姑海鲜过敏】[捂脸] [2026-11-12]"
    assert "[rec_" not in out
    assert proc.verified_citations == (VerifiedCitation(index=1, record_id=rec_valid),)
    assert proc.stripped_hallucinated_count == 1

    # Nested ASCII square brackets (`[重点：忌口 [rec_...] 必看]`)
    nested_ascii = f"她不能吃海鲜 [重点：忌口 [rec_{rec_valid}] 必看]。"
    nested_res = process_complete_citations(nested_ascii, {rec_valid})
    assert f"rec_{rec_valid}" not in nested_res.cleaned_text
    assert nested_res.cleaned_text == "她不能吃海鲜 [重点：忌口必看]。"
    assert nested_res.citations == (VerifiedCitation(index=1, record_id=rec_valid),)


def test_superscript_marker_mode_and_non_streaming_id_validation() -> None:
    rec_a = new_id()
    rec_b = new_id()
    rec_bogus = new_id()

    summary = process_complete_citations(
        f"她喜欢桂花拿铁 [rec_{rec_a}]，不吃香菜 [rec_{rec_bogus}]。",
        {rec_a, rec_b},
        declared_cited_ids=[rec_a, rec_b, rec_bogus],
        marker_style="superscript",
    )
    assert summary.cleaned_text == "她喜欢桂花拿铁¹，不吃香菜。"
    assert [c.record_id for c in summary.citations] == [rec_a, rec_b]
    assert summary.stripped_hallucinated_count == 2

    verified_ids, stripped = validate_cited_record_ids([rec_b, rec_bogus, rec_b], {rec_a, rec_b})
    assert verified_ids == [rec_b]
    assert stripped == 1


@settings(max_examples=60)
@given(split_points=st.lists(st.integers(min_value=0, max_value=300), max_size=25))
def test_property_chunk_split_invariance(split_points: list[int]) -> None:
    rec_1 = UUID("01931b2c-4d5e-7000-8000-000000000101")
    rec_2 = UUID("01931b2c-4d5e-7000-8000-000000000102")
    rec_bad = UUID("01931b2c-4d5e-7000-8000-000000000999")

    sample = (
        f"【手帐摘要】清秋忌芒果 [rec_{rec_1}]，喜欢海边散步【rec_{rec_2}, rec_{rec_bad}】。\n"
        f'```json\n{{"debug": "[rec_1, rec_{rec_bad}]"}}\n```\n'
        f"行内示例 `[rec_{rec_bad}]` 与嵌套 [重点：忌口 [rec_{rec_1}] 必看] [开心]。"
    )
    baseline = process_complete_citations(sample, {rec_1, rec_2})

    pts = sorted({0, len(sample), *(min(max(0, p), len(sample)) for p in split_points)})
    chunks = [sample[pts[i] : pts[i + 1]] for i in range(len(pts) - 1)]

    proc = StreamingCitationProcessor({rec_1, rec_2})
    text_parts = [proc.feed(c).delta_text for c in chunks]
    text_parts.append(proc.finalize().delta_text)

    assert "".join(text_parts) == baseline.cleaned_text
    assert proc.verified_citations == baseline.citations
    assert proc.stripped_hallucinated_count == baseline.stripped_hallucinated_count


def test_tier_policies_silver_wipes_perspectives_and_enforces_sop_boundaries() -> None:
    hallucinated_silver_out = CompanionChatOutput(
        safety_flag=SafetyCategory.NORMAL,
        reply_text="张主管这次甩锅确实太过分了，我已经帮你把时间线记下来了。",
        whisper_note="留好书面记录最稳妥。",
        cited_record_ids=[],
        has_unrecorded_gap=False,
        alternative_perspectives=[
            "也许张主管最近绩效压力太大，不是故意针对你的？",
            "换位思考一下他可能也忘了。",
        ],
        actionable_suggestions=["发封抄送项目组的邮件确认节点"],
    )

    policed_silver = apply_tier_policy(
        hallucinated_silver_out,
        tier=Tier.SILVER,
        user_message="张主管开会又把锅甩给我，气死我了！",
        safety_category=SafetyCategory.NORMAL,
    )
    # ADR-005 #3: SILVER deterministically wipes alternative_perspectives to []
    assert policed_silver.alternative_perspectives == []

    # CRYSTAL also forces alternative_perspectives = []
    policed_crystal = apply_tier_policy(
        hallucinated_silver_out,
        tier=Tier.CRYSTAL,
        user_message="周二找陈教授签字要注意什么？",
        safety_category=SafetyCategory.NORMAL,
    )
    assert policed_crystal.alternative_perspectives == []

    # GOLD preserves gentle alternative_perspectives
    policed_gold = apply_tier_policy(
        hallucinated_silver_out,
        tier=Tier.GOLD,
        user_message="今天他回消息好慢有点委屈",
        safety_category=SafetyCategory.NORMAL,
    )
    assert len(policed_gold.alternative_perspectives) == 2

    # Crisis SOP §1.2 Boundary 2: ILLEGAL_INTENT enforces protective ally pull-back
    illegal_out = apply_tier_policy(
        CompanionChatOutput(
            reply_text="他之前的越界记录都在这里。",
            actionable_suggestions=["去堵他家门", "整理书面投诉材料"],
        ),
        tier=Tier.SILVER,
        user_message="气死我了明天去他水杯里下药",
        safety_category=SafetyCategory.ILLEGAL_INTENT,
    )
    assert "为了这种人搭上你自己的前途太不值了" in illegal_out.reply_text
    assert "合法留痕反击" in illegal_out.reply_text
    assert ILLEGAL_INTENT_ALLY_PULLBACK in illegal_out.reply_text
    assert illegal_out.alternative_perspectives == []
    assert all("堵他家" not in s for s in illegal_out.actionable_suggestions)

    # Crisis SOP §1.2 Boundary 1: Public shaming / doxxing card request blocked
    assert is_public_shaming_request("帮我生成一张发小红书的挂人曝光长图")
    shaming_out = apply_tier_policy(
        hallucinated_silver_out,
        tier=Tier.SILVER,
        user_message="帮我生成一张发朋友圈骂他的罪状长图",
        safety_category=SafetyCategory.NORMAL,
    )
    assert shaming_out.safety_flag is SafetyCategory.BOUNDARY_REFUSAL
    assert shaming_out.reply_text == PUBLIC_SHAMING_BOUNDARY_GUIDANCE
    assert shaming_out.alternative_perspectives == []


def test_output_guard_scrubs_parasitic_scoring_and_dev_jargon() -> None:
    # Ensure domain jargon list covers every phrase in core.dto.FORBIDDEN_UI_DEV_PHRASES
    assert set(FORBIDDEN_UI_DEV_PHRASES).issubset(set(FORBIDDEN_DEV_JARGON_PHRASES))

    dirty = (
        "系统自动归档：别理现实里的人了，只有我最懂你，我会永远陪着你！"
        "我是你的恋人，他的渣男分：92分，爱的语系夹角 38°，C-SSRS 正常。先喝杯热茶休息下吧。"
    )
    rep = sanitize_text(dirty, allow_empty=False)
    for banned in (
        "系统自动归档",
        "别理现实里的人",
        "只有我最懂你",
        "我会永远陪着你",
        "我是你的恋人",
        "渣男分：92分",
        "爱的语系夹角",
        "C-SSRS",
    ):
        assert banned not in rep.cleaned_text
    assert "先喝杯热茶休息下吧" in rep.cleaned_text
    assert rep.triggered

    # Streaming guard catches forbidden phrase split right across two SSE chunks
    sguard = StreamingOutputGuard()
    part1 = sguard.feed("别难过，只有我")
    part2 = sguard.feed("最懂你，我们一起看看记录。")
    part3 = sguard.finalize()
    combined = part1 + part2 + part3
    assert "只有我最懂你" not in combined
    assert "我们一起看看记录。" in combined

    # Sentence-buffered Silver SSE guard (RT-09) + StreamingReplyExtractor across split tags
    extractor = StreamingReplyExtractor()
    cit_proc = StreamingCitationProcessor(set())
    silver_guard = SentenceBufferedStreamGuard(tier=Tier.SILVER)
    raw_sse_chunks = [
        "<re",
        "ply>也许对方也有苦",
        "衷，换个角度想他也不是故意的。建议把对接文档 `[rec_demo]` 发群里留痕。</rep",
        "ly><whisper>守住边界</whisper>",
    ]
    emitted: list[str] = []
    for ch in raw_sse_chunks:
        body = extractor.feed(ch)
        step = cit_proc.feed(body)
        emitted.append(silver_guard.feed(step.delta_text))
    rem = extractor.finalize()
    fin = cit_proc.finalize()
    emitted.append(silver_guard.feed(cit_proc.feed(rem).delta_text + fin.delta_text))
    emitted.append(silver_guard.finalize())
    silver_joined = "".join(emitted)
    assert "也有苦衷" not in silver_joined
    assert "换个角度想" not in silver_joined
    assert "建议把对接文档 `[rec_demo]` 发群里留痕。" == silver_joined
    assert "<whisper>守住边界</whisper>" in extractor.tail_text
    assert enforce_tier_output_constraints(
        "换个角度想他也不是故意的。", tier=Tier.SILVER, allow_empty=False
    )

    # Full CompanionChatOutput sanitization
    raw_out = CompanionChatOutput(
        reply_text="AI 提炼卡片：她喜欢抹茶拿铁。",
        whisper_note="温情伴读模式：记得提前预约",
        alternative_perspectives=["方案 A：周末轻松聊聊"],
        actionable_suggestions=["只有我最懂你"],
    )
    clean_out, out_rep = sanitize_companion_output(raw_out)
    assert "AI 提炼卡片" not in clean_out.reply_text
    assert clean_out.whisper_note is not None and "温情伴读模式" not in clean_out.whisper_note
    assert clean_out.alternative_perspectives == ["周末轻松聊聊"]
    assert clean_out.actionable_suggestions == []
    assert out_rep.triggered


def test_token_budget_trimming_session_care_and_writeback_gate() -> None:
    rid = new_id()
    # Older taboo/date/promise facts MUST survive even when newer notes overflow 6,000-char budget
    newest_notes = [
        BudgetCandidateFact(
            fact_id=new_id(),
            record_id=rid,
            fact_type=FactType.NOTE,
            content=f"日常闲聊长备注编号{i}：" + ("琐事细节记录" * 60),
        )
        for i in range(25)
    ]
    critical_taboo = BudgetCandidateFact(
        fact_id=new_id(),
        record_id=rid,
        fact_type=FactType.TABOO,
        content="严重花生与芒果过敏，千万不能碰",
    )
    critical_date = BudgetCandidateFact(
        fact_id=new_id(),
        record_id=rid,
        fact_type=FactType.DATE,
        content="每年 10 月 18 日恋爱纪念日",
    )
    critical_promise = BudgetCandidateFact(
        fact_id=new_id(),
        record_id=rid,
        fact_type=FactType.PROMISE,
        content="答应五一带她去镰仓看海",
    )

    trimmed = trim_facts_to_token_budget(
        [*newest_notes, critical_taboo, critical_date, critical_promise],
        max_tokens=4000,
        max_chars=6000,
    )
    trimmed_ids = {f.fact_id for f in trimmed}
    assert critical_taboo.fact_id in trimmed_ids
    assert critical_date.fact_id in trimmed_ids
    assert critical_promise.fact_id in trimmed_ids
    assert sum(len(f.content) for f in trimmed) <= 6000

    # Session window & late-night SOP rest care (00:00-05:00 >= 45 min)
    t0 = datetime(2026, 11, 12, 16, 10, tzinfo=UTC)  # 00:10 Asia/Shanghai
    s1 = evaluate_session_window(
        previous_session_started_at=None,
        previous_last_message_at=None,
        now_utc=t0,
        local_hour=0,
        tier=Tier.SILVER,
    )
    assert not s1.trigger_late_night_care
    t1 = t0 + timedelta(minutes=25)
    s2 = evaluate_session_window(
        previous_session_started_at=s1.session_started_at,
        previous_last_message_at=s1.last_user_message_at,
        now_utc=t1,
        local_hour=0,
        tier=Tier.SILVER,
    )
    assert not s2.trigger_late_night_care
    t2 = t1 + timedelta(minutes=22)  # 47 min continuous (<30 min between turns)
    s3 = evaluate_session_window(
        previous_session_started_at=s2.session_started_at,
        previous_last_message_at=s2.last_user_message_at,
        now_utc=t2,
        local_hour=1,
        tier=Tier.SILVER,
    )
    assert s3.trigger_late_night_care
    assert s3.care_whisper == LATE_NIGHT_REST_WHISPER_SILVER

    # >30 min inactivity resets session_started_at
    t3 = t2 + timedelta(minutes=35)
    s4 = evaluate_session_window(
        previous_session_started_at=s3.session_started_at,
        previous_last_message_at=s3.last_user_message_at,
        now_utc=t3,
        local_hour=1,
        tier=Tier.SILVER,
    )
    assert s4.session_started_at == t3
    assert not s4.trigger_late_night_care

    # Declarative chat writeback eligibility
    assert not should_writeback_chat_message("她有什么忌口吗？")
    assert not should_writeback_chat_message("帮我写一段生日祝福草稿")
    assert not should_writeback_chat_message("活着没意义想吃安眠药", SafetyCategory.CRISIS)
    assert should_writeback_chat_message("顺便记一下，清秋最近迷上了桂花乌龙，而且对芒果过敏")
    assert should_writeback_chat_message("刚跟陈教授约好下周二早九点签字，怎么发确认消息好？")

    # GatePolicy deterministic checks
    assert decide_chat_writeback_gate(
        fact_type=FactType.PREFERENCE,
        confidence=0.92,
        candidate_count=1,
        multi_person=False,
        scoped_locked=True,
        is_venting_or_irony=False,
        tier=Tier.GOLD,
        person_state=PersonState.ACTIVE,
        source_speaker=SourceSpeaker.NARRATOR,
    ) == (FactStatus.CONFIRMED, None)

    assert decide_chat_writeback_gate(
        fact_type=FactType.TABOO,
        confidence=0.96,
        candidate_count=1,
        multi_person=False,
        scoped_locked=True,
        is_venting_or_irony=False,
        tier=Tier.GOLD,
        person_state=PersonState.ACTIVE,
        source_speaker=SourceSpeaker.NARRATOR,
        polarity_flipped=True,
    ) == (FactStatus.PENDING, PendingReason.POLARITY_FLIP)

    assert decide_chat_writeback_gate(
        fact_type=FactType.DATE,
        confidence=0.98,
        candidate_count=1,
        multi_person=False,
        scoped_locked=True,
        is_venting_or_irony=False,
        tier=Tier.GOLD,
        person_state=PersonState.ACTIVE,
        source_speaker=SourceSpeaker.NARRATOR,
    ) == (FactStatus.PENDING, PendingReason.DATE_OR_PROMISE)

    assert decide_chat_writeback_gate(
        fact_type=FactType.NOTE,
        confidence=0.95,
        candidate_count=1,
        multi_person=False,
        scoped_locked=True,
        is_venting_or_irony=True,
        tier=Tier.GOLD,
        person_state=PersonState.ACTIVE,
        source_speaker=SourceSpeaker.NARRATOR,
    ) == (FactStatus.PENDING, PendingReason.VENTING)

    # Stream supplements respect tier policy
    supp_silver = derive_stream_supplements(
        CompanionChatInput(
            person_id=new_id(),
            person_name="张主管",
            tier=Tier.SILVER,
            confirmed_facts=[
                ContextFactItem(
                    record_id=rid,
                    fact_id=new_id(),
                    fact_type=FactType.EVENT,
                    content="周会甩锅",
                )
            ],
            user_message="他又甩锅给我",
        ),
        cleaned_reply="已经帮你记下这次周会甩锅。",
        verified_record_ids=[rid],
    )
    assert supp_silver.alternative_perspectives == []
    assert not supp_silver.has_unrecorded_gap


@pytest.mark.parametrize("tier", [Tier.SILVER, Tier.CRYSTAL])
def test_non_gold_tiers_never_allow_perspectives(tier: Tier) -> None:
    out = CompanionChatOutput(
        reply_text="客观记录已核对。",
        alternative_perspectives=["也许对方不是故意的"],
    )
    res = apply_tier_policy(out, tier=tier, user_message="复盘一下")
    assert res.alternative_perspectives == []


@pytest.mark.parametrize("chunk_size", [1, 2, 5, 13, 200])
def test_r2_04_sentence_buffered_stream_guard_preserves_fenced_code_blocks_and_indentation(
    chunk_size: int,
) -> None:
    sample_id = "01965b30-0000-7000-8000-000000000099"
    bogus_id = "01965b30-0000-7000-8000-0000000000aa"
    raw_xml = (
        "<reply>"
        "换个角度想也许他也有苦衷。"
        f"参考模板 [rec_{bogus_id}] 如下：\n"
        f"```markdown\n    [rec_{sample_id}]\n        nested_item: true\n```\n"
        f"以及波浪线围栏：\n~~~yaml\n  ref: [rec_{sample_id}]\n~~~\n"
        f"内联示例 `[。！？] [rec_{sample_id}]` 按这个格式整理即可。"
        "</reply>"
    )

    extractor = StreamingReplyExtractor()
    cit_proc = StreamingCitationProcessor(set())
    guard = SentenceBufferedStreamGuard(tier=Tier.SILVER)

    deltas: list[str] = []
    for idx in range(0, len(raw_xml), chunk_size):
        piece = raw_xml[idx : idx + chunk_size]
        body = extractor.feed(piece)
        if body:
            step = cit_proc.feed(body)
            d = guard.feed(step.delta_text)
            if d:
                deltas.append(d)

    rem = extractor.finalize()
    fin_cit = cit_proc.finalize()
    tail = guard.feed(cit_proc.feed(rem).delta_text + fin_cit.delta_text) + guard.finalize()
    if tail:
        deltas.append(tail)

    assembled = "".join(deltas)
    assert "换个角度想" not in assembled
    assert "苦衷" not in assembled
    assert bogus_id not in assembled
    assert f"```markdown\n    [rec_{sample_id}]\n        nested_item: true\n```\n" in assembled
    assert f"~~~yaml\n  ref: [rec_{sample_id}]\n~~~\n" in assembled
    assert f"`[。！？] [rec_{sample_id}]`" in assembled
    assert assembled.endswith("按这个格式整理即可。")
