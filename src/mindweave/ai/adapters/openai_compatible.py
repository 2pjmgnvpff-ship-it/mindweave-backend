"""M6-ready async OpenAI-compatible adapter for Alibaba Bailian (DashScope), Volcengine Ark, and DeepSeek.

Explicitly forbids reasoning models (`deepseek-reasoner` / `*-r1`) per ADR-001 due to first-token latency.
"""

from __future__ import annotations

import base64
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

_TRANSIENT_HTTP_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

_AUDIO_MIME_TO_FORMAT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/m4a": "mp4",
    "audio/x-m4a": "mp4",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
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

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _build_messages(self, request: AdapterCallRequest) -> list[dict[str, Any]]:
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
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": b64, "format": audio_fmt},
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

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
        model = request.model or self._default_model or ""
        if not model:
            raise AdapterPermanentError(f"No model configured for '{self._provider_name}'")
        validate_non_reasoning_model(model)

        headers = self._headers()
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request),
            "temperature": request.temperature,
            "stream": False,
        }
        if request.response_schema is not None:
            body["response_format"] = {"type": "json_object"}

        url = f"{self._base_url}/chat/completions"
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

        return AdapterCallResponse(
            text=content,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
        model = request.model or self._default_model or ""
        if not model:
            raise AdapterPermanentError(f"No model configured for '{self._provider_name}'")
        validate_non_reasoning_model(model)

        headers = self._headers()
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(request),
            "temperature": request.temperature,
            "stream": True,
        }
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
                    choices = chunk_obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield str(piece)
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise AdapterTransientError(
                f"{self._provider_name} stream transport/timeout error: {type(exc).__name__}"
            ) from exc
