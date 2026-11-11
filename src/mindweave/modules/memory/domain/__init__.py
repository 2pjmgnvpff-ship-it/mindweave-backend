"""Pure domain exports for the memory module."""

from mindweave.modules.memory.domain.bitemporal import (
    ExistingFactSnapshot,
    FactSupersessionPatch,
    NewFactCandidate,
    detect_polarity_flip,
    extract_topic_tokens,
    find_facts_to_supersede,
    has_explicit_update_marker,
    is_polar_opposite,
)
from mindweave.modules.memory.domain.inbox_policy import (
    InboxDomainError,
    InboxResolutionInput,
    validate_inbox_resolution,
)

__all__ = [
    "ExistingFactSnapshot",
    "FactSupersessionPatch",
    "InboxDomainError",
    "InboxResolutionInput",
    "NewFactCandidate",
    "detect_polarity_flip",
    "extract_topic_tokens",
    "find_facts_to_supersede",
    "has_explicit_update_marker",
    "is_polar_opposite",
    "validate_inbox_resolution",
]
