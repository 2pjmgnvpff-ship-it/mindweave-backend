"""Strict allowlist & zero-PII validator for MindWeave telemetry events (PRD §8, Invariant I10).

Pure domain module: zero FastAPI, SQLAlchemy, ORM, DB, or HTTP dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mindweave.shared.enums import CaptureSource, FactType, ReminderRule, Tier
from mindweave.shared.pii import mask_hard_pii

MAX_TELEMETRY_BATCH_SIZE = 50
MAX_METADATA_STR_LEN = 32

# PRD §8 canonical event names -> strictly allowlisted metadata keys
EVENT_METADATA_ALLOWLIST: dict[str, frozenset[str]] = {
    "record_created": frozenset({"source", "latency_ms"}),
    "fact_confirmed": frozenset({"fact_type", "confidence_bucket"}),
    "fact_edited": frozenset({"fact_type", "confidence_bucket"}),
    "fact_rejected": frozenset({"fact_type", "confidence_bucket"}),
    "record_undo": frozenset({"reason_optional"}),
    "reminder_opened": frozenset({"fact_type", "rule"}),
    "reminder_action_done": frozenset({"fact_type", "rule"}),
    "person_page_revisit": frozenset({"records_count_bucket", "tier"}),
    "ask_replied": frozenset({"cited_count", "has_gap", "tier"}),
    "draft_copied": frozenset({"cited_count", "has_gap", "tier"}),
    "crisis_flow_entered": frozenset({"trigger_category"}),
    "data_export": frozenset({"status"}),
    "account_delete": frozenset({"status"}),
}

ALLOWED_SOURCES: frozenset[str] = frozenset(s.value for s in CaptureSource)
ALLOWED_FACT_TYPES: frozenset[str] = frozenset(ft.value for ft in FactType)
ALLOWED_TIERS: frozenset[str] = frozenset(t.value for t in Tier)
ALLOWED_REMINDER_RULES: frozenset[str] = frozenset(r.value for r in ReminderRule)
ALLOWED_UNDO_REASONS: frozenset[str] = frozenset(
    {
        "wrong_person",
        "mistake",
        "wrong_type",
        "duplicate",
        "changed_mind",
        "unspecified",
    }
)
ALLOWED_TRIGGER_CATEGORIES: frozenset[str] = frozenset(
    {
        "crisis",
        "violence_victim",
        "illegal_intent",
        "third_party_attack",
        "boundary_refusal",
        "late_night_care",
    }
)
ALLOWED_DATA_RIGHTS_STATUSES: frozenset[str] = frozenset(
    {
        "requested",
        "pending",
        "processing",
        "completed",
        "failed",
        "expired",
        "downloaded",
        "shredded",
    }
)

_SAFE_BUCKET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+\-]{1,24}$")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CN_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_JWT_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.")

MetadataScalar = str | int | float | bool


class TelemetryPolicyViolationError(ValueError):
    """Raised when telemetry event_name or metadata violates PRD §8 / Invariant I10."""

    def __init__(self, detail: str, *, reason_code: str = "telemetry_policy_violation") -> None:
        super().__init__(detail)
        self.detail = detail
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ValidatedTelemetryEvent:
    event_name: str
    metadata: dict[str, MetadataScalar]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TelemetrySummary:
    total_events: int
    counts_by_event: dict[str, int]
    first_event_at: datetime | None
    last_event_at: datetime | None


def contains_pii_or_unsafe_string(value: str) -> str | None:
    """Return a reason code if a string metadata value exceeds bounds or contains PII."""
    if len(value) > MAX_METADATA_STR_LEN:
        return "metadata_string_too_long"
    if "\n" in value or "\r" in value:
        return "metadata_multiline_forbidden"
    if _EMAIL_RE.search(value):
        return "metadata_pii_email"
    if _CN_MOBILE_RE.search(value):
        return "metadata_pii_phone"
    if _JWT_LIKE_RE.search(value):
        return "metadata_secret_token"
    if mask_hard_pii(value).any_masked:
        return "metadata_hard_pii"
    return None


def _validate_metadata_entry(key: str, value: Any) -> MetadataScalar:
    if isinstance(value, str):
        violation = contains_pii_or_unsafe_string(value)
        if violation is not None:
            raise TelemetryPolicyViolationError(
                f"Metadata key '{key}' failed privacy/length validation ({violation}).",
                reason_code=violation,
            )

    if key == "source":
        if not isinstance(value, str) or value not in ALLOWED_SOURCES:
            raise TelemetryPolicyViolationError(
                "Metadata 'source' must be one of voice|text|image|chat.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "latency_ms":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 600_000:
            raise TelemetryPolicyViolationError(
                "Metadata 'latency_ms' must be an integer between 0 and 600000.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "fact_type":
        if not isinstance(value, str) or value not in ALLOWED_FACT_TYPES:
            raise TelemetryPolicyViolationError(
                "Metadata 'fact_type' must be a valid canonical FactType.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key in ("confidence_bucket", "records_count_bucket"):
        if not isinstance(value, str) or not _SAFE_BUCKET_TOKEN_RE.fullmatch(value):
            raise TelemetryPolicyViolationError(
                f"Metadata '{key}' must be an ASCII bucket code (no free-form text or names).",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "reason_optional":
        if not isinstance(value, str) or value not in ALLOWED_UNDO_REASONS:
            raise TelemetryPolicyViolationError(
                "Metadata 'reason_optional' must be an allowlisted undo reason code.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "rule":
        if not isinstance(value, str) or value not in ALLOWED_REMINDER_RULES:
            raise TelemetryPolicyViolationError(
                "Metadata 'rule' must be one of once|yearly.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "tier":
        if not isinstance(value, str) or value not in ALLOWED_TIERS:
            raise TelemetryPolicyViolationError(
                "Metadata 'tier' must be one of gold|silver|crystal.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "cited_count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 500:
            raise TelemetryPolicyViolationError(
                "Metadata 'cited_count' must be an integer between 0 and 500.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "has_gap":
        if not isinstance(value, bool):
            raise TelemetryPolicyViolationError(
                "Metadata 'has_gap' must be a boolean.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "trigger_category":
        if not isinstance(value, str) or value not in ALLOWED_TRIGGER_CATEGORIES:
            raise TelemetryPolicyViolationError(
                "Metadata 'trigger_category' must be an allowlisted safety category.",
                reason_code="invalid_metadata_value",
            )
        return value

    if key == "status":
        if not isinstance(value, str) or value not in ALLOWED_DATA_RIGHTS_STATUSES:
            raise TelemetryPolicyViolationError(
                "Metadata 'status' must be an allowlisted lifecycle status.",
                reason_code="invalid_metadata_value",
            )
        return value

    raise TelemetryPolicyViolationError(
        "Unrecognized metadata key.",
        reason_code="forbidden_metadata_key",
    )


def validate_telemetry_event(
    event_name: str,
    metadata: Mapping[str, Any] | None,
    *,
    occurred_at: datetime,
) -> ValidatedTelemetryEvent:
    cleaned_name = event_name.strip()
    allowed_keys = EVENT_METADATA_ALLOWLIST.get(cleaned_name)
    if allowed_keys is None:
        raise TelemetryPolicyViolationError(
            "Unknown telemetry event_name is not in PRD §8 allowlist.",
            reason_code="unknown_event_name",
        )

    raw_meta: Mapping[str, Any] = metadata if metadata is not None else {}
    if not isinstance(raw_meta, Mapping):
        raise TelemetryPolicyViolationError(
            "Telemetry metadata must be a JSON object.",
            reason_code="invalid_metadata_type",
        )

    sanitized_meta: dict[str, MetadataScalar] = {}
    for raw_key, raw_val in raw_meta.items():
        if not isinstance(raw_key, str) or raw_key not in allowed_keys:
            raise TelemetryPolicyViolationError(
                f"Metadata key is not allowlisted for event '{cleaned_name}'.",
                reason_code="forbidden_metadata_key",
            )
        if raw_val is None and raw_key == "reason_optional":
            continue
        if isinstance(raw_val, dict | list | tuple | set):
            raise TelemetryPolicyViolationError(
                f"Metadata key '{raw_key}' must be a scalar value.",
                reason_code="nested_metadata_forbidden",
            )
        sanitized_meta[raw_key] = _validate_metadata_entry(raw_key, raw_val)

    normalized_ts = (
        occurred_at.replace(tzinfo=UTC)
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC)
    )
    return ValidatedTelemetryEvent(
        event_name=cleaned_name,
        metadata=sanitized_meta,
        occurred_at=normalized_ts,
    )


def validate_telemetry_batch(
    events: Sequence[tuple[str, Mapping[str, Any] | None, datetime]],
) -> list[ValidatedTelemetryEvent]:
    if len(events) == 0:
        raise TelemetryPolicyViolationError(
            "Telemetry batch must contain at least 1 event.",
            reason_code="empty_batch",
        )
    if len(events) > MAX_TELEMETRY_BATCH_SIZE:
        raise TelemetryPolicyViolationError(
            f"Telemetry batch size cannot exceed {MAX_TELEMETRY_BATCH_SIZE} events.",
            reason_code="batch_too_large",
        )
    return [validate_telemetry_event(name, meta, occurred_at=ts) for name, meta, ts in events]
