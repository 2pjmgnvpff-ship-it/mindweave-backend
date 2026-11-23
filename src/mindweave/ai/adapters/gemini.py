"""Async Google GenAI (`google.genai`) multimodal & structured output adapter.

Architectural Contract (`.importlinter`): `google.genai` MUST ONLY be imported inside
`mindweave.ai.adapters`.
"""

from __future__ import annotations

import asyncio
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

_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


_GEMINI_MIME_ALIASES: dict[str, str] = {
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/opus": "audio/ogg",
}


class GeminiAdapter:
    """Production async adapter for Google Gemini (`gemini-2.5-flash` / `gemini-2.5-pro`)."""

    def __init__(self, *, api_key: str, client: Any | None = None) -> None:
        self._api_key = api_key.strip()
        self._client = client

    @property
    def provider_name(self) -> str:
        return "gemini"

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

    def _build_config(self, request: AdapterCallRequest) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "temperature": request.temperature,
        }
        if request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = request.response_schema
        return types.GenerateContentConfig(**kwargs)

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
        config = self._build_config(request)

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

        text = getattr(response, "text", None) or ""
        usage = getattr(response, "usage_metadata", None)
        in_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        if in_tokens <= 0:
            in_tokens = estimate_token_count(request.prompt)
        if out_tokens <= 0 and text:
            out_tokens = estimate_token_count(text)

        return AdapterCallResponse(
            text=text,
            model=request.model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
        client = self._get_client()
        contents = self._build_contents(request)
        config = types.GenerateContentConfig(temperature=request.temperature)

        try:
            stream_resp = await asyncio.wait_for(
                client.aio.models.generate_content_stream(
                    model=request.model,
                    contents=contents,
                    config=config,
                ),
                timeout=request.timeout_seconds,
            )
            async for chunk in stream_resp:
                piece = getattr(chunk, "text", None)
                if piece:
                    yield str(piece)
        except Exception as exc:
            raise self._map_exception(exc) from exc
