"""Live PostgreSQL integration tests for background worker queue/runner, reminders API, RLS, and push delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    BackgroundJobRow,
    DeviceRow,
    PersonRow,
    ReminderDeliveryRow,
    ReminderRow,
    TelemetryEventRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.reminders.api.router import router as reminders_router
from mindweave.modules.reminders.application.service import (
    ReminderService,
    scan_and_deliver_due_reminders,
)
from mindweave.modules.reminders.infra.push import InMemoryPushRecorder
from mindweave.shared.enums import (
    UNASSIGNED_PERSON_SLUG,
    JobStatus,
    PersonState,
    ReminderRule,
    ReminderStatus,
)
from mindweave.shared.scopes import UserScope
from mindweave.workers.__main__ import build_default_worker_runner
from mindweave.workers.ports import TASK_PROCESS_CAPTURE, TASK_SCAN_DUE_REMINDERS
from mindweave.workers.queue import JobPayloadPrivacyError, PostgresJobQueue
from mindweave.workers.runner import ClaimedJob, WorkerRunner
from mindweave.workers.scheduler import PeriodicScheduler


async def _provision_user_and_person(
    container: AppContainer,
    *,
    person_name: str = "沈清秋",
    timezone: str = "Asia/Shanghai",
) -> tuple[UUID, UUID, str]:
    user_id = new_id()
    person_id = new_id()
    now = container.clock.now()
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    dek = container.cipher.load_dek(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id)

    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=user_id, status="active", created_at=now, updated_at=now))

    scope = UserScope(user_id)
    async with container.db.user_session(scope) as sess:
        sess.add(
            UserKeyRow(
                user_id=user_id,
                wrapped_dek=wrapped_dek,
                kek_id=kek_id,
                created_at=now,
            )
        )
        sess.add(
            UserPreferenceRow(
                user_id=user_id,
                timezone=timezone,
                updated_at=now,
            )
        )
        sess.add(
            PersonRow(
                id=person_id,
                user_id=user_id,
                tier="gold",
                rank="i",
                state="active",
                display_name_enc=container.cipher.encrypt_str(
                    dek,
                    person_name,
                    user_id=user_id,
                    table="persons",
                    column="display_name_enc",
                    row_id=person_id,
                ),
                monogram=person_name[0],
                visual_tokens={},
                version=1,
                created_at=now,
                updated_at=now,
            )
        )

    access_token, _ = container.tokens.create_access_token(user_id=user_id, now=now)
    return user_id, person_id, access_token


def _build_test_app(container: AppContainer) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(reminders_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_postgres_job_queue_skip_locked_backoff_dead_letter_and_lease_recovery(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    user_id, _, _ = await _provision_user_and_person(db_container)
    scope = UserScope(user_id)
    queue = PostgresJobQueue(clock=manual_clock)

    # Rejects sensitive keys inside real session before touching DB
    async with db_container.db.user_session(scope) as sess:
        with pytest.raises(JobPayloadPrivacyError):
            await queue.enqueue(
                sess,
                task_name=TASK_PROCESS_CAPTURE,
                payload={"raw_text": "绝对不能落盘到队列的私密原文"},
                user_id=user_id,
            )

    # Enqueue inside user_session with idempotency_key deduplication
    capture_a = new_id()
    async with db_container.db.user_session(scope) as sess:
        j1 = await queue.enqueue(
            sess,
            task_name=TASK_PROCESS_CAPTURE,
            payload={"capture_id": capture_a},
            user_id=user_id,
            idempotency_key=f"cap:{capture_a}",
        )
        j1_dup = await queue.enqueue(
            sess,
            task_name=TASK_PROCESS_CAPTURE,
            payload={"capture_id": capture_a},
            user_id=user_id,
            idempotency_key=f"cap:{capture_a}",
        )
        assert j1 == j1_dup

    # Enqueue additional jobs inside system_session for concurrent worker test
    extra_ids: list[UUID] = []
    async with db_container.db.system_session() as sys_sess:
        for idx in range(5):
            jid = await queue.enqueue(
                sys_sess,
                task_name="test.concurrent",
                payload={"slot": idx},
                idempotency_key=f"conc:{idx}",
            )
            extra_ids.append(jid)

    executed_concurrent: list[int] = []
    executed_captures: list[str] = []

    async def _slow_concurrent_handler(job: ClaimedJob) -> None:
        await asyncio.sleep(0.02)
        executed_concurrent.append(int(job.payload["slot"]))

    async def _capture_handler(job: ClaimedJob) -> None:
        executed_captures.append(str(job.payload["capture_id"]))

    runner_a = WorkerRunner(db_container.db, clock=manual_clock, batch_size=2, lease_seconds=60)
    runner_b = WorkerRunner(db_container.db, clock=manual_clock, batch_size=2, lease_seconds=60)
    for r in (runner_a, runner_b):
        r.register_handler("test.concurrent", _slow_concurrent_handler)
        r.register_handler(TASK_PROCESS_CAPTURE, _capture_handler)

    await asyncio.gather(runner_a.drain(), runner_b.drain())
    assert sorted(executed_concurrent) == [0, 1, 2, 3, 4]
    assert executed_captures == [str(capture_a)]

    # Test exponential backoff -> dead-letter queue with zero-PII error sanitization
    async with db_container.db.system_session() as sys_sess:
        failing_job_id = await queue.enqueue(
            sys_sess,
            task_name="test.always_fails",
            payload={"entity_id": new_id()},
            max_attempts=3,
        )

    async def _failing_handler(_job: ClaimedJob) -> None:
        raise RuntimeError("upstream failed for leak@example.com raw_text=私密正文")

    runner_a.register_handler("test.always_fails", _failing_handler)

    # Attempt 1 fails -> backoff = min(3600, 2**1 * 5) = 10s
    assert await runner_a.run_once() == 1
    assert await runner_a.run_once() == 0  # Not due yet!

    manual_clock.advance(seconds=10)
    # Attempt 2 fails -> backoff = min(3600, 2**2 * 5) = 20s
    assert await runner_a.run_once() == 1
    manual_clock.advance(seconds=19)
    assert await runner_a.run_once() == 0

    manual_clock.advance(seconds=1)
    # Attempt 3 fails -> attempts == max_attempts (3) -> status = 'dead'
    assert await runner_a.run_once() == 1

    async with db_container.db.system_session() as sys_sess:
        dead_row = await sys_sess.get(BackgroundJobRow, failing_job_id)
        assert dead_row is not None
        assert dead_row.status == JobStatus.DEAD.value
        assert dead_row.attempts == 3
        assert dead_row.last_error is not None
        assert "leak@example.com" not in dead_row.last_error
        assert "私密正文" not in dead_row.last_error

    # Test expired lease recovery: simulate crashed worker holding expired 'running' lease
    reclaimed: list[UUID] = []
    async with db_container.db.system_session() as sys_sess:
        stalled_id = await queue.enqueue(
            sys_sess,
            task_name="test.reclaim",
            payload={"item_id": new_id()},
            max_attempts=3,
        )
    # Claim without executing to leave row in status='running' with active lease
    claimed_stalled = await runner_a.claim_due_batch()
    assert [c.id for c in claimed_stalled] == [stalled_id]
    # Another worker cannot steal while lease is valid
    assert await runner_b.claim_due_batch() == []

    async def _reclaim_handler(job: ClaimedJob) -> None:
        reclaimed.append(job.id)

    runner_b.register_handler("test.reclaim", _reclaim_handler)
    manual_clock.advance(seconds=61)
    assert await runner_b.run_once() == 1
    assert reclaimed == [stalled_id]

    async with db_container.db.system_session() as sys_sess:
        reclaimed_row = await sys_sess.get(BackgroundJobRow, stalled_id)
        assert reclaimed_row is not None
        assert reclaimed_row.status == JobStatus.SUCCEEDED.value
        assert reclaimed_row.attempts == 2


@pytest.mark.asyncio
async def test_periodic_scheduler_deduplicates_cron_buckets_and_graceful_worker_stop(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    scheduler = PeriodicScheduler(db_container.db, clock=manual_clock)
    ids_first, ids_second = await asyncio.gather(scheduler.tick(), scheduler.tick())
    assert ids_first == ids_second
    assert len(ids_first) == 3

    # Advancing 30s within same minute keeps 60s/15m/24h buckets identical
    manual_clock.advance(seconds=30)
    ids_same_bucket = await scheduler.tick()
    assert ids_same_bucket == ids_first

    # Advancing another 31s crosses the 60s minute boundary ONLY for TASK_SCAN_DUE_REMINDERS
    manual_clock.advance(seconds=31)
    ids_next_minute = await scheduler.tick()
    assert ids_next_minute[0] != ids_first[0]
    assert ids_next_minute[1:] == ids_first[1:]

    # Verify graceful run_forever shutdown via stop_event
    runner = build_default_worker_runner(db_container, batch_size=5, lease_seconds=30)
    stop_event = asyncio.Event()

    async def _trigger_stop() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.gather(
        runner.run_forever(
            poll_interval=0.05,
            stop_event=stop_event,
            install_signal_handlers=False,
        ),
        _trigger_stop(),
    )


@pytest.mark.asyncio
async def test_reminders_devices_api_lunar_delivery_zero_duplicates_rls_and_telemetry(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    # Anchor clock to 2026-11-11 00:30 UTC (08:30 Asia/Shanghai)
    manual_clock.set(datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC))
    recorder = InMemoryPushRecorder()
    db_container.extras["push_provider"] = recorder

    user_a, person_a, token_a = await _provision_user_and_person(
        db_container, person_name="沈清秋", timezone="Asia/Shanghai"
    )
    _, person_b, token_b = await _provision_user_and_person(
        db_container, person_name="陆景行", timezone="Asia/Shanghai"
    )

    app = _build_test_app(db_container)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Missing Idempotency-Key on mutating POST -> 400
        bad_resp = await client.post(
            "/v1/devices",
            headers=headers_a,
            json={"platform": "ios_apns", "pushToken": "apns-tok-1"},
        )
        assert bad_resp.status_code == 400

        # 2. Register APNs + Web Push devices with Idempotency-Key replay & upsert
        dev_payload = {
            "platform": "ios_apns",
            "pushToken": "apns-secret-token-user-a",
            "deviceLabel": "文杰的 iPhone",
        }
        dev_resp_1 = await client.post(
            "/v1/devices",
            headers={**headers_a, "Idempotency-Key": "dev-reg-1"},
            json=dev_payload,
        )
        assert dev_resp_1.status_code == 201
        dev_1 = dev_resp_1.json()
        assert dev_1["deviceLabel"] == "文杰的 iPhone"
        assert "pushToken" not in dev_1  # Never leak raw push token in API response!

        # Exact replay returns identical payload & status
        dev_replay = await client.post(
            "/v1/devices",
            headers={**headers_a, "Idempotency-Key": "dev-reg-1"},
            json=dev_payload,
        )
        assert dev_replay.status_code == 201
        assert dev_replay.json() == dev_1

        # Reusing same Idempotency-Key with different payload -> 409 idempotency_conflict
        dev_conflict = await client.post(
            "/v1/devices",
            headers={**headers_a, "Idempotency-Key": "dev-reg-1"},
            json={**dev_payload, "deviceLabel": "另一台设备"},
        )
        assert dev_conflict.status_code == 409

        # Upsert same token with updated label under new Idempotency-Key -> updates existing row!
        dev_upsert = await client.post(
            "/v1/devices",
            headers={**headers_a, "Idempotency-Key": "dev-reg-1b"},
            json={**dev_payload, "deviceLabel": "文杰的 iPhone 16 Pro"},
        )
        assert dev_upsert.status_code == 200
        assert dev_upsert.json()["id"] == dev_1["id"]
        assert dev_upsert.json()["deviceLabel"] == "文杰的 iPhone 16 Pro"

        # Register second device (Web Push) and verify raw ciphertext at rest
        dev_web_resp = await client.post(
            "/v1/devices",
            headers={**headers_a, "Idempotency-Key": "dev-reg-2"},
            json={
                "platform": "web_push",
                "pushToken": "https://push.example/sub/secret-xyz",
                "deviceLabel": "Mac Safari",
            },
        )
        assert dev_web_resp.status_code == 201
        dev_web_id = dev_web_resp.json()["id"]

        async with db_container.db.system_session() as sys_sess:
            raw_dev = await sys_sess.get(DeviceRow, UUID(dev_1["id"]))
            assert raw_dev is not None
            assert b"apns-secret-token-user-a" not in raw_dev.push_token_enc

        list_devs = await client.get("/v1/devices", headers=headers_a)
        assert list_devs.status_code == 200
        assert len(list_devs.json()["items"]) == 2

        # User B sees zero of User A's devices and cannot delete User A's device (RLS + scope!)
        list_devs_b = await client.get("/v1/devices", headers=headers_b)
        assert list_devs_b.json()["items"] == []
        del_cross = await client.delete(
            f"/v1/devices/{dev_web_id}",
            headers={**headers_b, "Idempotency-Key": "del-cross-1"},
        )
        assert del_cross.status_code == 404

        # Missing Idempotency-Key on DELETE /devices/{id} -> 400 (P1-7)
        assert (
            await client.delete(f"/v1/devices/{dev_web_id}", headers=headers_a)
        ).status_code == 400

        # Delete second device with Idempotency-Key + verify 204 replay!
        del_ok = await client.delete(
            f"/v1/devices/{dev_web_id}",
            headers={**headers_a, "Idempotency-Key": "del-dev-web-1"},
        )
        assert del_ok.status_code == 204
        del_replay = await client.delete(
            f"/v1/devices/{dev_web_id}",
            headers={**headers_a, "Idempotency-Key": "del-dev-web-1"},
        )
        assert del_replay.status_code == 204

        # 3. Create a PROPOSED Chinese Lunar yearly reminder (30-day lunar birthday edge case!)
        lunar_create = await client.post(
            "/v1/reminders",
            headers={**headers_a, "Idempotency-Key": "rem-lunar-1"},
            json={
                "personId": str(person_a),
                "title": "沈清秋腊月三十生日 · 准备芒果无敏蛋糕",
                "rule": "yearly",
                "isLunar": True,
                "targetMonth": 12,
                "targetDay": 30,
                "leadDays": [7, 0],
                "fireHour": 9,
                "fireMinute": 0,
                "status": "proposed",
            },
        )
        assert lunar_create.status_code == 201
        lunar_rem = lunar_create.json()
        lunar_id = lunar_rem["id"]
        assert lunar_rem["status"] == "proposed"
        assert lunar_rem["nextFireAt"] is None
        assert lunar_rem["version"] == 1

        # Filter GET /v1/reminders by status and personId
        prop_list = await client.get(
            "/v1/reminders",
            params={"status": "proposed", "personId": str(person_a)},
            headers=headers_a,
        )
        assert [r["id"] for r in prop_list.json()["items"]] == [lunar_id]

        # 4. Activate proposed -> active via PATCH with If-Match + Idempotency-Key
        patch_no_idem = await client.patch(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "If-Match": '"1"'},
            json={"status": "active"},
        )
        assert patch_no_idem.status_code == 400

        patch_no_if_match = await client.patch(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "Idempotency-Key": "patch-no-if-match"},
            json={"status": "active"},
        )
        assert patch_no_if_match.status_code == 400

        patch_ok = await client.patch(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "If-Match": '"1"', "Idempotency-Key": "patch-act-1"},
            json={"status": "active"},
        )
        assert patch_ok.status_code == 200
        activated_lunar = patch_ok.json()
        assert activated_lunar["status"] == "active"
        assert activated_lunar["version"] == 2
        assert activated_lunar["nextFireAt"] is not None
        assert activated_lunar["nextLeadDay"] == 7

        # Replay PATCH with same Idempotency-Key returns identical 200 + ETag
        patch_replay = await client.patch(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "If-Match": '"1"', "Idempotency-Key": "patch-act-1"},
            json={"status": "active"},
        )
        assert patch_replay.status_code == 200
        assert patch_replay.json() == activated_lunar
        assert patch_replay.headers.get("ETag") == '"2"'

        # Stale version If-Match -> 412 Precondition Failed!
        patch_stale = await client.patch(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "If-Match": '"1"', "Idempotency-Key": "patch-stale-1"},
            json={"fireHour": 10},
        )
        assert patch_stale.status_code == 412

        # Cross-tenant User B cannot read, patch, cancel, or mark done User A's reminder
        assert (await client.get("/v1/reminders", headers=headers_b)).json()["items"] == []
        assert (
            await client.patch(
                f"/v1/reminders/{lunar_id}",
                headers={**headers_b, "If-Match": '"2"', "Idempotency-Key": "forged-patch"},
                json={"fireHour": 10},
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/v1/reminders/{lunar_id}:done",
                headers={**headers_b, "Idempotency-Key": "forged-done"},
            )
        ).status_code == 404

        # 5. Create an ACTIVE ONCE reminder firing today at 09:00 Asia/Shanghai (01:00 UTC)
        once_create = await client.post(
            "/v1/reminders",
            headers={**headers_a, "Idempotency-Key": "rem-once-1"},
            json={
                "personId": str(person_a),
                "title": "给沈清秋寄银杏书签",
                "rule": "once",
                "onceDate": "2026-11-11",
                "leadDays": [1, 0],
                "fireHour": 9,
                "fireMinute": 0,
                "status": "active",
            },
        )
        assert once_create.status_code == 201
        once_rem = once_create.json()
        once_id = once_rem["id"]
        # Lead day 1 (Nov 10) already passed at 2026-11-11 00:30 UTC -> picks lead_day=0 (01:00 UTC)
        assert once_rem["nextLeadDay"] == 0
        assert datetime.fromisoformat(once_rem["nextFireAt"]) == datetime(
            2026, 11, 11, 1, 0, 0, tzinfo=UTC
        )

        # Advance clock to 01:00 UTC (>15m JWT TTL, so mint fresh token for subsequent API calls)
        # and run both WorkerRunner(TASK_SCAN_DUE_REMINDERS) + concurrent scanners -> ZERO duplicate pushes!
        manual_clock.set(datetime(2026, 11, 11, 1, 0, 5, tzinfo=UTC))
        fresh_token_a, _ = db_container.tokens.create_access_token(
            user_id=user_a, now=manual_clock.now()
        )
        headers_a = {"Authorization": f"Bearer {fresh_token_a}"}

        queue = PostgresJobQueue(clock=manual_clock)
        async with db_container.db.system_session() as sys_sess:
            await queue.enqueue(
                sys_sess,
                task_name=TASK_SCAN_DUE_REMINDERS,
                payload={"batch_limit": 50},
                idempotency_key="manual-scan-due-1",
            )
        worker = build_default_worker_runner(db_container, enable_scheduler=False)

        _, res_1, res_2 = await asyncio.gather(
            worker.drain(),
            scan_and_deliver_due_reminders(db_container),
            scan_and_deliver_due_reminders(db_container),
        )
        assert res_1.delivered_slots + res_2.delivered_slots <= 1
        assert len(recorder.deliveries) == 1

        pushed = recorder.deliveries[0]
        assert pushed.reminder_id == UUID(once_id)
        assert pushed.lead_day == 0
        assert "沈清秋" not in pushed.title and "沈清秋" not in pushed.body
        assert "银杏书签" not in pushed.title and "银杏书签" not in pushed.body
        assert pushed.title == "心络 · 今日有一份心意备忘"

        # ONCE reminder with no remaining future slots transitioned to status='completed'
        completed_list = await client.get(
            "/v1/reminders", params={"status": "completed"}, headers=headers_a
        )
        assert [r["id"] for r in completed_list.json()["items"]] == [once_id]

        async with db_container.db.user_session(UserScope(user_a)) as sess:
            delivery_count = await sess.scalar(
                select(func.count()).select_from(ReminderDeliveryRow)
            )
            assert delivery_count == 1

        # 6. Mark lunar reminder `:done` -> records North-Star telemetry (idempotent on replay!)
        done_resp = await client.post(
            f"/v1/reminders/{lunar_id}:done",
            headers={**headers_a, "Idempotency-Key": "done-lunar-1"},
        )
        assert done_resp.status_code == 200
        assert done_resp.json()["actionDoneAt"] is not None
        # Yearly reminder remains active when marked done for this cycle
        assert done_resp.json()["status"] == "active"

        done_replay = await client.post(
            f"/v1/reminders/{lunar_id}:done",
            headers={**headers_a, "Idempotency-Key": "done-lunar-1"},
        )
        assert done_replay.status_code == 200
        assert done_replay.json() == done_resp.json()

        async with db_container.db.user_session(UserScope(user_a)) as sess:
            telemetry_rows = (
                await sess.scalars(
                    select(TelemetryEventRow).where(
                        TelemetryEventRow.event_name == "reminder_action_done"
                    )
                )
            ).all()
            assert len(telemetry_rows) == 1
            assert telemetry_rows[0].metadata_json == {"rule": "yearly"}

        # 7. Cancel reminder via DELETE /v1/reminders/{id} (requires Idempotency-Key + replays!)
        assert (
            await client.delete(f"/v1/reminders/{lunar_id}", headers=headers_a)
        ).status_code == 400
        del_rem = await client.delete(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "Idempotency-Key": "del-rem-lunar-1"},
        )
        assert del_rem.status_code == 200
        assert del_rem.json()["status"] == "cancelled"
        assert del_rem.json()["nextFireAt"] is None

        del_rem_replay = await client.delete(
            f"/v1/reminders/{lunar_id}",
            headers={**headers_a, "Idempotency-Key": "del-rem-lunar-1"},
        )
        assert del_rem_replay.status_code == 200
        assert del_rem_replay.json() == del_rem.json()

        # Verify encrypted title at rest in PostgreSQL
        async with db_container.db.system_session() as sys_sess:
            raw_rem = await sys_sess.get(ReminderRow, UUID(lunar_id))
            assert raw_rem is not None
            assert "沈清秋".encode() not in raw_rem.title_enc
            assert person_b != person_a


@pytest.mark.asyncio
async def test_archived_deleted_unassigned_filtering_and_lunar_once_activation(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 11, 0, 30, 0, tzinfo=UTC))
    recorder = InMemoryPushRecorder()
    db_container.extras["push_provider"] = recorder

    user_id, active_person_id, token = await _provision_user_and_person(
        db_container, person_name="阿哲", timezone="Asia/Shanghai"
    )
    scope = UserScope(user_id)
    app = _build_test_app(db_container)
    headers = {"Authorization": f"Bearer {token}"}

    # Register a push device
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dev_resp = await client.post(
            "/v1/devices",
            headers={**headers, "Idempotency-Key": "dev-rt-test"},
            json={"platform": "ios_apns", "pushToken": "apns-rt-token"},
        )
        assert dev_resp.status_code == 201

    now = manual_clock.now()
    archived_person_id = new_id()
    deleted_person_id = new_id()
    unassigned_person_id = new_id()
    due_archived_rem_id = new_id()
    due_deleted_rem_id = new_id()
    due_unassigned_rem_id = new_id()
    lunar_once_rem_id = new_id()

    async with db_container.db.user_session(scope) as sess:
        dek = await db_container.load_user_dek(sess, scope)
        for pid, name, state, slug, archived_at, deleted_at in (
            (archived_person_id, "前任", PersonState.ARCHIVED.value, None, now, None),
            (deleted_person_id, "已删除", PersonState.ACTIVE.value, None, None, now),
            (
                unassigned_person_id,
                "未归属",
                PersonState.PROVISIONAL.value,
                UNASSIGNED_PERSON_SLUG,
                None,
                None,
            ),
        ):
            sess.add(
                PersonRow(
                    id=pid,
                    user_id=user_id,
                    slug=slug,
                    tier="gold",
                    rank="m",
                    state=state,
                    display_name_enc=db_container.cipher.encrypt_str(
                        dek,
                        name,
                        user_id=user_id,
                        table="persons",
                        column="display_name_enc",
                        row_id=pid,
                    ),
                    monogram=name[0],
                    visual_tokens={},
                    version=1,
                    archived_at=archived_at,
                    deleted_at=deleted_at,
                    created_at=now,
                    updated_at=now,
                )
            )
        await sess.flush()

        for rid, pid in (
            (due_archived_rem_id, archived_person_id),
            (due_deleted_rem_id, deleted_person_id),
            (due_unassigned_rem_id, unassigned_person_id),
        ):
            sess.add(
                ReminderRow(
                    id=rid,
                    user_id=user_id,
                    person_id=pid,
                    fact_id=None,
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "旧纪念日",
                        user_id=user_id,
                        table="reminders",
                        column="title_enc",
                        row_id=rid,
                    ),
                    rule=ReminderRule.ONCE.value,
                    is_lunar=False,
                    once_date=date(2026, 11, 11),
                    fire_hour=8,
                    fire_minute=0,
                    lead_days=[0],
                    timezone="Asia/Shanghai",
                    next_fire_at=now,
                    next_lead_day=0,
                    status=ReminderStatus.ACTIVE.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )

        # Proposed lunar ONCE reminder with once_date=None (RT-07)
        sess.add(
            ReminderRow(
                id=lunar_once_rem_id,
                user_id=user_id,
                person_id=active_person_id,
                fact_id=None,
                title_enc=db_container.cipher.encrypt_str(
                    dek,
                    "答应中秋陪阿哲赏月",
                    user_id=user_id,
                    table="reminders",
                    column="title_enc",
                    row_id=lunar_once_rem_id,
                ),
                rule=ReminderRule.ONCE.value,
                is_lunar=True,
                target_month=8,
                target_day=15,
                is_leap_month=False,
                once_date=None,
                fire_hour=9,
                fire_minute=0,
                lead_days=[1, 0],
                timezone="Asia/Shanghai",
                next_fire_at=None,
                next_lead_day=None,
                status=ReminderStatus.PROPOSED.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )

    # 1. OPT-2 + RT-03: system_session SQL scan skips archived, soft-deleted, and __unassigned__
    scan_res = await scan_and_deliver_due_reminders(db_container, push_provider=recorder)
    assert scan_res.scanned_count == 0
    assert scan_res.pushed_messages == 0
    assert recorder.deliveries == []

    # 2. Race-window check inside _deliver_single_due_reminder pauses archived & cancels deleted/unassigned
    svc = ReminderService(db_container, push_provider=recorder)
    assert await svc._deliver_single_due_reminder(
        scope=scope, reminder_id=due_archived_rem_id, now=now
    ) == (False, 0, False)
    assert await svc._deliver_single_due_reminder(
        scope=scope, reminder_id=due_deleted_rem_id, now=now
    ) == (False, 0, False)
    assert await svc._deliver_single_due_reminder(
        scope=scope, reminder_id=due_unassigned_rem_id, now=now
    ) == (False, 0, False)
    assert recorder.deliveries == []

    async with db_container.db.user_session(scope) as sess:
        arch_row = await sess.get(ReminderRow, due_archived_rem_id)
        del_row = await sess.get(ReminderRow, due_deleted_rem_id)
        unassigned_row = await sess.get(ReminderRow, due_unassigned_rem_id)
        assert arch_row is not None and arch_row.status == ReminderStatus.PAUSED.value
        assert arch_row.next_fire_at is None
        assert del_row is not None and del_row.status == ReminderStatus.CANCELLED.value
        assert (
            unassigned_row is not None and unassigned_row.status == ReminderStatus.CANCELLED.value
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 3. list_reminders excludes archived, soft-deleted, and __unassigned__ by default
        default_list = await client.get("/v1/reminders", headers=headers)
        assert [r["id"] for r in default_list.json()["items"]] == [str(lunar_once_rem_id)]

        # includeArchived=true includes archived person's reminder, still hiding deleted & __unassigned__
        incl_arch = await client.get(
            "/v1/reminders", params={"includeArchived": "true"}, headers=headers
        )
        assert {r["id"] for r in incl_arch.json()["items"]} == {
            str(lunar_once_rem_id),
            str(due_archived_rem_id),
        }

        # 4. RT-07: Activating proposed lunar ONCE reminder resolves onceDate & schedules nextFireAt!
        act_resp = await client.patch(
            f"/v1/reminders/{lunar_once_rem_id}",
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "act-lunar-once-1"},
            json={"status": "active"},
        )
        assert act_resp.status_code == 200
        act_body = act_resp.json()
        assert act_body["status"] == "active"
        assert act_body["onceDate"] == "2027-09-15"
        assert act_body["isLunar"] is True
        assert act_body["nextLeadDay"] == 1
        assert act_body["nextFireAt"] is not None

        # 5. R2-03: Reactivating archived person's paused reminder via PATCH -> 422!
        reactivate_arch = await client.patch(
            f"/v1/reminders/{due_archived_rem_id}",
            headers={**headers, "If-Match": '"2"', "Idempotency-Key": "r203-arch-act"},
            json={"status": "active", "onceDate": "2027-05-20"},
        )
        assert reactivate_arch.status_code == 422

        # Non-active updates / cancellation on archived person remain permitted
        pause_edit_arch = await client.patch(
            f"/v1/reminders/{due_archived_rem_id}",
            headers={**headers, "If-Match": '"2"', "Idempotency-Key": "r203-arch-paused"},
            json={"title": "继续暂停的备忘"},
        )
        assert pause_edit_arch.status_code == 200
        assert pause_edit_arch.json()["status"] == "paused"

        # Soft-deleted & __unassigned__ persons return 404 across PATCH, DELETE, and :done!
        for hidden_rem_id in (due_deleted_rem_id, due_unassigned_rem_id):
            p_res = await client.patch(
                f"/v1/reminders/{hidden_rem_id}",
                headers={**headers, "If-Match": '"2"', "Idempotency-Key": f"p-{hidden_rem_id}"},
                json={"status": "active", "onceDate": "2027-06-01"},
            )
            d_res = await client.delete(
                f"/v1/reminders/{hidden_rem_id}",
                headers={**headers, "Idempotency-Key": f"d-{hidden_rem_id}"},
            )
            done_res = await client.post(
                f"/v1/reminders/{hidden_rem_id}:done",
                headers={**headers, "Idempotency-Key": f"done-{hidden_rem_id}"},
            )
            assert p_res.status_code == 404
            assert d_res.status_code == 404
            assert done_res.status_code == 404

    async with db_container.db.user_session(scope) as sess:
        from mindweave.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await svc.get_reminder(sess, scope, due_deleted_rem_id)
        with pytest.raises(NotFoundError):
            await svc.get_reminder(sess, scope, due_unassigned_rem_id)
