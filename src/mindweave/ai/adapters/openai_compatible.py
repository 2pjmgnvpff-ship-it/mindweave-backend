"""M6-ready async OpenAI-compatible adapter for Alibaba Bailian (DashScope), Volcengine Ark, and DeepSeek.

Explicitly forbids reasoning models (`deepseek-reasoner` / `*-r1`) per ADR-001 due to first-token latency.
Implements verified Nov 16, 2026 Alibaba Cloud Model Studio `Qwen-Omni` audio wire format:
- `input_audio.format = "m4a"` for `audio/mp4` / `audio/m4a` (NEVER `"mp4"`, which DashScope maps to video)
- `input_audio.data = "data:;base64,<b64>"` with 7 MB raw size guard
- Mandatory `stream=True`, `modalities=["text"]`, `stream_options={"include_usage": True}`
- Model-specific thinking disable (`reasoning_effort="none"` on `qwen3.8`, `enable_thinking=False` on `qwen3`/`qwen3.5`)
- SSE reassembler capturing `usage` on empty-`choices` final chunk and skipping `delta.reasoning_content`
"""

from __future__ import annotations

import base64
import contextvars
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterPermanentError,
    AdapterTransientError,
)
from mindweave.ai.metering import estimate_token_count
from mindweave.core.logging import get_logger

logger = get_logger(__name__)

_TRANSIENT_HTTP_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_MAX_DASHSCOPE_RAW_AUDIO_BYTES: int = 7 * 1024 * 1024  # 7 MB raw (~9.3 MB base64 < 10 MB ceiling)

_AUDIO_MIME_TO_FORMAT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/amr": "amr",
    "audio/flac": "flac",
}


def validate_non_reasoning_model(model: str) -> None:
    """Enforce ADR-001 ban on DeepSeek-R1 / reasoning models."""
    lowered = model.strip().lower()
    if "reasoner" in lowered or "-r1" in lowered or "_r1" in lowered or lowered.endswith("r1"):
        raise AdapterPermanentError(
            f"Model '{model}' is forbidden by ADR-001 (reasoning models violate first-token latency)."
        )


def _normalize_mime_base(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


class OpenAICompatibleAdapter:
    """Async `httpx` OpenAI-compatible chat/vision/audio adapter for domestic M6 providers."""

    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str,
        base_url: str,
        default_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if default_model:
            validate_non_reasoning_model(default_model)
        self._provider_name = provider_name.strip().lower()
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._transport = transport
        self._stream_usage_var: contextvars.ContextVar[dict[str, int] | None] = (
            contextvars.ContextVar(f"openai_compat_stream_usage_{id(self)}", default=None)
        )

    @property
    def last_stream_usage(self) -> dict[str, int]:
        return dict(self._stream_usage_var.get() or {})

    @last_stream_usage.setter
    def last_stream_usage(self, value: dict[str, int]) -> None:
        self._stream_usage_var.set(dict(value))

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def supports_audio_input(self, model: str) -> bool:
        if self._provider_name == "deepseek":
            return False
        if self._provider_name == "dashscope":
            return "omni" in model.strip().lower()
        return False

    def _is_dashscope_omni(self, model: str) -> bool:
        return self._provider_name == "dashscope" and "omni" in model.strip().lower()

    def _build_messages(self, request: AdapterCallRequest, *, model: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if request.response_schema is not None:
            schema_str = json.dumps(
                request.response_schema.model_json_schema(),
                ensure_ascii=False,
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "请严格输出符合以下 JSON Schema 的合法 JSON 对象，严禁输出额外文字：\n"
                        f"{schema_str}"
                    ),
                }
            )

        if request.media_bytes is not None and request.media_mime_type:
            mime_base = _normalize_mime_base(request.media_mime_type)
            if mime_base.startswith("audio/") and not self.supports_audio_input(model):
                raise AdapterPermanentError(
                    f"Provider '{self._provider_name}' model '{model}' does not support audio input"
                )
            if mime_base.startswith("audio/") and self._provider_name == "dashscope":
                if len(request.media_bytes) > _MAX_DASHSCOPE_RAW_AUDIO_BYTES:
                    raise AdapterPermanentError(
                        "DashScope audio payload exceeds 7 MB raw size ceiling"
                    )
            b64 = base64.b64encode(request.media_bytes).decode("ascii")
            if mime_base.startswith("image/"):
                data_uri = f"data:{mime_base};base64,{b64}"
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_uri}},
                            {"type": "text", "text": request.prompt},
                        ],
                    }
                )
            elif mime_base.startswith("audio/"):
                audio_fmt = _AUDIO_MIME_TO_FORMAT.get(mime_base)
                if audio_fmt is None:
                    raise AdapterPermanentError(
                        f"Provider '{self._provider_name}' does not support audio mime type "
                        f"'{request.media_mime_type}'"
                    )
                audio_data = f"data:;base64,{b64}" if self._is_dashscope_omni(model) else b64
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": audio_data, "format": audio_fmt},
                            },
                            {"type": "text", "text": request.prompt},
                        ],
                    }
                )
            else:
                raise AdapterPermanentError(
                    f"Provider '{self._provider_name}' chat endpoint only accepts image/* or "
                    f"audio/* media; got '{request.media_mime_type}'"
                )
        else:
            messages.append({"role": "user", "content": request.prompt})

        return messages

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise AdapterPermanentError(f"API key for provider '{self._provider_name}' is missing")
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _apply_dashscope_thinking_flags(self, body: dict[str, Any], model: str) -> None:
        if self._provider_name != "dashscope":
            return
        m = model.strip().lower()
        if "qwen3.8" in m:
            body["reasoning_effort"] = "none"
        elif "qwen3" in m:
            body["enable_thinking"] = False

    async def _complete_via_sse(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        model: str,
        request: AdapterCallRequest,
    ) -> AdapterCallResponse:
        """Consume DashScope Qwen-Omni mandatory SSE stream into a single AdapterCallResponse."""
        content_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = None

        try:
            async with (
                httpx.AsyncClient(
                    timeout=request.timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream("POST", url, headers=headers, json=body) as resp,
            ):
                if resp.status_code in _TRANSIENT_HTTP_CODES:
                    raise AdapterTransientError(
                        f"{self._provider_name} transient HTTP {resp.status_code}"
                    )
                if resp.status_code >= 400:
                    raise AdapterPermanentError(
                        f"{self._provider_name} permanent HTTP {resp.status_code}"
                    )

                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    # CRITICAL: capture `usage` BEFORE `if not choices: continue` because DashScope
                    # emits final token usage on an empty-choices tail frame!
                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens") or prompt_tokens)
                        completion_tokens = int(usage.get("completion_tokens") or completion_tokens)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = str(fr)
                    delta = choices[0].get("delta") or {}
                    # Explicitly ignore `delta.reasoning_content`; collect only visible `content`.
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        content_parts.append(piece)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise AdapterTransientError(
                f"{self._provider_name} transport/timeout error: {type(exc).__name__}"
            ) from exc

        content = "".join(content_parts)
        in_tok = prompt_tokens or estimate_token_count(request.prompt)
        out_tok = completion_tokens or estimate_token_count(content)
        return AdapterCallResponse(
            text=content,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            finish_reason=finish_reason,
        )

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
        model = request.model or self._default_model or ""
        if not model:
            raise AdapterPermanentError(f"No model configured for '{self._provider_name}'")
        validate_non_reasoning_model(model)

        headers = self._headers()
        model_lower = model.strip().lower()
        is_dashscope_omni = self._is_dashscope_omni(model)

        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request, model=model),
            "temperature": request.temperature,
            "stream": is_dashscope_omni,
        }
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        elif self._provider_name == "deepseek" and request.task == "extract_record":
            body["max_tokens"] = 2048

        if request.response_schema is not None:
            body["response_format"] = {"type": "json_object"}

        if is_dashscope_omni:
            body["modalities"] = ["text"]
            body["stream_options"] = {"include_usage": True}
            self._apply_dashscope_thinking_flags(body, model)
            if "qwen-omni-turbo" in model_lower:
                # Legacy qwen-omni-turbo rejects response_format=json_object
                body.pop("response_format", None)
        elif self._provider_name == "dashscope" and request.response_schema is not None:
            self._apply_dashscope_thinking_flags(body, model)

        url = f"{self._base_url}/chat/completions"

        if is_dashscope_omni:
            return await self._complete_via_sse(
                url=url,
                headers=headers,
                body=body,
                model=model,
                request=request,
            )

        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_seconds,
                transport=self._transport,
            ) as client:
                resp = await client.post(url, headers=headers, json=body)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise AdapterTransientError(
                f"{self._provider_name} transport/timeout error: {type(exc).__name__}"
            ) from exc

        if resp.status_code in _TRANSIENT_HTTP_CODES:
            raise AdapterTransientError(f"{self._provider_name} transient HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise AdapterPermanentError(f"{self._provider_name} permanent HTTP {resp.status_code}")

        try:
            data = resp.json()
            choices = data.get("choices") or []
            message = choices[0].get("message") if choices else {}
            finish_reason = (
                str(choices[0].get("finish_reason"))
                if choices and choices[0].get("finish_reason")
                else None
            )
            raw_content = (message or {}).get("content")
            if isinstance(raw_content, list):
                content = "".join(
                    str(part.get("text") or "") for part in raw_content if isinstance(part, dict)
                )
            else:
                content = str(raw_content or "")
            usage = data.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens") or 0) or estimate_token_count(request.prompt)
            out_tok = int(usage.get("completion_tokens") or 0) or estimate_token_count(content)
        except Exception as exc:
            raise AdapterPermanentError(
                f"{self._provider_name} malformed response envelope"
            ) from exc

        if (
            self._provider_name == "deepseek"
            and request.response_schema is not None
            and not content.strip()
        ):
            raise AdapterTransientError("deepseek returned empty JSON content")

        return AdapterCallResponse(
            text=content,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            finish_reason=finish_reason,
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
        model = request.model or self._default_model or ""
        if not model:
            raise AdapterPermanentError(f"No model configured for '{self._provider_name}'")
        validate_non_reasoning_model(model)

        self.last_stream_usage = {}
        headers = self._headers()
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request, model=model),
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        if self._provider_name == "dashscope":
            self._apply_dashscope_thinking_flags(body, model)

        url = f"{self._base_url}/chat/completions"

        try:
            async with (
                httpx.AsyncClient(
                    timeout=request.timeout_seconds,
                    transport=self._transport,
                ) as client,
                client.stream("POST", url, headers=headers, json=body) as resp,
            ):
                if resp.status_code in _TRANSIENT_HTTP_CODES:
                    raise AdapterTransientError(
                        f"{self._provider_name} stream transient HTTP {resp.status_code}"
                    )
                if resp.status_code >= 400:
                    raise AdapterPermanentError(
                        f"{self._provider_name} stream permanent HTTP {resp.status_code}"
                    )

                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        chunk_obj = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk_obj.get("usage")
                    if isinstance(usage, dict):
                        self.last_stream_usage = {
                            "input_tokens": int(usage.get("prompt_tokens") or 0),
                            "output_tokens": int(usage.get("completion_tokens") or 0),
                        }
                    choices = chunk_obj.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason") == "length":
                        logger.warning(
                            "openai_compatible_stream_truncated_length",
                            provider=self._provider_name,
                            model=model,
                        )
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield str(piece)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise AdapterTransientError(
                f"{self._provider_name} stream transport/timeout error: {type(exc).__name__}"
            ) from exc
