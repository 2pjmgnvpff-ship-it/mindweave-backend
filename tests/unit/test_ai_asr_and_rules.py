"""Unit tests for context-aware ASR, audio adapters, dedicated ASR routing, history-grounded extraction, event_group splitting, and warm companion replies."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterPermanentError,
)
from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.adapters.gemini import GeminiAdapter
from mindweave.ai.adapters.openai_compatible import OpenAICompatibleAdapter
from mindweave.ai.gateway import ModelGateway, load_routing_config
from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    ContextFactItem,
    ContextMaterialItem,
    ExtractedFactItem,
    ExtractedPersonCandidate,
    ExtractRecordInput,
    ExtractRecordOutput,
    KnownPersonRef,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.ai.validators.asr_postprocess import (
    postprocess_asr_transcript,
    resolve_spoken_self_corrections,
    restore_context_homophones,
)
from mindweave.core.config import Settings
from mindweave.core.errors import UpstreamAIError
from mindweave.core.ids import new_id
from mindweave.shared.enums import FactType, Polarity, SafetyCategory, Tier, TimeStatus
from mindweave.shared.scopes import UserScope

# ============================================================================
# 1. Backward-Compatible Schema Expansions & extra="forbid"
# ============================================================================


def test_expanded_ai_schemas_keep_backward_compatibility_and_forbid_extras() -> None:
    pid = new_id()
    person_minimal = KnownPersonRef(person_id=pid, display_name="妈妈")
    assert person_minimal.recent_facts == []

    person_with_facts = KnownPersonRef(
        person_id=pid,
        display_name="妈妈",
        recent_facts=["右膝盖疼", "纯羊绒围巾"],
    )
    assert person_with_facts.recent_facts == ["右膝盖疼", "纯羊绒围巾"]

    t_minimal = TranscribeInput(audio_bytes=b"abc")
    assert t_minimal.active_person_name is None
    assert t_minimal.recent_context_snippets == []

    fact_minimal = ExtractedFactItem(
        person_mention="妈妈",
        fact_type=FactType.EVENT,
        content="复查膝盖",
    )
    assert fact_minimal.event_group is None

    ext_minimal = ExtractRecordInput(text="妈妈喜欢围巾", current_date="2026-11-23")
    assert ext_minimal.recent_history_hints == []

    with pytest.raises(ValidationError):
        KnownPersonRef.model_validate(
            {"person_id": str(pid), "display_name": "妈妈", "unknown_field": 1}
        )
    with pytest.raises(ValidationError):
        TranscribeInput.model_validate({"audio_bytes": b"abc", "bogus": True})
    with pytest.raises(ValidationError):
        ExtractedFactItem.model_validate(
            {
                "person_mention": "妈妈",
                "fact_type": "event",
                "content": "x",
                "extra_key": "no",
            }
        )


# ============================================================================
# 2. Real Audio Support Across OpenAI-Compatible & Gemini + Dedicated ASR Config
# ============================================================================


@pytest.mark.parametrize(
    ("mime_type", "expected_format"),
    [
        ("audio/webm", "webm"),
        ("audio/webm;codecs=opus", "webm"),
        ("audio/mp4", "m4a"),
        ("audio/m4a", "m4a"),
        ("audio/mpeg", "mp3"),
        ("audio/mp3", "mp3"),
        ("audio/wav", "wav"),
        ("audio/ogg", "ogg"),
    ],
)
async def test_openai_compatible_adapter_supports_multimodal_input_audio_formats(
    mime_type: str,
    expected_format: str,
) -> None:
    captured_bodies: list[dict[str, Any]] = []
    raw_audio = b"MOCK_REAL_AUDIO_PAYLOAD_12345"

    async def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_bodies.append(body)
        sse_lines = [
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": '{"raw_transcript":"程经理约了周五","corrected_transcript":"陈经理约了周五","duration_ms":2100}'
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "data: "
            + json.dumps({"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 30}}),
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("\n\n".join(sse_lines) + "\n\n").encode("utf-8"),
        )

    adapter = OpenAICompatibleAdapter(
        provider_name="dashscope",
        api_key="sk-dashscope-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3-omni-flash",
        transport=httpx.MockTransport(_handler),
    )

    resp = await adapter.complete(
        AdapterCallRequest(
            task="transcribe",
            model="qwen3-omni-flash",
            prompt="请转写音频",
            temperature=0.0,
            response_schema=TranscribeOutput,
            media_bytes=raw_audio,
            media_mime_type=mime_type,
        )
    )
    assert resp.model == "qwen3-omni-flash"
    assert '"corrected_transcript":"陈经理约了周五"' in resp.text
    assert resp.input_tokens == 90
    assert resp.output_tokens == 30
    assert len(captured_bodies) == 1
    assert captured_bodies[0]["stream"] is True
    assert captured_bodies[0]["modalities"] == ["text"]
    user_content = captured_bodies[0]["messages"][-1]["content"]
    assert user_content[0] == {
        "type": "input_audio",
        "input_audio": {
            "data": f"data:;base64,{base64.b64encode(raw_audio).decode('ascii')}",
            "format": expected_format,
        },
    }
    assert user_content[1] == {"type": "text", "text": "请转写音频"}


async def test_openai_compatible_adapter_rejects_unsupported_non_image_audio_media() -> None:
    adapter = OpenAICompatibleAdapter(
        provider_name="dashscope",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3-omni-flash",
    )
    with pytest.raises(AdapterPermanentError, match="image/\\* or audio/\\*"):
        await adapter.complete(
            AdapterCallRequest(
                task="transcribe",
                model="qwen3-omni-flash",
                prompt="hi",
                media_bytes=b"%PDF-1.7",
                media_mime_type="application/pdf",
            )
        )


class _SpyGeminiModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})

        class _Resp:
            text = (
                '{"raw_transcript":"测试录音","corrected_transcript":"测试录音","duration_ms":1200}'
            )
            usage_metadata = None

        return _Resp()


class _SpyGeminiClient:
    def __init__(self) -> None:
        self.models = _SpyGeminiModels()
        self.aio = self


@pytest.mark.parametrize(
    ("input_mime", "expected_normalized_mime"),
    [
        ("audio/webm;codecs=opus", "audio/webm"),
        ("audio/m4a", "audio/mp4"),
        ("audio/mp4", "audio/mp4"),
        ("audio/wav", "audio/wav"),
    ],
)
async def test_gemini_adapter_normalizes_audio_mime_types_for_part_from_bytes(
    input_mime: str,
    expected_normalized_mime: str,
) -> None:
    spy = _SpyGeminiClient()
    adapter = GeminiAdapter(api_key="test-key", client=spy)
    resp = await adapter.complete(
        AdapterCallRequest(
            task="transcribe",
            model="gemini-2.5-flash",
            prompt="转写",
            temperature=0.0,
            response_schema=TranscribeOutput,
            media_bytes=b"FAKE_AUDIO",
            media_mime_type=input_mime,
        )
    )
    assert "测试录音" in resp.text
    sent_part = spy.models.calls[0]["contents"][0]
    assert sent_part.inline_data.mime_type == expected_normalized_mime
    assert spy.models.calls[0]["config"].response_mime_type == "application/json"


async def test_dedicated_asr_provider_routing_and_prod_cn_guardrails() -> None:
    routing = load_routing_config()
    assert routing.semantic_cache_enabled is False
    assert routing.tasks["transcribe"].models["dashscope"] == "qwen3-omni-flash"

    # prod_cn strictly forbids gemini & fake for ai_asr_provider too!
    strong_kek = "a" * 64
    strong_jwt = "b" * 40
    with pytest.raises(ValueError, match="Compliance Gate M6"):
        Settings(
            environment="prod_cn",
            ai_primary_provider="deepseek",
            ai_fallback_provider="dashscope",
            ai_asr_provider="gemini",
            allow_dev_mock_apple_auth=False,
            expose_dev_otp_in_response=False,
            push_apns_adapter="disabled",
            push_web_push_adapter="disabled",
            kek_hex=strong_kek,
            jwt_secret=strong_jwt,
        )
    with pytest.raises(ValueError, match="fake AI provider"):
        Settings(
            environment="prod_cn",
            ai_primary_provider="deepseek",
            ai_fallback_provider="dashscope",
            ai_asr_provider="fake",
            allow_dev_mock_apple_auth=False,
            expose_dev_otp_in_response=False,
            push_apns_adapter="disabled",
            push_web_push_adapter="disabled",
            kek_hex=strong_kek,
            jwt_secret=strong_jwt,
        )

    # Runtime gateway routing: deepseek primary + dashscope dedicated ASR provider
    settings = Settings(
        environment="test",
        ai_primary_provider="deepseek",
        ai_fallback_provider="none",
        ai_asr_provider="dashscope",
    )
    spy_dashscope = FakeAdapter()
    spy_deepseek = FakeAdapter()
    gw = ModelGateway(
        settings,
        adapters={"dashscope": spy_dashscope, "deepseek": spy_deepseek},
    )
    assert gw._resolve_model("transcribe", "dashscope") == "qwen3-omni-flash"
    assert gw._provider_chain_for_task("extract_record") == ["deepseek"]
    assert gw._provider_chain_for_task("transcribe") == ["dashscope", "deepseek"]

    out = await gw.transcribe(
        scope=UserScope(new_id()),
        payload=TranscribeInput(
            audio_bytes="TEXT:程经理说周五前发报价单".encode(),
            known_person_names=["陈经理"],
        ),
    )
    assert out.corrected_transcript == "陈经理说周五前发报价单"
    assert len(spy_dashscope.recorded_requests) == 1
    assert spy_dashscope.recorded_requests[0].model == "qwen3-omni-flash"
    assert len(spy_deepseek.recorded_requests) == 0

    # Runtime M6 guard blocks bypassed prod_cn + ai_asr_provider="gemini"
    bypassed = settings.model_copy(
        update={
            "environment": "prod_cn",
            "ai_primary_provider": "deepseek",
            "ai_asr_provider": "gemini",
        }
    )
    gw_prod = ModelGateway(bypassed, adapters={"deepseek": spy_deepseek, "gemini": FakeAdapter()})
    with pytest.raises(UpstreamAIError, match="prod_cn"):
        gw_prod._provider_chain_for_task("transcribe")


# ============================================================================
# 3. Context-Grounded ASR Homophone Restoration & Spoken Self-Correction
# ============================================================================


def test_postprocess_asr_homophones_self_corrections_and_idempotency() -> None:
    # 1. Homophone restoration from known_person_names & recent_context_snippets
    fixed = restore_context_homophones(
        "程经理和林雨今天点了桂花乌笼，还给妈妈买了纯羊容围巾",
        active_person_name="林屿",
        known_person_names=["陈经理", "妈妈"],
        recent_context_snippets=["喜欢喝桂花乌龙", "冬天只戴纯羊绒围巾"],
    )
    assert fixed == "陈经理和林屿今天点了桂花乌龙，还给妈妈买了纯羊绒围巾"

    # Never clobber an exact known person name even if phonetically close to another known person
    no_clobber = restore_context_homophones(
        "程经理和陈经理一起开会",
        known_person_names=["陈经理", "程经理"],
    )
    assert no_clobber == "程经理和陈经理一起开会"

    # 2. Mid-sentence spoken self-corrections
    assert resolve_spoken_self_corrections("约了周五，不对，是周六去吃饭") == "约了周六去吃饭"
    assert (
        resolve_spoken_self_corrections("和妈妈约了下周三复查，不对，是下周六")
        == "和妈妈约了下周六复查"
    )
    assert (
        resolve_spoken_self_corrections(
            "刚跟阿哲，说错了是阿远，吃晚饭",
            candidate_names=["阿远"],
        )
        == "刚跟阿远吃晚饭"
    )
    assert resolve_spoken_self_corrections("买了茉莉花茶，说错了是桂花乌龙") == "买了桂花乌龙"
    # Evaluative statement containing "不对，是..." must NOT be corrupted
    untouched = "我觉得这样不对，是要跟她好好沟通一下"
    assert resolve_spoken_self_corrections(untouched) == untouched

    # 3. End-to-end combined + idempotency
    combined = postprocess_asr_transcript(
        "程经理说约了周五，不对，是周六喝桂花乌笼",
        known_person_names=["陈经理"],
        recent_context_snippets=["喜欢喝桂花乌龙"],
    )
    assert combined == "陈经理说约了周六喝桂花乌龙"
    assert (
        postprocess_asr_transcript(
            combined,
            known_person_names=["陈经理"],
            recent_context_snippets=["喜欢喝桂花乌龙"],
        )
        == combined
    )


async def test_gateway_transcribe_injects_context_masks_pii_and_handles_silence() -> None:
    fake = FakeAdapter()
    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"), adapters={"fake": fake}
    )
    scope = UserScope(new_id())

    # Silence returns empty strings without hallucinating
    silent_out = await gw.transcribe(
        scope=scope,
        payload=TranscribeInput(
            audio_bytes=b"SILENCE",
            active_person_name="妈妈",
            known_person_names=["妈妈", "陈经理"],
        ),
    )
    assert silent_out.raw_transcript == ""
    assert silent_out.corrected_transcript == ""

    # Context-grounded transcription with PII masking on recent_context_snippets
    raw_id = "11010519950618127X"
    out = await gw.transcribe(
        scope=scope,
        payload=TranscribeInput(
            audio_bytes="TEXT:程经理说约了周五，不对，是周六喝桂花乌笼".encode(),
            active_person_name="陈经理",
            known_person_names=["陈经理", "林屿"],
            recent_context_snippets=[f"身份证 {raw_id}，爱喝桂花乌龙"],
        ),
    )
    assert out.raw_transcript == "程经理说约了周五，不对，是周六喝桂花乌笼"
    assert out.corrected_transcript == "陈经理说约了周六喝桂花乌龙"
    sent_prompt = fake.recorded_requests[-1].prompt
    assert raw_id not in sent_prompt
    assert "[REDACTED_ID_CARD]" in sent_prompt
    assert "<active_person>陈经理</active_person>" in sent_prompt
    assert "桂花乌龙" in sent_prompt


# ============================================================================
# 4. Implicit recent_facts Resolution, Multi-Event event_group, & Hallucination Strip
# ============================================================================


async def test_extract_record_recent_facts_implicit_resolution_and_ambiguity() -> None:
    fake = FakeAdapter()
    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"), adapters={"fake": fake}
    )
    scope = UserScope(new_id())
    mom_id = new_id()
    dad_id = new_id()
    lin_id = new_id()

    # Unique match: "上次说右膝盖疼那个复查改到下周六了" -> 妈妈
    out_unique, ver, _ = await gw.extract_record(
        scope=scope,
        payload=ExtractRecordInput(
            text="上次说右膝盖疼那个复查改到下周六了",
            current_date="2026-11-23",
            route_mode="global",
            known_persons=[
                KnownPersonRef(
                    person_id=mom_id,
                    display_name="妈妈",
                    tier=Tier.GOLD,
                    recent_facts=["最近总说右膝盖疼，天冷要戴护膝"],
                ),
                KnownPersonRef(
                    person_id=lin_id,
                    display_name="林屿",
                    tier=Tier.GOLD,
                    recent_facts=["喜欢喝桂花乌龙，对芒果过敏"],
                ),
            ],
            recent_history_hints=["妈妈11月提到右膝盖疼"],
        ),
    )
    assert ver == "extract_record@1.3.0"
    assert out_unique.needs_disambiguation is False
    assert len(out_unique.facts) == 1
    assert out_unique.facts[0].person_id == mom_id
    assert out_unique.facts[0].trigger_date == "2026-12-05"

    # Ambiguous match: BOTH 妈妈 and 爸爸 have "膝盖疼" -> MUST set needs_disambiguation=True!
    out_ambig, _, _ = await gw.extract_record(
        scope=scope,
        payload=ExtractRecordInput(
            text="上次说膝盖疼那个复查改到下周六了",
            current_date="2026-11-23",
            route_mode="global",
            known_persons=[
                KnownPersonRef(
                    person_id=mom_id,
                    display_name="妈妈",
                    tier=Tier.GOLD,
                    recent_facts=["最近总说膝盖疼要复查"],
                ),
                KnownPersonRef(
                    person_id=dad_id,
                    display_name="爸爸",
                    tier=Tier.GOLD,
                    recent_facts=["爬山后膝盖疼准备去复查"],
                ),
            ],
        ),
    )
    assert out_ambig.needs_disambiguation is True
    assert out_ambig.persons[0].matched_person_id is None
    assert set(out_ambig.persons[0].candidate_person_ids) == {mom_id, dad_id}
    assert all(f.person_id is None for f in out_ambig.facts)


async def test_extract_record_multi_event_group_and_multi_person_zero_leak() -> None:
    fake = FakeAdapter()
    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"), adapters={"fake": fake}
    )
    scope = UserScope(new_id())
    mom_id = new_id()
    chen_id = new_id()

    # Same person, 2 unrelated events + second person in same utterance
    out, _, _ = await gw.extract_record(
        scope=scope,
        payload=ExtractRecordInput(
            text="妈妈农历腊月初八过生日，另外她下周六要去医院复查右膝盖；陈经理这周五前要交合同签字表",
            current_date="2026-11-23",
            route_mode="global",
            known_persons=[
                KnownPersonRef(person_id=mom_id, display_name="妈妈", tier=Tier.GOLD),
                KnownPersonRef(person_id=chen_id, display_name="陈经理", tier=Tier.CRYSTAL),
            ],
        ),
    )
    mom_facts = [f for f in out.facts if f.person_id == mom_id]
    chen_facts = [f for f in out.facts if f.person_id == chen_id]

    assert len(mom_facts) == 2
    assert mom_facts[0].event_group == "evt_1"
    assert mom_facts[1].event_group == "evt_2"
    assert mom_facts[0].is_lunar is True and mom_facts[0].lunar_month == 12
    # Zero cross-person leakage
    assert all("陈经理" not in f.content and "签字" not in f.content for f in mom_facts)
    assert len(chen_facts) == 1
    assert chen_facts[0].event_group is None
    assert "妈妈" not in chen_facts[0].content and "膝盖" not in chen_facts[0].content


class _HallucinatingExtractAdapter:
    def __init__(self, fake_uuid: UUID, valid_uuid: UUID) -> None:
        self._fake_uuid = fake_uuid
        self._valid_uuid = valid_uuid

    @property
    def provider_name(self) -> str:
        return "fake"

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
        out = ExtractRecordOutput(
            safety_flag=SafetyCategory.NORMAL,
            needs_disambiguation=False,
            persons=[
                ExtractedPersonCandidate(
                    mention="幻觉人物",
                    matched_person_id=self._fake_uuid,
                    candidate_person_ids=[self._fake_uuid, self._valid_uuid],
                    confidence=0.95,
                )
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="幻觉人物",
                    person_id=self._fake_uuid,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    content="喜欢喝乌龙茶",
                    confidence=0.95,
                )
            ],
        )
        return AdapterCallResponse(
            text=out.model_dump_json(),
            model="fake-hallucinating",
            input_tokens=20,
            output_tokens=20,
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:  # pragma: no cover
        yield ""


async def test_gateway_extract_record_strips_hallucinated_person_ids_and_masks_recent_facts() -> (
    None
):
    valid_pid = new_id()
    bogus_pid = new_id()
    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"),
        adapters={"fake": _HallucinatingExtractAdapter(bogus_pid, valid_pid)},
    )
    out, _, _ = await gw.extract_record(
        scope=UserScope(new_id()),
        payload=ExtractRecordInput(
            text="某人喜欢喝乌龙茶",
            current_date="2026-11-23",
            known_persons=[
                KnownPersonRef(person_id=valid_pid, display_name="林屿", tier=Tier.GOLD)
            ],
        ),
    )
    assert out.needs_disambiguation is True
    assert out.persons[0].matched_person_id is None
    assert out.persons[0].candidate_person_ids == [valid_pid]
    assert out.facts[0].person_id is None

    # Verify PII in recent_facts & recent_history_hints is masked before reaching adapter
    spy_fake = FakeAdapter()
    gw_mask = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"),
        adapters={"fake": spy_fake},
    )
    secret_id = "11010519950618127X"
    await gw_mask.extract_record(
        scope=UserScope(new_id()),
        payload=ExtractRecordInput(
            text="妈妈喜欢纯羊绒围巾",
            current_date="2026-11-23",
            known_persons=[
                KnownPersonRef(
                    person_id=valid_pid,
                    display_name="妈妈",
                    tier=Tier.GOLD,
                    recent_facts=[f"身份证 {secret_id} 右膝盖疼"],
                )
            ],
            recent_history_hints=[f"历史备忘含身份证 {secret_id}"],
        ),
    )
    sent_prompt = spy_fake.recorded_requests[-1].prompt
    assert secret_id not in sent_prompt
    assert "[REDACTED_ID_CARD]" in sent_prompt


# ============================================================================
# 5. Warm, Natural, Keyword-Grounded Companion Replies (`FakeAdapter`)
# ============================================================================


async def test_fake_companion_keyword_grounded_demo_queries_do_not_dump_unrelated_records() -> None:
    gw = ModelGateway(Settings(environment="test", ai_primary_provider="fake"))
    scope = UserScope(new_id())
    mom_id = new_id()
    rec_scarf = new_id()
    rec_knee = new_id()
    rec_ribs = new_id()
    rec_sign = new_id()

    mom_facts = [
        ContextFactItem(
            record_id=rec_scarf,
            fact_id=new_id(),
            fact_type=FactType.PREFERENCE,
            content="冬天出门只戴纯羊绒围巾，对真丝蛋白过敏",
            happened_on="2026-11-01",
        ),
        ContextFactItem(
            record_id=rec_knee,
            fact_id=new_id(),
            fact_type=FactType.EVENT,
            content="右膝盖疼，约了下周六去骨科复查",
            happened_on="2026-11-05",
        ),
        ContextFactItem(
            record_id=rec_ribs,
            fact_id=new_id(),
            fact_type=FactType.PREFERENCE,
            content="最爱吃少糖的糖醋排骨",
            happened_on="2026-11-10",
        ),
    ]

    # Query 1: "围巾" -> ONLY rec_scarf!
    out_scarf, chat_ver, _ = await gw.generate_companion_reply(
        scope=scope,
        payload=CompanionChatInput(
            person_id=mom_id,
            person_name="妈妈",
            tier=Tier.GOLD,
            confirmed_facts=mom_facts,
            user_message="想给妈妈买条围巾，有什么讲究？",
        ),
    )
    assert chat_ver == "ask_relationship@1.4.0"
    assert out_scarf.cited_record_ids == [rec_scarf]
    assert "纯羊绒围巾" in out_scarf.reply_text
    assert "糖醋排骨" not in out_scarf.reply_text
    assert "右膝盖" not in out_scarf.reply_text

    # Query 2: "膝盖" -> ONLY rec_knee!
    out_knee, _, _ = await gw.generate_companion_reply(
        scope=scope,
        payload=CompanionChatInput(
            person_id=mom_id,
            person_name="妈妈",
            tier=Tier.GOLD,
            confirmed_facts=mom_facts,
            user_message="妈妈膝盖复查是哪天来着？",
        ),
    )
    assert out_knee.cited_record_ids == [rec_knee]
    assert "右膝盖" in out_knee.reply_text
    assert "围巾" not in out_knee.reply_text

    # Query 3: "排骨" -> ONLY rec_ribs (NOT rec_scarf's "过敏")!
    out_ribs, _, _ = await gw.generate_companion_reply(
        scope=scope,
        payload=CompanionChatInput(
            person_id=mom_id,
            person_name="妈妈",
            tier=Tier.GOLD,
            confirmed_facts=mom_facts,
            user_message="排骨怎么做她才爱吃？",
        ),
    )
    assert out_ribs.cited_record_ids == [rec_ribs]
    assert "糖醋排骨" in out_ribs.reply_text
    assert "围巾" not in out_ribs.reply_text

    # Query 4: "签字" grounded via referenced_materials on CRYSTAL person
    out_sign, _, _ = await gw.generate_companion_reply(
        scope=scope,
        payload=CompanionChatInput(
            person_id=new_id(),
            person_name="陈经理",
            tier=Tier.CRYSTAL,
            confirmed_facts=[
                ContextFactItem(
                    record_id=new_id(),
                    fact_id=new_id(),
                    fact_type=FactType.PREFERENCE,
                    content="开会喜欢喝桂花乌龙",
                    happened_on="2026-11-02",
                )
            ],
            referenced_materials=[
                ContextMaterialItem(
                    record_id=rec_sign,
                    kind="voice",
                    title="项目签字节点",
                    body="周五下午三点前要把季度合同签字版发给陈经理",
                    happened_on="2026-11-18",
                )
            ],
            user_message="陈经理那个签字什么时候截止？",
        ),
    )
    assert out_sign.cited_record_ids == [rec_sign]
    assert "签字" in out_sign.reply_text
    assert "桂花乌龙" not in out_sign.reply_text
    assert out_sign.alternative_perspectives == []
