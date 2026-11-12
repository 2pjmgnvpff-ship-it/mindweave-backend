"""Internal adapter protocol and error types for AI Model Gateway providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel


class AdapterError(Exception):
    """Base error raised by an AI provider adapter (zero user PII allowed in messages)."""


class AdapterTransientError(AdapterError):
    """Retryable upstream error (rate limit 429, 5xx, network transport, or timeout)."""


class AdapterPermanentError(AdapterError):
    """Non-retryable upstream error (invalid API key, forbidden model, bad request)."""


@dataclass(frozen=True, slots=True)
class AdapterCallRequest:
    """Normalized outbound model invocation request (all text already hard-PII masked)."""

    task: str
    model: str
    prompt: str
    temperature: float = 0.2
    timeout_seconds: float = 18.0
    response_schema: type[BaseModel] | None = None
    media_bytes: bytes | None = None
    media_mime_type: str | None = None
    structured_context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AdapterCallResponse:
    """Raw text/JSON output and token usage returned by a provider adapter."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int


class ProviderAdapter(Protocol):
    """Uniform async provider adapter contract."""

    @property
    def provider_name(self) -> str: ...

    async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse: ...

    def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]: ...
