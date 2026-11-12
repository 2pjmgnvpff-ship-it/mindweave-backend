"""OpenAPI 3.1 contract snapshot, Schemathesis validation, and full-stack E2E journey tests."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import schemathesis
from httpx import ASGITransport

from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.main import create_app
from mindweave.tools.export_openapi import generate_openapi_dict
from mindweave.workers.__main__ import build_default_worker_runner


def test_openapi_snapshot_is_up_to_date_and_jargon_free() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    snapshot_path = repo_root / "openapi" / "openapi.json"
    assert snapshot_path.exists(), "openapi/openapi.json snapshot must exist"

    expected_dict = generate_openapi_dict()
    expected_text = json.dumps(expected_dict, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    actual_text = snapshot_path.read_text(encoding="utf-8")
    assert actual_text == expected_text, (
        "openapi/openapi.json drifted from create_app().openapi(); run `make openapi`."
    )

    paths = expected_dict["paths"]
    assert isinstance(paths, dict)
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/v1/bootstrap" in paths
    assert "/v1/voice/ingest" in paths
    assert "/v1/captures" in paths
    assert "/v1/persons/{person_id}/chat/stream" in paths
    assert "/v1/me/exports" in paths
    assert "/v1/me" in paths
    # V10 compatibility alias `/api/v1/*` must remain hidden from canonical OpenAPI schema
    assert not any(str(p).startswith("/api/v1") for p in paths)

    # Verify Schemathesis parses OpenAPI 3.1 schema cleanly with zero specification errors
    st_schema = schemathesis.openapi.from_dict(expected_dict)
    assert len(list(st_schema.get_all_operations())) >= 50

    # Verify no operation summary hits forbidden internal developer phrases
    for path_key, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method_name, op in methods.items():
            if not isinstance(op, dict):
                continue
            summary = str(op.get("summary") or "")
            for phrase in FORBIDDEN_UI_DEV_PHRASES:
                assert phrase not in summary, (
                    f"Forbidden jargon {phrase!r} leaked in OpenAPI summary of {method_name} {path_key}"
                )


@pytest.mark.asyncio
async def test_health_readiness_request_id_and_problem_details_envelope(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health_resp = await client.get("/healthz", headers={"X-Request-Id": "req-smoke-001"})
        assert health_resp.status_code == 200
        assert health_resp.headers["X-Request-Id"] == "req-smoke-001"
        assert health_resp.json()["status"] == "ok"

        ready_resp = await client.get("/readyz")
        assert ready_resp.status_code == 200
        assert ready_resp.json() == {"status": "ready", "database": "ok"}
        assert ready_resp.headers.get("X-Request-Id")

        # Malformed header injection in X-Request-Id is sanitized/replaced with a fresh UUID
        injected = await client.get("/healthz", headers={"X-Request-Id": "bad id with spaces!!"})
        assert injected.status_code == 200
        assert injected.headers["X-Request-Id"] != "bad id with spaces!!"

        # Unauthenticated protected route returns RFC 7807 Problem Details + X-Request-Id
        unauth = await client.get("/v1/bootstrap", headers={"X-Request-Id": "req-unauth-01"})
        assert unauth.status_code == 401
        assert unauth.headers["content-type"].startswith("application/problem+json")
        assert unauth.headers["X-Request-Id"] == "req-unauth-01"
        body = unauth.json()
        assert body["code"] == "unauthorized"
        assert body["requestId"] == "req-unauth-01"


@pytest.mark.asyncio
async def test_full_stack_end_to_end_journey_with_background_worker(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    runner = build_default_worker_runner(db_container, enable_scheduler=True)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Request & verify Email OTP -> brand-new account
        otp_req = await client.post(
            "/v1/auth/email/otp:request",
            json={"email": "linxia@example.com", "purpose": "login"},
        )
        assert otp_req.status_code == 200
        otp_data = otp_req.json()
        verify_resp = await client.post(
            "/v1/auth/email/otp:verify",
            json={
                "email": "linxia@example.com",
                "challengeId": otp_data["challengeId"],
                "code": otp_data["devOtpCode"],
            },
        )
        assert verify_resp.status_code == 200
        access_token = verify_resp.json()["accessToken"]
        auth_headers = {"Authorization": f"Bearer {access_token}"}

        # 2. Record PIPL consents
        for kind in ("privacy_policy", "ai_processing"):
            consent_resp = await client.post(
                "/v1/me/consents",
                headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
                json={"consentKind": kind, "granted": True, "policyVersion": "2026.11"},
            )
            assert consent_resp.status_code in (200, 201)

        # 3. Verify V10 `/api/v1/bootstrap` compatibility alias starts with ZERO fictional demo people
        boot_empty = await client.get("/api/v1/bootstrap", headers=auth_headers)
        assert boot_empty.status_code == 200
        boot_json = boot_empty.json()
        assert boot_json["people"] == {}
        assert boot_json["coreIds"] == []
        assert boot_json["casualIds"] == []

        # 4. Create a Gold-tier person ("阿泽") and exercise non-destructive 3-state tier transitions
        create_person = await client.post(
            "/v1/persons",
            headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
            json={
                "nameCn": "阿泽",
                "roleTag": "大学室友",
                "tier": "gold",
                "traits": ["重情义", "嘴硬心软"],
                "aliases": ["泽哥"],
            },
        )
        assert create_person.status_code == 201
        person_id = create_person.json()["id"]

        for target_tier in ("silver", "crystal", "gold"):
            t_resp = await client.patch(
                f"/v1/persons/{person_id}/tier",
                headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
                json={"tier": target_tier},
            )
            assert t_resp.status_code == 200
            assert t_resp.json()["tier"] == target_tier

        # 5. V10 `/api/v1/voice/ingest` compatibility endpoint
        ingest_resp = await client.post(
            "/api/v1/voice/ingest",
            headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
            json={
                "text": "阿泽不能吃花生，对花生严重过敏。",
                "routeMode": "current",
                "activePersonId": person_id,
            },
        )
        assert ingest_resp.status_code == 200
        ingest_body = ingest_resp.json()
        assert "<mark>阿泽</mark>" in ingest_body["transcriptHtml"]
        assert len(ingest_body["splits"]) >= 1

        # 6. Async `POST /v1/captures?sync=false` enqueued to PostgreSQL worker queue & drained
        async_cap = await client.post(
            "/v1/captures?sync=false",
            headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
            json={
                "text": "下周六答应陪阿泽去看展览。",
                "routeMode": "current",
                "activePersonId": person_id,
                "sync": False,
            },
        )
        assert async_cap.status_code == 202
        cap_id = async_cap.json()["captureId"]
        drained = await runner.drain()
        assert drained >= 1

        cap_polled = await client.get(f"/v1/captures/{cap_id}", headers=auth_headers)
        assert cap_polled.status_code == 200
        assert cap_polled.json()["status"] in ("completed", "needs_review")

        # Resolve any open pending inbox items (e.g. date/promise gate)
        inbox_resp = await client.get("/v1/inbox?status=open", headers=auth_headers)
        assert inbox_resp.status_code == 200
        for item in inbox_resp.json()["items"]:
            res_resp = await client.post(
                f"/v1/inbox/{item['id']}:resolve",
                headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
                json={"action": "confirm"},
            )
            assert res_resp.status_code == 200

        # 7. Cupid Companion SSE streaming chat + async background writeback worker drain
        async with client.stream(
            "POST",
            f"/v1/persons/{person_id}/chat/stream",
            headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
            json={
                "clientMessageId": str(uuid4()),
                "message": "帮我想想请阿泽吃饭要注意什么？",
            },
        ) as sse_resp:
            assert sse_resp.status_code == 200
            raw_sse = (await sse_resp.aread()).decode("utf-8")
            assert "event: done" in raw_sse

        await runner.drain()

        # 8. Register push device & run periodic scheduler tick + drain
        dev_resp = await client.post(
            "/v1/devices",
            headers={**auth_headers, "Idempotency-Key": f"idem-{uuid4()}"},
            json={
                "platform": "ios_apns",
                "pushToken": "apns-dev-token-1234567890",
                "deviceLabel": "iPhone 16 Pro",
                "enabled": True,
            },
        )
        assert dev_resp.status_code in (200, 201)
        assert runner._scheduler is not None
        await runner._scheduler.tick()
        await runner.drain()

        # 9. Step-up reauth -> Async GB 45438-2025 export -> Worker build -> Single-use ZIP download
        reauth_otp_req = await client.post(
            "/v1/auth/email/otp:request",
            json={"email": "linxia@example.com", "purpose": "reauth"},
        )
        assert reauth_otp_req.status_code == 200
        reauth_challenge = reauth_otp_req.json()
        reauth_verify = await client.post(
            "/v1/auth/email/otp:verify",
            headers=auth_headers,
            json={
                "email": "linxia@example.com",
                "challengeId": reauth_challenge["challengeId"],
                "code": reauth_challenge["devOtpCode"],
            },
        )
        assert reauth_verify.status_code == 200
        reauth_token = reauth_verify.json()["reauthToken"]

        export_create = await client.post(
            "/v1/me/exports",
            headers={
                **auth_headers,
                "X-Reauth-Token": reauth_token,
                "Idempotency-Key": f"idem-{uuid4()}",
            },
            json={"asyncMode": True},
        )
        assert export_create.status_code in (200, 201, 202)
        export_created_body = export_create.json()
        export_id = export_created_body["exportId"]
        assert export_created_body["status"] == "pending"
        assert export_created_body["downloadToken"] is None

        await runner.drain()

        # Without X-Reauth-Token, status endpoint hides downloadToken
        plain_status = await client.get(
            f"/v1/me/exports/{export_id}",
            headers=auth_headers,
        )
        assert plain_status.status_code == 200
        assert plain_status.json()["status"] == "completed"
        assert plain_status.json()["downloadToken"] is None

        # With X-Reauth-Token, completed async export issues a single-use downloadToken
        export_status = await client.get(
            f"/v1/me/exports/{export_id}",
            headers={**auth_headers, "X-Reauth-Token": reauth_token},
        )
        assert export_status.status_code == 200
        export_info = export_status.json()
        assert export_info["status"] == "completed"
        dl_token = export_info["downloadToken"]
        assert dl_token

        dl_resp = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": dl_token},
            headers=auth_headers,
        )
        assert dl_resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(dl_resp.content), "r") as zf:
            names = set(zf.namelist())
            assert {"data.json", "journal.md"}.issubset(names)
            exported_user_data = json.loads(zf.read("data.json").decode("utf-8"))
            assert "阿泽" in json.dumps(exported_user_data, ensure_ascii=False)

        # Replay of one-time download token must fail
        dl_replay = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": dl_token},
            headers=auth_headers,
        )
        assert dl_replay.status_code in (403, 410)

        # 10. Irreversible crypto-shredding account deletion (`DELETE /v1/me`)
        del_resp = await client.delete(
            "/v1/me",
            headers={
                **auth_headers,
                "X-Reauth-Token": reauth_token,
                "Idempotency-Key": f"idem-{uuid4()}",
            },
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["cryptoShredded"] is True

        # Existing JWT must immediately be rejected with 401
        post_del = await client.get("/v1/bootstrap", headers=auth_headers)
        assert post_del.status_code == 401
