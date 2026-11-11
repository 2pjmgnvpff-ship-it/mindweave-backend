"""AI Model Gateway provider adapters."""

from mindweave.ai.adapters.base import (
    AdapterCallRequest,
    AdapterCallResponse,
    AdapterError,
    AdapterPermanentError,
    AdapterTransientError,
    ProviderAdapter,
)
from mindweave.ai.adapters.fake import FakeAdapter, FakeFaultConfig, strip_prompt_injections
from mindweave.ai.adapters.gemini import GeminiAdapter
from mindweave.ai.adapters.openai_compatible import (
    OpenAICompatibleAdapter,
    validate_non_reasoning_model,
)

__all__ = [
    "AdapterCallRequest",
    "AdapterCallResponse",
    "AdapterError",
    "AdapterPermanentError",
    "AdapterTransientError",
    "FakeAdapter",
    "FakeFaultConfig",
    "GeminiAdapter",
    "OpenAICompatibleAdapter",
    "ProviderAdapter",
    "strip_prompt_injections",
    "validate_non_reasoning_model",
]
