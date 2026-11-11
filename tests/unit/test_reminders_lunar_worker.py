"""Unit tests for solar/lunar reminder scheduling, desensitized push copy, and worker invariants."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from mindweave.core.ids import new_id
from mindweave.modules.reminders.domain import (
    FORBIDDEN_ANTHROPOMORPHIC_PUSH_PHRASES,
    PushCopySafetyError,
    ReminderScheduleError,
    build_desensitized_push_copy,
    compute_next_fire_slot,
    resolve_lunar_yearly_date,
    resolve_next_lunar_occurrence_date,
    resolve_solar_yearly_date,
    validate_push_copy_safety,
)
from mindweave.modules.reminders.infra.push import (
    APNsLogAdapter,
    InMemoryPushRecorder,
    PushMessage,
    WebPushLogAdapter,
)
from mindweave.shared.enums import PushPlatform, ReminderRule
from mindweave.workers.queue import JobPayloadPrivacyError, sanitize_and_validate_job_payload
from mindweave.workers.runner import compute_backoff_seconds, sanitize_job_error
from mindweave.workers.scheduler import build_cron_idempotency_key


def test_once_reminder_progresses_through_lead_days_and_completes() -> None:
    target = date(2026, 11, 20)
    # 2026-11-10 09:30 Asia/Shanghai == 2026-11-10 01:30 UTC
    created_at = datetime(2026, 11, 10, 1, 30, 0, tzinfo=UTC)

    slot_7 = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=target,
        lead_days=[7, 1, 0],
        fire_hour=9,
        fire_minute=30,
        timezone="Asia/Shanghai",
        reference_utc=created_at,
    )
    assert slot_7 is not None
    assert slot_7.lead_day == 7
    assert slot_7.occurrence_date == target
    assert slot_7.scheduled_for_utc == datetime(2026, 11, 13, 1, 30, 0, tzinfo=UTC)

    # Initial creation at exact slot timestamp is inclusive (>= reference_utc)
    exact_slot = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=target,
        lead_days=[7, 1, 0],
        fire_hour=9,
        fire_minute=30,
        timezone="Asia/Shanghai",
        reference_utc=slot_7.scheduled_for_utc,
    )
    assert exact_slot == slot_7

    # Advancing strictly > after_utc moves 7 -> 1 -> 0 -> None
    slot_1 = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=target,
        lead_days=[7, 1, 0],
        fire_hour=9,
        fire_minute=30,
        timezone="Asia/Shanghai",
        after_utc=slot_7.scheduled_for_utc,
    )
    assert slot_1 is not None
    assert slot_1.lead_day == 1
    assert slot_1.scheduled_for_utc == datetime(2026, 11, 19, 1, 30, 0, tzinfo=UTC)

    slot_0 = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=target,
        lead_days=[7, 1, 0],
        fire_hour=9,
        fire_minute=30,
        timezone="Asia/Shanghai",
        after_utc=slot_1.scheduled_for_utc,
    )
    assert slot_0 is not None
    assert slot_0.lead_day == 0
    assert slot_0.scheduled_for_utc == datetime(2026, 11, 20, 1, 30, 0, tzinfo=UTC)

    exhausted = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=target,
        lead_days=[7, 1, 0],
        fire_hour=9,
        fire_minute=30,
        timezone="Asia/Shanghai",
        after_utc=slot_0.scheduled_for_utc,
    )
    assert exhausted is None


def test_solar_yearly_feb29_falls_back_to_feb28_in_non_leap_years() -> None:
    assert resolve_solar_yearly_date(2024, 2, 29) == date(2024, 2, 29)
    assert resolve_solar_yearly_date(2025, 2, 29) == date(2025, 2, 28)
    assert resolve_solar_yearly_date(2026, 2, 29) == date(2026, 2, 28)
    assert resolve_solar_yearly_date(2028, 2, 29) == date(2028, 2, 29)

    # Full schedule rollover from 2024 leap day -> 2025 Feb 28 -> 2028 Feb 29
    slot_2025 = compute_next_fire_slot(
        rule=ReminderRule.YEARLY,
        is_lunar=False,
        target_month=2,
        target_day=29,
        lead_days=[0],
        fire_hour=8,
        fire_minute=0,
        timezone="Asia/Shanghai",
        after_utc=datetime(2024, 2, 29, 2, 0, 0, tzinfo=UTC),
    )
    assert slot_2025 is not None
    assert slot_2025.occurrence_date == date(2025, 2, 28)
    assert slot_2025.scheduled_for_utc == datetime(2025, 2, 28, 0, 0, 0, tzinfo=UTC)

    slot_2028 = compute_next_fire_slot(
        rule=ReminderRule.YEARLY,
        is_lunar=False,
        target_month=2,
        target_day=29,
        lead_days=[0],
        fire_hour=8,
        fire_minute=0,
        timezone="Asia/Shanghai",
        reference_utc=datetime(2028, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    assert slot_2028 is not None
    assert slot_2028.occurrence_date == date(2028, 2, 29)

    with pytest.raises(ReminderScheduleError):
        resolve_solar_yearly_date(2026, 2, 30)
    with pytest.raises(ReminderScheduleError):
        resolve_solar_yearly_date(2026, 4, 31)


def test_lunar_yearly_leap_month_and_30_day_small_month_fallbacks() -> None:
    # 2023 (癸卯年) has leap 2nd lunar month (闰二月):
    # Regular 二月十五 = 2023-03-06; Leap 闰二月十五 = 2023-04-05.
    assert resolve_lunar_yearly_date(2023, 2, 15, is_leap_month=False) == date(2023, 3, 6)
    assert resolve_lunar_yearly_date(2023, 2, 15, is_leap_month=True) == date(2023, 4, 5)

    # 2024 (甲辰年) has NO leap month -> falls back cleanly to regular 二月十五 (2024-03-24)
    assert resolve_lunar_yearly_date(2024, 2, 15, is_leap_month=True) == date(2024, 3, 24)
    assert resolve_lunar_yearly_date(2024, 2, 15, is_leap_month=False) == date(2024, 3, 24)

    # 2025 (乙巳年) has 闰六月, NOT 闰二月 -> 闰二月 birthday falls back to regular month 2
    assert resolve_lunar_yearly_date(2025, 2, 15, is_leap_month=True) == resolve_lunar_yearly_date(
        2025, 2, 15, is_leap_month=False
    )

    # 30-day lunar birthday (腊月三十):
    # Lunar 2023 腊月 has 30 days -> 2024-02-09.
    # Lunar 2024 腊月 only has 29 days (小月, 2025除夕为腊月廿九) -> clamps cleanly to day 29 (2025-01-28)!
    assert resolve_lunar_yearly_date(2023, 12, 30) == date(2024, 2, 9)
    assert resolve_lunar_yearly_date(2024, 12, 30) == date(2025, 1, 28)

    # Stress-check 25 consecutive lunar years for 30th-day & leap-month combinations: zero crashes!
    for lunar_year in range(2020, 2045):
        d1 = resolve_lunar_yearly_date(lunar_year, 1, 30, is_leap_month=False)
        d2 = resolve_lunar_yearly_date(lunar_year, 2, 30, is_leap_month=True)
        assert d1.year >= lunar_year
        assert d2.year >= lunar_year


def test_lunar_schedule_handles_january_before_chinese_new_year() -> None:
    # On 2025-01-15 UTC (still lunar year 2024 腊月!), lunar 12-23 (小年) falls on 2025-01-22.
    slot = compute_next_fire_slot(
        rule=ReminderRule.YEARLY,
        is_lunar=True,
        target_month=12,
        target_day=23,
        lead_days=[1, 0],
        fire_hour=9,
        fire_minute=0,
        timezone="Asia/Shanghai",
        reference_utc=datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC),
    )
    assert slot is not None
    assert slot.occurrence_date == date(2025, 1, 22)
    assert slot.lead_day == 1
    assert slot.scheduled_for_utc == datetime(2025, 1, 21, 1, 0, 0, tzinfo=UTC)


def test_lunar_once_resolves_next_gregorian_date_when_once_date_is_none() -> None:
    # Relative to 2026-11-12 UTC, 2026's lunar 8-15 (2026-09-25) already passed -> resolves 2027-09-15!
    ref = datetime(2026, 11, 12, 4, 0, 0, tzinfo=UTC)
    next_mid_autumn = resolve_next_lunar_occurrence_date(
        target_month=8,
        target_day=15,
        is_leap_month=False,
        timezone="Asia/Shanghai",
        reference_utc=ref,
        fire_hour=9,
        fire_minute=0,
        lead_days=[1, 0],
    )
    assert next_mid_autumn == date(2027, 9, 15)

    slot = compute_next_fire_slot(
        rule=ReminderRule.ONCE,
        once_date=None,
        is_lunar=True,
        target_month=8,
        target_day=15,
        is_leap_month=False,
        lead_days=[1, 0],
        fire_hour=9,
        fire_minute=0,
        timezone="Asia/Shanghai",
        reference_utc=ref,
    )
    assert slot is not None
    assert slot.occurrence_date == date(2027, 9, 15)
    assert slot.lead_day == 1
    assert slot.scheduled_for_utc == datetime(2027, 9, 14, 1, 0, 0, tzinfo=UTC)

    # Non-lunar ONCE without once_date still raises ReminderScheduleError
    with pytest.raises(ReminderScheduleError):
        compute_next_fire_slot(
            rule=ReminderRule.ONCE,
            once_date=None,
            is_lunar=False,
            reference_utc=ref,
        )


@pytest.mark.asyncio
async def test_privacy_desensitized_push_copy_and_anti_parasocial_guardrails() -> None:
    rid = new_id()
    copy_today = build_desensitized_push_copy(
        lead_day=0,
        rule=ReminderRule.YEARLY,
        reminder_id=rid,
    )
    assert copy_today.title == "心络 · 今日有一份心意备忘"
    assert copy_today.body == "点开查看你记下的重要日子与准备清单"
    assert copy_today["deeplinkPath"] == f"/reminders/{rid}"
    validate_push_copy_safety(
        copy_today.title,
        copy_today.body,
        forbidden_substrings=["沈清秋", "芒果过敏", "银杏书签"],
    )

    copy_lead = build_desensitized_push_copy(lead_day=7, rule=ReminderRule.ONCE, reminder_id=rid)
    assert copy_lead.title == "心络 · 7 天后有一个重要约定"

    for banned in FORBIDDEN_ANTHROPOMORPHIC_PUSH_PHRASES:
        with pytest.raises(PushCopySafetyError):
            validate_push_copy_safety(f"心络：{banned}", "点开看看吧")

    with pytest.raises(PushCopySafetyError):
        validate_push_copy_safety("心络提醒", "沈清秋后天过生日", forbidden_substrings=["沈清秋"])

    with pytest.raises(PushCopySafetyError):
        validate_push_copy_safety("系统自动归档", "点开查看 [rec_01]")

    # Push adapters also enforce copy safety and work deterministically
    apns = APNsLogAdapter()
    web = WebPushLogAdapter()
    recorder = InMemoryPushRecorder()
    sample_msg = PushMessage(
        device_id=new_id(),
        user_id=new_id(),
        reminder_id=rid,
        platform=PushPlatform.IOS_APNS,
        push_token="apns-dev-token-xyz",
        title=copy_today.title,
        body=copy_today.body,
        deeplink_path=copy_today.deeplink_path,
        scheduled_for=datetime(2026, 11, 12, 1, 0, 0, tzinfo=UTC),
        lead_day=0,
    )
    r1 = await apns.send(sample_msg)
    r2 = await web.send(
        PushMessage(
            device_id=sample_msg.device_id,
            user_id=sample_msg.user_id,
            reminder_id=rid,
            platform=PushPlatform.WEB_PUSH,
            push_token="web-vapid-endpoint",
            title=copy_lead.title,
            body=copy_lead.body,
            deeplink_path=copy_lead.deeplink_path,
            scheduled_for=sample_msg.scheduled_for,
            lead_day=7,
        )
    )
    r3 = await recorder.send(sample_msg)
    assert r1.delivered and r1.channel == "ios_apns"
    assert r2.delivered and r2.channel == "web_push"
    assert r3.delivered and len(recorder.deliveries) == 1


def test_job_payload_zero_pii_validation_backoff_and_cron_keys() -> None:
    cid = new_id()
    safe = sanitize_and_validate_job_payload({"capture_id": cid, "retry_hint": 1})
    assert UUID(safe["capture_id"]) == cid

    for bad_payload in (
        {"raw_text": "林屿说芒果过敏"},
        {"rawText": "林屿说芒果过敏"},
        {"displayName": "沈清秋"},
        {"pushToken": "apns-secret-token"},
        {"nested": {"content": "私密正文"}},
        {"ref": "user@example.com"},
        {"auth": "Bearer eyJhbGciOiJIUzI1NiJ9.secret"},
        {"blob": b"raw-bytes"},
    ):
        with pytest.raises(JobPayloadPrivacyError):
            sanitize_and_validate_job_payload(bad_payload)

    assert compute_backoff_seconds(1) == 10
    assert compute_backoff_seconds(2) == 20
    assert compute_backoff_seconds(3) == 40
    assert compute_backoff_seconds(12) == 3600

    err = RuntimeError("gateway failed for user@example.com phone 13812345678 raw_text=秘密内容")
    sanitized = sanitize_job_error(err)
    assert "user@example.com" not in sanitized
    assert "13812345678" not in sanitized
    assert "秘密内容" not in sanitized
    assert sanitized.startswith("RuntimeError:")

    t1 = datetime(2026, 11, 12, 4, 0, 5, tzinfo=UTC)
    t2 = datetime(2026, 11, 12, 4, 0, 59, tzinfo=UTC)
    t3 = datetime(2026, 11, 12, 4, 1, 0, tzinfo=UTC)
    assert build_cron_idempotency_key("reminders.scan_due", t1, 60) == build_cron_idempotency_key(
        "reminders.scan_due", t2, 60
    )
    assert build_cron_idempotency_key("reminders.scan_due", t1, 60) != build_cron_idempotency_key(
        "reminders.scan_due", t3, 60
    )
