"""Pure domain exports for MindWeave privacy-safe telemetry."""

from mindweave.modules.telemetry.domain.allowlist import (
    ALLOWED_DATA_RIGHTS_STATUSES,
    ALLOWED_FACT_TYPES,
    ALLOWED_REMINDER_RULES,
    ALLOWED_SOURCES,
    ALLOWED_TIERS,
    ALLOWED_TRIGGER_CATEGORIES,
    ALLOWED_UNDO_REASONS,
    EVENT_METADATA_ALLOWLIST,
    MAX_METADATA_STR_LEN,
    MAX_TELEMETRY_BATCH_SIZE,
    TelemetryPolicyViolationError,
    TelemetrySummary,
    ValidatedTelemetryEvent,
    contains_pii_or_unsafe_string,
    validate_telemetry_batch,
    validate_telemetry_event,
)

__all__ = [
    "ALLOWED_DATA_RIGHTS_STATUSES",
    "ALLOWED_FACT_TYPES",
    "ALLOWED_REMINDER_RULES",
    "ALLOWED_SOURCES",
    "ALLOWED_TIERS",
    "ALLOWED_TRIGGER_CATEGORIES",
    "ALLOWED_UNDO_REASONS",
    "EVENT_METADATA_ALLOWLIST",
    "MAX_METADATA_STR_LEN",
    "MAX_TELEMETRY_BATCH_SIZE",
    "TelemetryPolicyViolationError",
    "TelemetrySummary",
    "ValidatedTelemetryEvent",
    "contains_pii_or_unsafe_string",
    "validate_telemetry_batch",
    "validate_telemetry_event",
]
