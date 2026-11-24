"""Async Google GenAI (`google.genai`) multimodal & structured output adapter.

Architectural Contract (`.importlinter`): `google.genai` MUST ONLY be imported inside
`mindweave.ai.adapters`.

Upgraded for Gemini 3.8 Flash / 3.5 Flash-Lite (`google-genai>=3.2.0`):
- Generation-aware `_build_config(..., streaming=...)` used by BOTH `complete()` and `stream()`.
- Gemini 3.x / 4.x uses `ThinkingConfig(thinking_level=...)` (clamping `MINIMAL -> LOW` on
  `gemini-3.8-flash` where `MINIMAL` throws HTTP 400!), native `response_json_schema`, and omits
  deprecated/forbidden params (`temperature`, `thinking_budget`, `frequency_penalty`, etc.).
- Legacy Gemini 2.5-* override path keeps `temperature` + `thinking_budget=0` + `response_schema`.
- Guards against `FinishReason.MAX_TOKENS` JSON truncation (`python-genai#782`), captures
  `thoughts_token_count` into billable `output_tokens`, enforces 25s per-chunk stream timeouts,
  and strips leaked `THOUGHT:` prefixes (`python-genai#2121`).
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterPermanentError,
    AdapterTransientError,
)
from mindweave.ai.metering import estimate_token_count
from mindweave.core.logging import get_logger

logger = get_logger(__name__)

_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

_GEMINI_MIME_ALIASES: dict[str, str] = {
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/aac",
    "audio/flac": "audio/flac",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/opus": "audio/ogg",
}

_THOUGHT_PREFIX_RE = re.compile(r"^\s*THOUGHT\s*:\s*.*?(?=\n\s*\n|<\s*reply\b|\Z)", re.IGNORECASE | re.DOTALL)
_STREAM_CHUNK_IDLE_TIMEOUT_SEC: float = 25.0


class GeminiAdapter:
    """Production async adapter for Google Gemini 3.8 Flash / 3.5 Flash-Lite (with legacy 2.5 compat)."""

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._api_key = api_key.strip()
        self._client = client
        self._stream_usage_var: contextvars.ContextVar[dict[str, int] | None] = (
            contextvars.ContextVar(f"gemini_stream_usage_{id(self)}", default=None)
        )

    @property
    def last_stream_usage(self) -> dict[str, int]:
        return dict(self._stream_usage_var.get() or {})

    @last_stream_usage.setter
    def last_stream_usage(self, value: dict[str, int]) -> None:
        self._stream_usage_var.set(dict(value))

    @property
    def provider_name(self) -> str:
        return "gemini"

    @staticmethod
    def supports_audio_input(model: str) -> bool:
        return model.strip().lower().startswith("gemini-")

    @staticmethod
    def _is_gemini_3_plus(model: str) -> bool:
        m = model.strip().lower()
        return m.startswith("gemini-3") or m.startswith("gemini-4")

    @staticmethod
    def _resolve_thinking_level(model: str, task: str, requested: str | None) -> Any:
        default_level = (
            "MEDIUM"
            if task == "ask_relationship"
            else ("MINIMAL" if task in ("safety_classify", "summarize_thread") else "LOW")
        )
        level = (requested or default_level).strip().upper()
        # CRITICAL: gemini-3.8-flash rejects MINIMAL with HTTP 400!
        if level == "MINIMAL" and "3.8" in model.lower():
            level = "LOW"
        if hasattr(types, "ThinkingLevel"):
            return getattr(types.ThinkingLevel, level, types.ThinkingLevel.LOW)
        return level

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise AdapterPermanentError("Gemini API key is not configured")
        self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def _normalize_mime_type(raw_mime: str) -> str:
        base = raw_mime.split(";", 1)[0].strip().lower()
        return _GEMINI_MIME_ALIASES.get(base, base)

    def _build_contents(self, request: AdapterCallRequest) -> Any:
        if request.media_bytes is not None and request.media_mime_type:
            media_part = types.Part.from_bytes(
                data=request.media_bytes,
                mime_type=self._normalize_mime_type(request.media_mime_type),
            )
            return [media_part, request.prompt]
        return request.prompt

    def _build_config(
        self,
        request: AdapterCallRequest,
        *,
        streaming: bool = False,
    ) -> types.GenerateContentConfig:
        model_lower = request.model.strip().lower()
        kwargs: dict[str, Any] = {}

        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens
        elif request.task == "extract_record":
            kwargs["max_output_tokens"] = 8192
        else:
            kwargs["max_output_tokens"] = 4096

        if self._is_gemini_3_plus(model_lower):
            # Gemini 3.x / 4.x: use thinking_level, omit temperature & thinking_budget!
            if hasattr(types, "ThinkingConfig"):
                kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=self._resolve_thinking_level(
                        model_lower, request.task, request.thinking_level
                    )
                )
        else:
            # Legacy Gemini 2.5-* compatibility path
            kwargs["temperature"] = request.temperature
            if (
                not streaming
                and model_lower.startswith("gemini-2.5-flash")
                and hasattr(types, "ThinkingConfig")
            ):
                if request.task in (
                    "transcribe",
                    "safety_classify",
                    "summarize_thread",
                    "extract_record",
                ):
                    kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        if not streaming and request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            if self._is_gemini_3_plus(model_lower):
                kwargs["response_json_schema"] = request.response_schema.model_json_schema()
            else:
                kwargs["response_schema"] = request.response_schema

        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _extract_finish_reason(response: Any) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        raw_reason = getattr(candidates[0], "finish_reason", None)
        if raw_reason is None:
            return None
        return str(getattr(raw_reason, "value", raw_reason))

    @staticmethod
    def _is_max_tokens_finish(response: Any) -> bool:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return False
        raw_reason = getattr(candidates[0], "finish_reason", None)
        if raw_reason is None:
            return False
        if hasattr(types, "FinishReason") and raw_reason == types.FinishReason.MAX_TOKENS:
            return True
        return str(getattr(raw_reason, "value", raw_reason)).upper().endswith("MAX_TOKENS")

    @staticmethod
    def _extract_non_thought_text(chunk: Any) -> str:
        """Extract visible text from a chunk while skipping `part.thought == True` parts."""
        candidates = getattr(chunk, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            if parts:
                pieces: list[str] = []
                for part in parts:
                    if getattr(part, "thought", False) is True:
                        continue
                    part_text = getattr(part, "text", None)
                    if part_text:
                        pieces.append(str(part_text))
                return "".join(pieces)
        raw_text = getattr(chunk, "text", None)
        return str(raw_text) if raw_text else ""

    @staticmethod
    def _map_exception(exc: Exception) -> Exception:
        if isinstance(exc, AdapterTransientError | AdapterPermanentError):
            return exc
        if isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError):
            return AdapterTransientError(f"Gemini transport/timeout failure: {type(exc).__name__}")
        if isinstance(exc, genai_errors.APIError):
            code = int(getattr(exc, "code", 0) or 0)
            if code in _TRANSIENT_STATUS_CODES:
                return AdapterTransientError(f"Gemini transient HTTP {code}")
            return AdapterPermanentError(f"Gemini non-retryable HTTP {code}")
        return AdapterTransientError(f"Gemini unexpected upstream failure: {type(exc).__name__}")

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
        client = self._get_client()
        contents = self._build_contents(request)
        config = self._build_config(request, streaming=False)

        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=request.model,
                    contents=contents,
                    config=config,
                ),
                timeout=request.timeout_seconds,
            )
        except Exception as exc:
            raise self._map_exception(exc) from exc

        # python-genai#782: thinking tokens share max_output_tokens; never feed truncated JSON to Pydantic!
        if self._is_max_tokens_finish(response):
            raise AdapterTransientError("Gemini response truncated by MAX_TOKENS")

        text = self._extract_non_thought_text(response)
        usage = getattr(response, "usage_metadata", None)
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        cand_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
        cached_tokens = int(getattr(usage, "cached_content_token_count", 0) or 0)
        # Google bills thoughts_token_count at the output token rate!
        out_tokens = cand_tokens + thoughts_tokens

        if in_tokens <= 0:
            in_tokens = estimate_token_count(request.prompt)
        if out_tokens <= 0 and text:
            out_tokens = estimate_token_count(text)

        finish_reason = self._extract_finish_reason(response)
        if thoughts_tokens > 0 or cached_tokens > 0:
            logger.debug(
                "gemini_token_telemetry",
                task=request.task,
                model=request.model,
                thoughts_tokens=thoughts_tokens,
                cached_tokens=cached_tokens,
                finish_reason=finish_reason,
            )

        return AdapterCallResponse(
            text=text,
            model=request.model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            thoughts_tokens=thoughts_tokens,
            cached_tokens=cached_tokens,
            finish_reason=finish_reason,
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
        client = self._get_client()
        contents = self._build_contents(request)
        config = self._build_config(request, streaming=True)
        self.last_stream_usage = {}

        try:
            stream_resp = await asyncio.wait_for(
                client.aio.models.generate_content_stream(
                    model=request.model,
                    contents=contents,
                    config=config,
                ),
                timeout=request.timeout_seconds,
            )
            chunk_iter = stream_resp.__aiter__()
            leading_buffer = ""
            stripped_thought_phase = True
            chunk_timeout = max(5.0, min(_STREAM_CHUNK_IDLE_TIMEOUT_SEC, request.timeout_seconds))

            while True:
                try:
                    chunk = await asyncio.wait_for(chunk_iter.__anext__(), timeout=chunk_timeout)
                except StopAsyncIteration:
                    break

                usage = getattr(chunk, "usage_metadata", None)
                if usage is not None:
                    in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
                    cand_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
                    thought_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
                    cached_tok = int(getattr(usage, "cached_content_token_count", 0) or 0)
                    if in_tok or cand_tok or thought_tok:
                        self.last_stream_usage = {
                            "input_tokens": in_tok,
                            "output_tokens": cand_tok + thought_tok,
                            "thoughts_tokens": thought_tok,
                            "cached_tokens": cached_tok,
                        }

                piece = self._extract_non_thought_text(chunk)
                if not piece:
                    continue

                # python-genai#2121: strip leaked leading `THOUGHT:` prefix on gemini-3.x streams!
                if stripped_thought_phase:
                    leading_buffer += piece
                    lstrip_buf = leading_buffer.lstrip()
                    if not lstrip_buf:
                        continue
                    upper_head = lstrip_buf[:8].upper()
                    if "THOUGHT:".startswith(upper_head) and len(lstrip_buf) < 8:
                        continue
                    if upper_head.startswith("THOUGHT:"):
                        # Wait until blank line or <reply> delimiter (bounded to 512 chars)
                        reply_idx = leading_buffer.lower().find("<reply>")
                        if reply_idx != -1:
                            cleaned = leading_buffer[reply_idx:]
                            leading_buffer = ""
                            stripped_thought_phase = False
                            if cleaned:
                                yield cleaned
                            continue
                        nl_idx = leading_buffer.find("\n\n")
                        if nl_idx != -1:
                            cleaned = leading_buffer[nl_idx + 2 :]
                            leading_buffer = ""
                            stripped_thought_phase = False
                            if cleaned:
                                yield cleaned
                            continue
                        if len(leading_buffer) < 512:
                            continue
                        cleaned = _THOUGHT_PREFIX_RE.sub("", leading_buffer)
                        leading_buffer = ""
                        stripped_thought_phase = False
                        if cleaned:
                            yield cleaned
                        continue
                    stripped_thought_phase = False
                    out_chunk = leading_buffer
                    leading_buffer = ""
                    yield out_chunk
                else:
                    yield piece

            if leading_buffer:
                cleaned_tail = _THOUGHT_PREFIX_RE.sub("", leading_buffer)
                if cleaned_tail:
                    yield cleaned_tail
        except Exception as exc:
            raise self._map_exception(exc) from exc
