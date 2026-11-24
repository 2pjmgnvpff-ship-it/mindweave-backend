"""V2 Native Voice, Multimodal AI Routing, Deterministic Extractor & Range Media unit tests.

Covers:
- T1: Invariant I13 — FakeAdapter NEVER fabricates fake Chinese text from opaque binary audio.
- T2: Multi-person & multi-event split with `happened_at <= current_date` and future `trigger_date`.
- T3: Gemini 3.8 Flash `_build_config` (`MINIMAL -> LOW` clamp, `response_json_schema`,
      `FinishReason.MAX_TOKENS` handling, `thoughts_token_count` billing, `stream()` config).
- T4: DashScope Qwen3-Omni wire format (`format="m4a"`, `stream=True`, `data:;base64,`,
      usage captured on empty-`choices` chunk) & DeepSeek `supports_audio_input() is False`.
- T5: Deterministic local extractor (`extract_deterministically`) 3-event split +
      contrast-safe spoken self-correction (`resolve_spoken_self_corrections`).
- T6: C1 Recent Captures RFC 9110 Range media response (`200`/`206`/`416` + `nosniff`).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from google.genai import types

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterPermanentError,
    AdapterTransientError,
)
from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.adapters.gemini import GeminiAdapter
from mindweave.ai.adapters.openai_compatible import OpenAICompatibleAdapter
from mindweave.ai.deterministic_extractor import extract_deterministically
from mindweave.ai.gateway import ModelGateway
from mindweave.ai.schemas.contracts import (
    ExtractRecordInput,
    ExtractRecordOutput,
    KnownPersonRef,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.ai.validators.asr_postprocess import resolve_spoken_self_corrections
from mindweave.core.config import Settings
from mindweave.core.errors import UpstreamAIError
from mindweave.core.ids import new_id
from mindweave.modules.capture.api.router import _build_range_media_response
from mindweave.shared.enums import FactType, Tier
from mindweave.shared.scopes import UserScope

# ============================================================================
# T1: Invariant I13 — Zero-Fabrication ASR Guard on Opaque Binary Audio
# ============================================================================


@pytest.mark.asyncio
async def test_t1_fake_adapter_never_fabricates_transcript_from_opaque_audio() -> None:
    fake = FakeAdapter()
    opaque_m4a = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\xab" * 256
    opaque_wav = b"RIFF\x24\x01\x00\x00WAVEfmt " + b"\x00" * 128

    for raw_bytes, mime in ((opaque_m4a, "audio/m4a"), (opaque_wav, "audio/wav")):
        with pytest.raises(AdapterPermanentError, match="No live ASR provider configured"):
            await fake.complete(
                AdapterCallRequest(
                    task="transcribe",
                    model="fake-asr",
                    prompt="请如实转写",
                    media_bytes=raw_bytes,
                    media_mime_type=mime,
                    structured_context={
                        "payload": {
                            "audio_bytes": raw_bytes,
                            "mime_type": mime,
                            "active_person_name": "妈妈",
                            "known_person_names": ["妈妈", "林屿"],
                        }
                    },
                )
            )

    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake", ai_fallback_provider="none"),
        adapters={"fake": fake},
    )
    with pytest.raises(UpstreamAIError):
        await gw.transcribe(
            scope=UserScope(new_id()),
            payload=TranscribeInput(
                audio_bytes=opaque_m4a,
                mime_type="audio/m4a",
                active_person_name="妈妈",
                known_person_names=["妈妈"],
            ),
        )


# ============================================================================
# T2: Multi-Person & Multi-Event Split + Temporal Invariant (`happened_at <= today`)
# ============================================================================


def test_t2_multi_person_multi_event_and_temporal_invariant() -> None:
    mom_id = new_id()
    wang_a = new_id()
    wang_b = new_id()

    out = extract_deterministically(
        ExtractRecordInput(
            text="妈妈下周六要去医院复查膝盖，另外她平时最喜欢喝桂花乌龙茶；陈经理这周五前要收项目合同签字表",
            current_date="2026-11-23",
            route_mode="global",
            known_persons=[
                KnownPersonRef(
                    person_id=mom_id,
                    display_name="妈妈",
                    tier=Tier.GOLD,
                    role_tag="家人",
                ),
                KnownPersonRef(
                    person_id=wang_a,
                    display_name="陈经理",
                    tier=Tier.SILVER,
                    role_tag="同事",
                ),
                KnownPersonRef(
                    person_id=wang_b,
                    display_name="小王",
                    tier=Tier.CRYSTAL,
                    role_tag="大学同学",
                ),
            ],
        )
    )
    assert out.needs_cloud_refinement is True
    mom_facts = [f for f in out.facts if f.person_id == mom_id]
    chen_facts = [f for f in out.facts if f.person_id == wang_a]

    assert len(mom_facts) == 2
    assert len(chen_facts) == 1
    assert all(f.source_span for f in out.facts)

    # Future trigger_date MUST NOT be copied into happened_at (> current_date forbidden!)
    date_facts = [f for f in out.facts if f.trigger_date is not None]
    assert len(date_facts) >= 1
    for fact in out.facts:
        if fact.happened_at is not None:
            assert fact.happened_at <= "2026-11-23"
        if fact.fact_type is FactType.DATE and fact.trigger_date and fact.trigger_date > "2026-11-23":
            assert fact.happened_at is None


# ============================================================================
# T3: Gemini 3.8 Flash `_build_config`, `MINIMAL -> LOW`, `MAX_TOKENS`, `thoughts_token_count`
# ============================================================================


class _StubUsage:
    def __init__(self, prompt: int, candidates: int, thoughts: int = 0) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts


class _StubCandidate:
    def __init__(self, finish_reason: Any) -> None:
        self.finish_reason = finish_reason


class _StubGeminiResponse:
    def __init__(
        self,
        text: str,
        *,
        usage: _StubUsage | None = None,
        finish_reason: Any = "STOP",
    ) -> None:
        self.text = text
        self.usage_metadata = usage
        self.candidates = [_StubCandidate(finish_reason)]


class _SpyGemini3Models:
    def __init__(self, *, finish_reason: Any = "STOP", thoughts: int = 42) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self._finish_reason = finish_reason
        self._thoughts = thoughts

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _StubGeminiResponse(
            '{"facts": [], "persons": [], "safety_flag": "normal", "needs_disambiguation": false}',
            usage=_StubUsage(prompt=120, candidates=60, thoughts=self._thoughts),
            finish_reason=self._finish_reason,
        )

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
        self.stream_calls.append({"model": model, "contents": contents, "config": config})

        async def _iter() -> AsyncIterator[Any]:
            yield _StubGeminiResponse(
                "<reply>你好，",
                usage=_StubUsage(prompt=80, candidates=12, thoughts=20),
            )
            yield _StubGeminiResponse(
                "我帮你记着呢。</reply>",
                usage=_StubUsage(prompt=80, candidates=25, thoughts=35),
            )

        return _iter()


class _SpyGemini3Client:
    def __init__(self, *, finish_reason: Any = "STOP", thoughts: int = 42) -> None:
        self.models = _SpyGemini3Models(finish_reason=finish_reason, thoughts=thoughts)
        self.aio = self


@pytest.mark.asyncio
async def test_t3_gemini_3_8_flash_config_thinking_clamp_metering_and_max_tokens() -> None:
    spy = _SpyGemini3Client(finish_reason="STOP", thoughts=45)
    adapter = GeminiAdapter(api_key="test-key", client=spy)

    # 1. MINIMAL requested on gemini-3.8-flash MUST clamp to LOW (never HTTP 400!), omit temperature,
    #    use response_json_schema (not response_schema), and bill thoughts_token_count in output_tokens.
    resp = await adapter.complete(
        AdapterCallRequest(
            task="safety_classify",
            model="gemini-3.8-flash",
            prompt="分类安全级别",
            temperature=0.0,
            thinking_level="MINIMAL",
            response_schema=ExtractRecordOutput,
        )
    )
    cfg = spy.models.calls[0]["config"]
    assert cfg.temperature is None
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema is not None
    assert resp.input_tokens == 120
    assert resp.output_tokens == 60 + 45  # candidates + thoughts billed honestly!

    # 2. stream() MUST also use _build_config(streaming=True) (omits temperature & response_json_schema)
    chunks = [
        c
        async for c in adapter.stream(
            AdapterCallRequest(
                task="ask_relationship",
                model="gemini-3.8-flash",
                prompt="聊聊妈妈的近况",
                temperature=0.6,
            )
        )
    ]
    assert "".join(chunks) == "<reply>你好，我帮你记着呢。</reply>"
    stream_cfg = spy.models.stream_calls[0]["config"]
    assert stream_cfg.temperature is None
    assert stream_cfg.thinking_config is not None
    assert stream_cfg.thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    assert stream_cfg.response_json_schema is None
    assert adapter.last_stream_usage["output_tokens"] == 25 + 35

    # 3. FinishReason.MAX_TOKENS on structured JSON response MUST raise AdapterTransientError!
    spy_trunc = _SpyGemini3Client(finish_reason="MAX_TOKENS", thoughts=10)
    adapter_trunc = GeminiAdapter(api_key="test-key", client=spy_trunc)
    with pytest.raises(AdapterTransientError, match="MAX_TOKENS"):
        await adapter_trunc.complete(
            AdapterCallRequest(
                task="extract_record",
                model="gemini-3.8-flash",
                prompt="提取记录",
                response_schema=ExtractRecordOutput,
            )
        )


# ============================================================================
# T4: DashScope Qwen3-Omni Wire Format & DeepSeek Audio Capability Guard
# ============================================================================


@pytest.mark.asyncio
async def test_t4_dashscope_qwen3_omni_and_deepseek_audio_guard() -> None:
    captured: list[dict[str, Any]] = []
    raw_m4a = b"\x00\x00\x00\x1cftypM4A fake-audio-payload"

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        frames = [
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"reasoning_content": "internal thought ignored"}}]}
            ),
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "content": '{"raw_transcript":"明天买花","corrected_transcript":"明天买花","duration_ms":1500}'
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "data: "
            + json.dumps({"choices": [], "usage": {"prompt_tokens": 111, "completion_tokens": 22}}),
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("\n\n".join(frames) + "\n\n").encode("utf-8"),
        )

    dashscope = OpenAICompatibleAdapter(
        provider_name="dashscope",
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3-omni-flash",
        transport=httpx.MockTransport(_handler),
    )
    assert dashscope.supports_audio_input("qwen3-omni-flash") is True
    assert dashscope.supports_audio_input("qwen-plus") is False

    resp = await dashscope.complete(
        AdapterCallRequest(
            task="transcribe",
            model="qwen3-omni-flash",
            prompt="转写",
            response_schema=TranscribeOutput,
            media_bytes=raw_m4a,
            media_mime_type="audio/mp4",
        )
    )
    assert "internal thought ignored" not in resp.text
    assert '"corrected_transcript":"明天买花"' in resp.text
    assert resp.input_tokens == 111
    assert resp.output_tokens == 22
    req_body = captured[0]
    assert req_body["stream"] is True
    assert req_body["modalities"] == ["text"]
    assert req_body["stream_options"] == {"include_usage": True}
    audio_part = req_body["messages"][-1]["content"][0]["input_audio"]
    assert audio_part["format"] == "m4a"
    assert audio_part["data"] == f"data:;base64,{base64.b64encode(raw_m4a).decode('ascii')}"

    # DeepSeek MUST refuse audio input cleanly
    deepseek = OpenAICompatibleAdapter(
        provider_name="deepseek",
        api_key="sk-ds-test",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    )
    assert deepseek.supports_audio_input("deepseek-chat") is False
    with pytest.raises(AdapterPermanentError, match="does not support audio input"):
        await deepseek.complete(
            AdapterCallRequest(
                task="transcribe",
                model="deepseek-chat",
                prompt="转写",
                media_bytes=raw_m4a,
                media_mime_type="audio/m4a",
            )
        )


# ============================================================================
# T5: Contrast-Safe Spoken Self-Correction & Multi-Clause Deterministic Split
# ============================================================================


def test_t5_spoken_self_correction_and_contrast_preservation() -> None:
    # Genuine mid-sentence repair: Friday -> Saturday
    repaired_day = resolve_spoken_self_corrections("妈妈复查改周五了，不对，是周六")
    assert "周六" in repaired_day
    assert "周五" not in repaired_day

    # Genuine person repair: 阿哲 -> 阿远
    repaired_person = resolve_spoken_self_corrections(
        "刚才跟阿哲，说错了是阿远，约了周末爬山",
        candidate_names=["阿哲", "阿远"],
    )
    assert "阿远" in repaired_person
    assert "阿哲" not in repaired_person

    # Contrast / evaluative sentence MUST NOT be corrupted by self-correction regex!
    contrast_sentence = "我觉得这样做不太对，她应该先沟通清楚"
    assert resolve_spoken_self_corrections(contrast_sentence) == contrast_sentence


# ============================================================================
# T6: RFC 9110 Range Media Response (200 / 206 / 416 + X-Content-Type-Options)
# ============================================================================


def test_t6_range_media_response_200_206_416_and_nosniff() -> None:
    payload = b"0123456789abcdefghij"  # 20 bytes

    r200 = _build_range_media_response(payload, "audio/mp4", None)
    assert r200.status_code == 200
    assert r200.body == payload
    assert r200.headers["accept-ranges"] == "bytes"
    assert r200.headers["content-length"] == "20"
    assert r200.headers["x-content-type-options"] == "nosniff"
    assert r200.headers["cache-control"] == "private, no-store"

    # Safari AVPlayer initial probe: bytes=0-1
    r206_probe = _build_range_media_response(payload, "audio/mp4", "bytes=0-1")
    assert r206_probe.status_code == 206
    assert r206_probe.body == b"01"
    assert r206_probe.headers["content-range"] == "bytes 0-1/20"
    assert r206_probe.headers["content-length"] == "2"
    assert r206_probe.headers["x-content-type-options"] == "nosniff"

    # Suffix range: bytes=-5
    r206_suffix = _build_range_media_response(payload, "audio/mp4", "bytes=-5")
    assert r206_suffix.status_code == 206
    assert r206_suffix.body == b"fghij"
    assert r206_suffix.headers["content-range"] == "bytes 15-19/20"

    # Out-of-bounds range: bytes=99-120 -> 416
    r416 = _build_range_media_response(payload, "audio/mp4", "bytes=99-120")
    assert r416.status_code == 416
    assert r416.headers["content-range"] == "bytes */20"
    assert r416.headers["x-content-type-options"] == "nosniff"
