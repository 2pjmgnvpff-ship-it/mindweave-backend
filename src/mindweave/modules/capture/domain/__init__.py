"""Pure domain exports for the capture module."""

from mindweave.modules.capture.domain.gate_policy import (
    GateConfig,
    GateDecision,
    GateInput,
    decide,
    decide_gate,
)
from mindweave.modules.capture.domain.highlighter import (
    build_natural_slip_tag,
    build_transcript_html,
    format_duration_mmss,
)
from mindweave.modules.capture.domain.media_sniffer import (
    AudioFormatInfo,
    InvalidMediaFormatError,
    MediaTooLargeError,
    MediaValidationError,
    sniff_audio_magic,
    validate_and_sniff_audio,
)
from mindweave.modules.capture.domain.routing import (
    CandidateHint,
    KnownPersonProfile,
    MentionResolution,
    clause_mentions_multiple_known_people,
    derive_monogram,
    is_pronoun_or_empty_mention,
    normalize_mention,
    resolve_mention_for_fact,
)
from mindweave.modules.capture.domain.screenshot_rules import (
    ScreenshotFilterResult,
    filter_screenshot_facts,
    format_screenshot_bubbles,
    is_allowed_for_speaker,
    is_system_noise_bubble,
    partition_screenshot_facts,
)

__all__ = [
    "AudioFormatInfo",
    "CandidateHint",
    "GateConfig",
    "GateDecision",
    "GateInput",
    "InvalidMediaFormatError",
    "KnownPersonProfile",
    "MediaTooLargeError",
    "MediaValidationError",
    "MentionResolution",
    "ScreenshotFilterResult",
    "build_natural_slip_tag",
    "build_transcript_html",
    "clause_mentions_multiple_known_people",
    "decide",
    "decide_gate",
    "derive_monogram",
    "filter_screenshot_facts",
    "format_duration_mmss",
    "format_screenshot_bubbles",
    "is_allowed_for_speaker",
    "is_pronoun_or_empty_mention",
    "is_system_noise_bubble",
    "normalize_mention",
    "partition_screenshot_facts",
    "resolve_mention_for_fact",
    "sniff_audio_magic",
    "validate_and_sniff_audio",
]
