"""Infrastructure adapters for the Companion module."""

from mindweave.modules.companion.infra.context_builder import (
    BuiltCompanionContext,
    CitationRecordMeta,
    ContextBuilder,
)
from mindweave.modules.companion.infra.repository import (
    CompanionRepository,
    HydratedChatMessage,
    HydratedCitation,
)

__all__ = [
    "BuiltCompanionContext",
    "CitationRecordMeta",
    "CompanionRepository",
    "ContextBuilder",
    "HydratedChatMessage",
    "HydratedCitation",
]
