"""P0 0.7 honest push: log-only adapters never count as delivered."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.integration.test_reminders_worker import _provision_user_and_person

from mindweave.core.clock import ManualClock
from mindweave.core.config import Settings
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.orm import ReminderDeliveryRow, ReminderRow
from mindweave.modules.reminders.api.router import router as reminders_router
from mindweave.modules.reminders.application.service import (
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_LOGGED,
    DELIVERY_STATUS_NO_DEVICE,
    DELIVERY_STATUS_NOT_SENT,
    scan_and_deliver_due_reminders,
    summarize_push_receipts,
)
from mindweave.modules.reminders.infra.push import (
    CompositePushDispatcher,
    DisabledPushAdapter,
    PushDeliveryReceipt,
    WebPushLogAdapter,
)
from mindweave.shared.enums import ReminderStatus

# ---------------------------------------------------------------------------
# 0.7 Honest push
# ---------------------------------------------------------------------------


def test_summarize_push_receipts_never_counts_log_only_as_delivered() -> None:
    logged = PushDeliveryReceipt(delivered=False, channel="log", failure_reason="log_only_adapter")
    disabled = PushDeliveryReceipt(
        delivered=False, channel="disabled", failure_reason="push_disabled"
    )
    real = PushDeliveryReceipt(delivered=True, channel="web_push", provider_message_id="m1")
    boom = PushDeliveryReceipt(delivered=False, channel="web_push", failure_reason="gone")

    assert summarize_push_receipts([logged, logged]) == (DELIVERY_STATUS_LOGGED, 0)
    assert summarize_push_receipts([logged, disabled]) == (DELIVERY_STATUS_NOT_SENT, 0)
    assert summarize_push_receipts([logged, real]) == (DELIVERY_STATUS_DELIVERED, 1)
    assert summarize_push_receipts([boom]) == (DELIVERY_STATUS_FAILED, 0)
    assert summarize_push_receipts([]) == (DELIVERY_STATUS_NO_DEVICE, 0)


@pytest.mark.parametrize("environment", ["staging", "prod_cn"])
def test_log_only_push_adapters_are_a_startup_error_outside_dev(environment: str) -> None:
    prod_safe = {
        "ai_primary_provider": "deepseek",
        "deepseek_api_key": "sk-test-valid",
        "email_provider": "resend",
        "resend_api_key": "re_test_valid",
        "email_from_address": "noreply@mail.mindweave.app",
        "kek_hex": "ab" * 32,
        "jwt_secret": "x" * 48,
        "allow_dev_mock_apple_auth": False,
        "expose_dev_otp_in_response": False,
    }
    with pytest.raises(ValueError, match="Log-only push adapters"):
        Settings(environment=environment, **prod_safe)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Log-only push adapters"):
        Settings(
            environment=environment,  # type: ignore[arg-type]
            push_apns_adapter="disabled",
            push_web_push_adapter="log",
            **prod_safe,
        )
    ok = Settings(
        environment=environment,  # type: ignore[arg-type]
        push_apns_adapter="disabled",
        push_web_push_adapter="disabled",
        **prod_safe,
    )
    dispatcher = CompositePushDispatcher.from_adapter_kinds(
        apns=ok.push_apns_adapter, web_push=ok.push_web_push_adapter
    )
    assert isinstance(dispatcher._web_push, DisabledPushAdapter)
    # dev keeps the honest log adapters by default
    dev = Settings(environment="dev")
    assert dev.push_web_push_adapter == "log"
    assert isinstance(
        CompositePushDispatcher.from_adapter_kinds(apns="log", web_push="log")._web_push,
        WebPushLogAdapter,
    )


@pytest.mark.asyncio
async def test_log_only_dispatch_is_recorded_as_logged_and_never_retried(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC))
    db_container.extras["push_provider"] = CompositePushDispatcher.from_adapter_kinds(
        apns="log", web_push="log"
    )
    _user_id, person_id, token = await _provision_user_and_person(db_container)

    app = FastAPI()
    app.state.container = db_container
    register_exception_handlers(app)
    app.include_router(reminders_router, prefix="/v1")
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dev_resp = await client.post(
            "/v1/devices",
            headers={**headers, "Idempotency-Key": "honest-dev-1"},
            json={"platform": "web_push", "pushToken": "https://push.example/sub/abc"},
        )
        assert dev_resp.status_code == 201
        rem_resp = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": "honest-rem-1"},
            json={
                "personId": str(person_id),
                "title": "寄银杏书签",
                "rule": "once",
                "onceDate": "2026-11-11",
                "leadDays": [0],
                "fireHour": 9,
                "fireMinute": 0,
                "status": "active",
            },
        )
        assert rem_resp.status_code == 201
        reminder_id = UUID(rem_resp.json()["id"])

    manual_clock.set(datetime(2026, 11, 11, 1, 0, 5, tzinfo=UTC))
    first = await scan_and_deliver_due_reminders(db_container)
    assert first.scanned_count == 1
    assert first.delivered_slots == 0
    assert first.pushed_messages == 0

    # The slot is consumed exactly once: a second scan finds nothing due (no retry storm).
    second = await scan_and_deliver_due_reminders(db_container)
    assert second.scanned_count == 0

    async with db_container.db.system_session() as sys_sess:
        deliveries = (
            await sys_sess.scalars(
                select(ReminderDeliveryRow).where(ReminderDeliveryRow.reminder_id == reminder_id)
            )
        ).all()
        assert [d.status for d in deliveries] == [DELIVERY_STATUS_LOGGED]
        rem_row = await sys_sess.get(ReminderRow, reminder_id)
        assert rem_row is not None
        assert rem_row.status == ReminderStatus.COMPLETED.value
        assert rem_row.next_fire_at is None
