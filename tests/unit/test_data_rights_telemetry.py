"""Unit tests for Data Sovereignty (GB 45438-2025 export & token security) and Privacy-Safe Telemetry."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from mindweave.core.dto import contains_forbidden_dev_jargon
from mindweave.core.security import hash_opaque_token
from mindweave.modules.data_rights.domain import (
    AIGC_STANDARD_GB45438,
    EXPORT_SCHEMA_VERSION,
    JOURNAL_AIGC_DISCLOSURE_FOOTER,
    ExportMediaItem,
    allocate_media_archive_path,
    package_export_zip,
    verify_one_time_download_token,
)
from mindweave.modules.telemetry.domain import (
    EVENT_METADATA_ALLOWLIST,
    MAX_TELEMETRY_BATCH_SIZE,
    TelemetryPolicyViolationError,
    validate_telemetry_batch,
    validate_telemetry_event,
)
from mindweave.shared.pii import is_luhn_valid, is_valid_cn_id18


def test_prd_section_8_allowlist_accepts_all_canonical_events() -> None:
    now = datetime(2026, 11, 12, 8, 0, 0, tzinfo=UTC)
    valid_samples: list[tuple[str, dict[str, object]]] = [
        ("record_created", {"source": "voice", "latency_ms": 380}),
        ("fact_confirmed", {"fact_type": "preference", "confidence_bucket": "0.9-1.0"}),
        ("fact_edited", {"fact_type": "date", "confidence_bucket": "0.8-0.9"}),
        ("fact_rejected", {"fact_type": "taboo", "confidence_bucket": "lt_0.8"}),
        ("record_undo", {"reason_optional": "wrong_person"}),
        ("record_undo", {"reason_optional": None}),
        ("reminder_opened", {"fact_type": "date", "rule": "yearly"}),
        ("reminder_action_done", {"fact_type": "promise", "rule": "once"}),
        ("person_page_revisit", {"records_count_bucket": "6-20", "tier": "gold"}),
        ("ask_replied", {"cited_count": 3, "has_gap": False, "tier": "gold"}),
        ("draft_copied", {"cited_count": 2, "has_gap": True, "tier": "silver"}),
        ("crisis_flow_entered", {"trigger_category": "crisis"}),
        ("data_export", {"status": "completed"}),
        ("account_delete", {"status": "shredded"}),
    ]
    assert {name for name, _ in valid_samples} == set(EVENT_METADATA_ALLOWLIST.keys())

    for event_name, meta in valid_samples:
        validated = validate_telemetry_event(event_name, meta, occurred_at=now)
        assert validated.event_name == event_name
        assert validated.occurred_at == now


@pytest.mark.parametrize(
    ("event_name", "metadata", "expected_reason"),
    [
        ("unknown_custom_event", {}, "unknown_event_name"),
        ("record_created", {"text": "今天和沈清秋去镰仓"}, "forbidden_metadata_key"),
        ("record_created", {"body": "secret diary"}, "forbidden_metadata_key"),
        ("person_page_revisit", {"name": "沈清秋"}, "forbidden_metadata_key"),
        ("person_page_revisit", {"person_name": "沈清秋"}, "forbidden_metadata_key"),
        ("ask_replied", {"message": "帮我写道歉信"}, "forbidden_metadata_key"),
        ("data_export", {"email": "user@example.com"}, "forbidden_metadata_key"),
        (
            "fact_confirmed",
            {"confidence_bucket": "a" * 33},
            "metadata_string_too_long",
        ),
        (
            "fact_confirmed",
            {"confidence_bucket": "a@b.co"},
            "metadata_pii_email",
        ),
        (
            "fact_confirmed",
            {"confidence_bucket": "u13800138000"},
            "metadata_pii_phone",
        ),
        (
            "fact_confirmed",
            {"confidence_bucket": "沈清秋"},
            "invalid_metadata_value",
        ),
        (
            "record_undo",
            {"reason_optional": "不小心把林屿记成陈教授了"},
            "invalid_metadata_value",
        ),
        (
            "record_created",
            {"source": "voice", "latency_ms": True},
            "invalid_metadata_value",
        ),
        (
            "ask_replied",
            {"cited_count": 1, "has_gap": "yes", "tier": "gold"},
            "invalid_metadata_value",
        ),
    ],
)
def test_telemetry_validator_rejects_unallowlisted_keys_long_strings_and_pii(
    event_name: str,
    metadata: dict[str, object],
    expected_reason: str,
) -> None:
    now = datetime(2026, 11, 12, 8, 0, 0, tzinfo=UTC)
    with pytest.raises(TelemetryPolicyViolationError) as exc_info:
        validate_telemetry_event(event_name, metadata, occurred_at=now)
    assert exc_info.value.reason_code == expected_reason
    # Ensure raw sensitive string values are never echoed back in error messages (Invariant I10)
    assert "沈清秋" not in exc_info.value.detail
    assert "user@example.com" not in exc_info.value.detail


def test_telemetry_validator_blocks_hard_pii_and_enforces_batch_bounds() -> None:
    now = datetime(2026, 11, 12, 8, 0, 0, tzinfo=UTC)

    # Valid 18-digit mainland ID card (<= 32 chars) must be caught as hard PII
    prefix17 = "11010519950618123"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check = "10X98765432"[sum(int(d) * w for d, w in zip(prefix17, weights, strict=True)) % 11]
    valid_id = prefix17 + check
    assert is_valid_cn_id18(valid_id)

    with pytest.raises(TelemetryPolicyViolationError) as id_exc:
        validate_telemetry_event(
            "fact_confirmed",
            {"confidence_bucket": valid_id},
            occurred_at=now,
        )
    assert id_exc.value.reason_code == "metadata_hard_pii"

    # Valid 16-digit Luhn bank card (<= 32 chars) must be caught as hard PII
    base15 = "622202123456789"
    valid_card = next(base15 + d for d in "0123456789" if is_luhn_valid(base15 + d))
    with pytest.raises(TelemetryPolicyViolationError) as card_exc:
        validate_telemetry_event(
            "fact_confirmed",
            {"confidence_bucket": valid_card},
            occurred_at=now,
        )
    assert card_exc.value.reason_code == "metadata_hard_pii"

    # Batch size boundaries: 0 rejected, 50 accepted, 51 rejected
    with pytest.raises(TelemetryPolicyViolationError) as empty_exc:
        validate_telemetry_batch([])
    assert empty_exc.value.reason_code == "empty_batch"

    max_batch = [
        ("data_export", {"status": "completed"}, now) for _ in range(MAX_TELEMETRY_BATCH_SIZE)
    ]
    assert len(validate_telemetry_batch(max_batch)) == MAX_TELEMETRY_BATCH_SIZE

    with pytest.raises(TelemetryPolicyViolationError) as overflow_exc:
        validate_telemetry_batch([*max_batch, ("data_export", {"status": "completed"}, now)])
    assert overflow_exc.value.reason_code == "batch_too_large"


def test_package_export_zip_gb45438_metadata_and_warm_journal_markdown() -> None:
    used_paths: set[str] = set()
    p1 = allocate_media_archive_path("u1/audio/memo.webm", used_paths)
    p2 = allocate_media_archive_path("u1/records/memo.webm", used_paths)
    assert p1 == "media/memo.webm"
    assert p2 == "media/memo_2.webm"

    sections = {
        "persons": [
            {
                "id": "p-gold",
                "tier": "gold",
                "rank": "i",
                "displayName": "沈清秋",
                "roleTag": "伴侣",
                "shortMemo": "偏爱桂花乌龙与海边日落",
            },
            {
                "id": "p-silver",
                "tier": "silver",
                "rank": "m",
                "displayName": "张主管",
                "roleTag": "前部门主管",
                "shortMemo": "会议甩锅需书面留痕",
            },
            {
                "id": "p-crystal",
                "tier": "crystal",
                "rank": "q",
                "displayName": "陈教授",
                "roleTag": "论文导师",
                "shortMemo": "周二上午办公时间",
            },
        ],
        "materials": [
            {
                "id": "rec-1",
                "personId": "p-gold",
                "kind": "voice",
                "title": "周五午后 · 海边看海鸥约定",
                "body": "记得带拍立得相纸与燕麦拿铁",
                "happenedOn": "2026-11-10",
            }
        ],
        "facts": [
            {
                "id": "f-1",
                "personId": "p-gold",
                "type": "taboo",
                "status": "confirmed",
                "content": "对芒果严重过敏",
                "triggerDate": None,
            }
        ],
        "reminders": [
            {
                "id": "rem-1",
                "personId": "p-gold",
                "title": "清秋生日",
                "rule": "yearly",
                "status": "active",
            }
        ],
        "chatMessages": [
            {
                "id": "msg-1",
                "personId": "p-gold",
                "role": "assistant",
                "content": "她对芒果过敏 [rec_01]，甜品建议选桂花冻。",
            }
        ],
    }
    media = [
        ExportMediaItem(
            object_key="u1/audio/memo.webm",
            archive_path=p1,
            content_type="audio/webm",
            data=b"RIFF-decrypted-voice-bytes",
        )
    ]

    bundle = package_export_zip(
        export_id="exp-001",
        user_id="user-001",
        exported_at_iso="2026-11-12T04:00:00+00:00",
        sections=sections,
        media_items=media,
    )

    assert hashlib.sha256(bundle.zip_bytes).hexdigest() == bundle.zip_sha256_hex

    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes), mode="r") as zf:
        names = set(zf.namelist())
        assert names == {"data.json", "journal.md", "media/memo.webm"}

        comment_meta = json.loads(zf.comment.decode("utf-8"))
        assert comment_meta["_aigcMetadata"]["standard"] == AIGC_STANDARD_GB45438

        data_doc = json.loads(zf.read("data.json").decode("utf-8"))
        assert data_doc["schemaVersion"] == EXPORT_SCHEMA_VERSION
        aigc = data_doc["_aigcMetadata"]
        assert aigc["standard"] == "GB 45438-2025"
        assert aigc["contentGeneratedByAI"] is True
        assert aigc["provider"] == "MindWeave"
        assert aigc["exportedAt"] == "2026-11-12T04:00:00+00:00"
        assert len(aigc["contentSha256"]) == 64

        journal_text = zf.read("journal.md").decode("utf-8")
        assert "## 金箭 · 记爱" in journal_text
        assert "## 银箭 · 记恨" in journal_text
        assert "## 水晶箭 · 记繁琐" in journal_text
        assert "沈清秋" in journal_text
        assert "张主管" in journal_text
        assert "陈教授" in journal_text
        assert JOURNAL_AIGC_DISCLOSURE_FOOTER in journal_text
        # Invariant I6: zero raw [rec_...] citation IDs or banned developer jargon in rendered journal
        assert contains_forbidden_dev_jargon(journal_text) is None

        assert zf.read("media/memo.webm") == b"RIFF-decrypted-voice-bytes"


def test_one_time_download_token_constant_time_and_expiry_rules() -> None:
    now = datetime(2026, 11, 12, 4, 0, 0, tzinfo=UTC)
    raw_token = "tok_high_entropy_one_time_download_secret"
    stored_hash = hash_opaque_token(raw_token)
    expires = now + timedelta(hours=24)

    assert (
        verify_one_time_download_token(
            provided_token=raw_token,
            stored_token_hash=stored_hash,
            status="completed",
            expires_at=expires,
            downloaded_at=None,
            now=now,
        )
        is None
    )

    assert (
        verify_one_time_download_token(
            provided_token="wrong-token",
            stored_token_hash=stored_hash,
            status="completed",
            expires_at=expires,
            downloaded_at=None,
            now=now,
        )
        == "invalid_token"
    )

    assert (
        verify_one_time_download_token(
            provided_token=raw_token,
            stored_token_hash=stored_hash,
            status="completed",
            expires_at=now,
            downloaded_at=None,
            now=now,
        )
        == "expired"
    )

    assert (
        verify_one_time_download_token(
            provided_token=raw_token,
            stored_token_hash=stored_hash,
            status="completed",
            expires_at=expires,
            downloaded_at=now - timedelta(minutes=1),
            now=now,
        )
        == "already_downloaded"
    )

    assert (
        verify_one_time_download_token(
            provided_token=raw_token,
            stored_token_hash=stored_hash,
            status="pending",
            expires_at=expires,
            downloaded_at=None,
            now=now,
        )
        == "export_not_ready"
    )
