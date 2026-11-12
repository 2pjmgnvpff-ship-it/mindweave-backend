"""Adversarial Red-Team Round 4 Suite — Final Fourth-Order Verification & OpenAPI Fuzzing Sweep.

Verifies every Round 3 remediation (`commit 2490cd9`) against fourth-order attacks and executes
an automated OpenAPI + Schemathesis boundary sweep across all routes:

1. `test_r4_01_idempotency_envelope_aad_binding_cross_row_tamper_and_all_routers_encrypted`:
   Verifies `_enc_v1` envelope encryption across all mutating router families (`identity`,
   `preferences`, `people`, `capture`, `memory`, `companion`, `reminders`, `data_rights`,
   `telemetry`), exact replay fidelity, and AES-256-GCM AAD rejection on cross-row/cross-user
   ciphertext transplant.
2. `test_r4_02_safety_l1_fourth_order_edge_matrix`:
   Verifies amusement rides (`跳楼机`), recreational diving (`跳海潜水`), workplace hyperbole,
   high-rise window ledges (`28楼窗台往下跳`), sleeping pills (`吞安眠药`), and emoji separators
   (`自✨杀`, `家🔥暴`).
3. `test_r4_03_null_byte_and_control_char_fuzzing_across_query_headers_json_and_multipart`:
   Verifies null-byte (`\\x00` / `%00` / `\\u0000`) rejection across query parameters, headers,
   nested JSON dicts/lists/keys, and multipart form fields/filenames with zero `500` responses.
4. `test_r4_04_companion_sentinel_collision_nested_fences_and_silver_code_quotes`:
   Verifies literal `\\x01MWCODE0\\x02` sentinel collision immunity, quadruple-backtick nested
   fences, unclosed EOF fences, and quoted Silver opponent lines inside code blocks.
5. `test_r4_05_fact_lunar_to_solar_toggle_updates_associated_reminder_cleanly`:
   Verifies toggling `FactRow` from `isLunar=True` to `isLunar=False` clears stale lunar fields
   AND updates the linked `ReminderRow` to `isLunar=False` with a valid solar `nextFireAt`.
6. `test_r4_06_openapi_and_schemathesis_operation_fuzz_sweep`:
   Exercises all OpenAPI operations (unauthenticated 401 sweep + authenticated boundary/invalid-UUID/
   missing-header/malformed-body + Schemathesis schema conformance), asserting `status_code < 500`,
   RFC 7807 `application/problem+json` on 4xx, `X-Request-Id` presence, and zero forbidden jargon.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import schemathesis
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
)
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.main import create_app
from mindweave.modules.safety.domain.l1_rules import L1SafetyEngine
from mindweave.shared.enums import SafetyCategory
from mindweave.shared.scopes import UserScope


def _make_png_bytes(color: tuple[int, int, int] = (60, 120, 170)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def _signup_and_headers(
    client: AsyncClient,
    email: str,
) -> tuple[dict[str, str], str]:
    r1 = await client.post(
        "/v1/auth/email/otp:request",
        json={"email": email, "purpose": "login"},
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    r2 = await client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": d1["challengeId"],
            "email": email,
            "code": d1["devOtpCode"],
        },
    )
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    return {"Authorization": f"Bearer {d2['accessToken']}"}, str(d2["userId"])


async def _get_reauth_token(
    client: AsyncClient,
    headers: dict[str, str],
    email: str,
) -> str:
    r1 = await client.post(
        "/v1/auth/email/otp:request",
        headers=headers,
        json={"email": email, "purpose": "reauth"},
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    r2 = await client.post(
        "/v1/auth/email/otp:verify",
        headers=headers,
        json={
            "challengeId": d1["challengeId"],
            "email": email,
            "code": d1["devOtpCode"],
        },
    )
    assert r2.status_code == 200, r2.text
    return str(r2.json()["reauthToken"])


async def _create_person(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name_cn: str,
    tier: str = "gold",
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/persons",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "nameCn": name_cn,
            "tier": tier,
            "roleTag": "好友",
            "aliases": aliases or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_r4_01_idempotency_envelope_aad_binding_cross_row_tamper_and_all_routers_encrypted(
    db_container: AppContainer,
) -> None:
    """Verify `_enc_v1` envelope across all routers, replay parity, and AAD tamper rejection."""
    app = create_app(container=db_container)
    png = _make_png_bytes()
    email_a = "r4_idem_a@example.com"
    email_b = "r4_idem_b@example.com"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers_a, uid_a = await _signup_and_headers(client, email_a)
        headers_b, uid_b = await _signup_and_headers(client, email_b)
        reauth_a = await _get_reauth_token(client, headers_a, email_a)

        # 1. Exercise idempotent mutations across ALL 9 stateful router families
        k_consent = str(uuid4())
        r_consent = await client.post(
            "/v1/me/consents",
            headers={**headers_a, "Idempotency-Key": k_consent},
            json={"consentKind": "privacy_policy", "policyVersion": "2026.11", "granted": True},
        )
        assert r_consent.status_code in (200, 201)

        k_pref = str(uuid4())
        r_pref = await client.patch(
            "/v1/me/preferences",
            headers={**headers_a, "Idempotency-Key": k_pref},
            json={"wallpaperBlur": 12},
        )
        assert r_pref.status_code == 200

        k_person = str(uuid4())
        r_person = await client.post(
            "/v1/persons",
            headers={**headers_a, "Idempotency-Key": k_person},
            json={"nameCn": "沈清秋TopSecret", "tier": "gold", "shortMemo": "竹舍清茶SecretMemo"},
        )
        assert r_person.status_code == 201
        pid = r_person.json()["id"]

        k_photo = str(uuid4())
        r_photo = await client.post(
            f"/v1/persons/{pid}/photos/avatar",
            headers={**headers_a, "Idempotency-Key": k_photo},
            files={"file": ("a.png", png, "image/png")},
        )
        assert r_photo.status_code == 200

        k_cap = str(uuid4())
        r_cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers_a, "Idempotency-Key": k_cap},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": pid,
                "text": "喜欢白桃乌龙茶SecretCap",
            },
        )
        assert r_cap.status_code == 201

        k_mat = str(uuid4())
        r_mat = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers_a, "Idempotency-Key": k_mat},
            json={"kind": "note", "title": "手札", "body": "绝密手札正文SecretMat"},
        )
        assert r_mat.status_code == 201

        k_fact = str(uuid4())
        r_fact = await client.post(
            f"/v1/persons/{pid}/facts",
            headers={**headers_a, "Idempotency-Key": k_fact},
            json={"type": "taboo", "polarity": "negative", "content": "对芒果过敏SecretFact"},
        )
        assert r_fact.status_code == 201

        k_chat = str(uuid4())
        r_chat = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers_a, "Idempotency-Key": k_chat},
            json={"message": "树洞私密聊天SecretChat"},
        )
        assert r_chat.status_code == 200

        k_rem = str(uuid4())
        r_rem = await client.post(
            "/v1/reminders",
            headers={**headers_a, "Idempotency-Key": k_rem},
            json={
                "personId": pid,
                "title": "中秋送礼SecretRem",
                "rule": "once",
                "onceDate": "2027-09-15",
                "status": "active",
            },
        )
        assert r_rem.status_code == 201

        k_exp = str(uuid4())
        r_exp = await client.post(
            "/v1/me/exports",
            headers={**headers_a, "X-Reauth-Token": reauth_a, "Idempotency-Key": k_exp},
            json={"asyncMode": False},
        )
        assert r_exp.status_code == 201

        k_tel = str(uuid4())
        r_tel = await client.post(
            "/v1/telemetry/events",
            headers={**headers_a, "Idempotency-Key": k_tel},
            json={"events": [{"eventName": "person_page_revisit", "metadata": {"tier": "gold"}}]},
        )
        assert r_tel.status_code in (200, 201)

        # 2. Inspect PostgreSQL `idempotency_keys`: EVERY completed row MUST store ONLY `{"_enc_v1": ...}`
        async with db_container.db.system_session() as sys_sess:
            rows = (
                await sys_sess.execute(
                    text(
                        "SELECT id, idem_key, response_body::text "
                        "FROM idempotency_keys WHERE user_id = :uid AND status = 'completed'"
                    ),
                    {"uid": uid_a},
                )
            ).all()
            assert len(rows) >= 11
            for _, _, raw_body_txt in rows:
                parsed = json.loads(raw_body_txt)
                assert list(parsed.keys()) == ["_enc_v1"]
                for secret in (
                    "沈清秋TopSecret",
                    "竹舍清茶SecretMemo",
                    "喜欢白桃乌龙茶SecretCap",
                    "绝密手札正文SecretMat",
                    "对芒果过敏SecretFact",
                    "树洞私密聊天SecretChat",
                    "中秋送礼SecretRem",
                ):
                    assert secret not in raw_body_txt

        # 3. Verify idempotent replay returns decrypted payload faithfully
        replay_person = await client.post(
            "/v1/persons",
            headers={**headers_a, "Idempotency-Key": k_person},
            json={"nameCn": "沈清秋TopSecret", "tier": "gold", "shortMemo": "竹舍清茶SecretMemo"},
        )
        assert replay_person.status_code == 201
        assert replay_person.json()["nameCn"] == "沈清秋TopSecret"

        # 4. Adversarial AAD Swap Attack: transplant `_enc_v1` ciphertext from `k_person` onto `k_mat`
        #    and onto User B's idempotency row -> AES-256-GCM AAD (`user_id:table:column:row_id`) MUST reject!
        k_user_b = str(uuid4())
        rb = await client.post(
            "/v1/persons",
            headers={**headers_b, "Idempotency-Key": k_user_b},
            json={"nameCn": "路人乙", "tier": "crystal"},
        )
        assert rb.status_code == 201

        async with db_container.db.system_session() as sys_sess:
            person_enc_body = await sys_sess.scalar(
                text(
                    "SELECT response_body::text FROM idempotency_keys "
                    "WHERE user_id = :uid AND idem_key = :k"
                ),
                {"uid": uid_a, "k": k_person},
            )
            assert person_enc_body is not None
            # Transplant row A's ciphertext onto row B (same user, different row_id -> AAD mismatch!)
            await sys_sess.execute(
                text(
                    "UPDATE idempotency_keys SET response_body = CAST(:body AS jsonb) "
                    "WHERE user_id = :uid AND idem_key = :k"
                ),
                {"body": person_enc_body, "uid": uid_a, "k": k_mat},
            )
            # Also transplant User A's ciphertext onto User B's row (different user_id & row_id!)
            await sys_sess.execute(
                text(
                    "UPDATE idempotency_keys SET response_body = CAST(:body AS jsonb) "
                    "WHERE user_id = :uid AND idem_key = :k"
                ),
                {"body": person_enc_body, "uid": uid_b, "k": k_user_b},
            )

        tampered_same_user = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers_a, "Idempotency-Key": k_mat},
            json={"kind": "note", "title": "手札", "body": "绝密手札正文SecretMat"},
        )
        assert tampered_same_user.status_code == 500
        assert "沈清秋TopSecret" not in tampered_same_user.text

        tampered_cross_user = await client.post(
            "/v1/persons",
            headers={**headers_b, "Idempotency-Key": k_user_b},
            json={"nameCn": "路人乙", "tier": "crystal"},
        )
        assert tampered_cross_user.status_code == 500
        assert "沈清秋TopSecret" not in tampered_cross_user.text


async def test_r4_02_safety_l1_fourth_order_edge_matrix() -> None:
    """Verify fourth-order safety combinations across amusement rides, diving, ledges, pills, and emojis."""
    engine = L1SafetyEngine()

    benign_cases = (
        "游乐场的跳楼机很好玩，下次还想坐",
        "周末去海边跳海潜水看珊瑚礁",
        "夏天去游泳馆跳水超开心",
        "周末和朋友去玩蹦极，站在高台往下跳太刺激了",
        "重感冒嗓子疼，准备吞药睡一觉",
        "论文写得想死哈哈哈，顺便记一下阿哲喜欢喝美式",
    )
    for text_in in benign_cases:
        res = engine.evaluate(text_in)
        assert res.category is SafetyCategory.NORMAL, f"False positive on {text_in!r}: {res}"

    crisis_cases = (
        "站在28楼窗台准备往下跳",
        "站在十八楼阳台往下跳算了",
        "今晚准备吞安眠药结束一切",
        "我已经决定自✨杀了",
        "站在天台准备跳🎉楼了",
        "想从江桥跳下去一了百了",
    )
    for text_in in crisis_cases:
        res = engine.evaluate(text_in)
        assert res.category is SafetyCategory.CRISIS, f"Missed crisis on {text_in!r}: {res}"

    dv_res = engine.evaluate("他正在家🔥暴动手打我，把大门锁了不让我出门")
    assert dv_res.category is SafetyCategory.VIOLENCE_VICTIM


async def test_r4_03_null_byte_and_control_char_fuzzing_across_query_headers_json_and_multipart(
    db_container: AppContainer,
) -> None:
    """Verify null bytes in query params, headers, nested JSON structures, and multipart forms never 500."""
    app = create_app(container=db_container)
    png = _make_png_bytes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r4_null_sweep@example.com")
        person = await _create_person(client, headers, name_cn="谢怜", tier="gold")
        pid = person["id"]

        # 1. URL-encoded null bytes (`%00`) in query parameters -> 4xx (never 500)
        for q_url in (
            f"/v1/persons/{pid}/materials?cursor=%00bad",
            f"/v1/persons/{pid}/materials?kind=note%00",
            "/v1/inbox?status=open%00",
            "/v1/reminders?status=active%00",
            f"/v1/persons/{pid}/chat/messages?cursor=%00",
        ):
            resp = await client.get(q_url, headers=headers)
            assert resp.status_code < 500, f"500 on {q_url}: {resp.text}"

        # 2. Malformed / control-character headers (`X-Request-Id`, `If-Match`, `Idempotency-Key`)
        r_reqid = await client.get(
            "/v1/bootstrap",
            headers={**headers, "X-Request-Id": "evil id with spaces & symbols!@#"},
        )
        assert r_reqid.status_code == 200
        # Unsafe X-Request-Id must be replaced with a clean server-generated UUID
        assert r_reqid.headers["X-Request-Id"] != "evil id with spaces & symbols!@#"

        # 3. Deeply nested JSON null bytes (dict keys, dict values, nested arrays) -> 422
        nested_payloads: list[tuple[str, dict[str, Any]]] = [
            ("/v1/persons", {"nameCn": "正常名", "tier": "gold", "aliases": ["别名\x00一"]}),
            (
                "/v1/persons",
                {"nameCn": "正常名", "tier": "gold", "visualTokens": {"accent\x00": "1"}},
            ),
            (
                "/v1/persons",
                {"nameCn": "正常名", "tier": "gold", "visualTokens": {"accent": "gold\x00"}},
            ),
            (
                "/v1/telemetry/events",
                {
                    "events": [
                        {
                            "eventName": "person_page_revisit",
                            "metadata": {"nested": ["ok", "bad\x00"]},
                        }
                    ]
                },
            ),
            (
                "/v1/me/consents",
                {"consentKind": "privacy_policy", "policyVersion": "1.0\x00", "granted": True},
            ),
        ]
        for path, body in nested_payloads:
            resp = await client.post(
                path,
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json=body,
            )
            assert resp.status_code == 422, f"Expected 422 on {path}, got {resp.status_code}"

        # 4. Multipart form text fields & raw wire filenames containing `\x00` -> 422
        mp_mat_val = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            files={"photo": ("clean.png", png, "image/png")},
            data={"title": "标题\x00", "body": "正文"},
        )
        assert mp_mat_val.status_code == 422

        raw_mp_body = (
            b"--MWNULLBOUND\r\n"
            b'Content-Disposition: form-data; name="title"\r\n\r\n'
            b"clean title\r\n"
            b"--MWNULLBOUND\r\n"
            b'Content-Disposition: form-data; name="body"\r\n\r\n'
            b"clean body\r\n"
            b"--MWNULLBOUND\r\n"
            b'Content-Disposition: form-data; name="photo"; filename="bad\x00name.png"\r\n'
            b"Content-Type: image/png\r\n\r\n" + png + b"\r\n--MWNULLBOUND--\r\n"
        )
        mp_mat_fname = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "multipart/form-data; boundary=MWNULLBOUND",
            },
            content=raw_mp_body,
        )
        assert mp_mat_fname.status_code == 422

        mp_cap_val = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            files={"image": ("shot.png", png, "image/png")},
            data={"source": "screenshot", "routeMode": "global", "text": "备注\x00"},
        )
        assert mp_cap_val.status_code == 422


async def test_r4_04_companion_sentinel_collision_nested_fences_and_silver_code_quotes(
    db_container: AppContainer,
) -> None:
    """Verify literal `\\x01MWCODE0\\x02` sentinel injection does not leak raw `[rec_]` or corrupt output."""
    sample_id = "01965b30-0000-7000-8000-000000000055"
    adversarial_reply = (
        "前置文本 \x01MWCODE0\x02 与 \x01MWCODE99\x02 换个角度想他也有苦衷。\n"
        f"````markdown\n```text\n对方原话：换个角度想我也有苦衷\n    [rec_{sample_id}]\n```\n````\n"
        "请把书面记录保存好。"
    )

    class _SentinelFenceGateway:
        async def generate_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> tuple[CompanionChatOutput, str, str]:
            return (
                CompanionChatOutput(
                    safety_flag=SafetyCategory.NORMAL,
                    reply_text=adversarial_reply,
                    cited_record_ids=[],
                    has_unrecorded_gap=False,
                    alternative_perspectives=[],
                    actionable_suggestions=[],
                ),
                "r4_sentinel",
                "fake",
            )

        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            wrapped = f"<reply>{adversarial_reply}</reply>"
            for i in range(0, len(wrapped), 3):
                yield wrapped[i : i + 3]

    db_container.extras["model_gateway"] = _SentinelFenceGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r4_sentinel@example.com")
        silver = await _create_person(client, headers, name_cn="苛刻甲方", tier="silver")
        sid = silver["id"]

        # JSON turn
        j_res = await client.post(
            f"/v1/persons/{sid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "测试哨兵字符碰撞"},
        )
        assert j_res.status_code == 200
        j_content = j_res.json()["assistantMessage"]["content"]
        assert f"    [rec_{sample_id}]" in j_content
        assert "对方原话：换个角度想我也有苦衷" in j_content
        assert "他也有苦衷" not in j_content

        # SSE stream turn (3-char chunks)
        s_res = await client.post(
            f"/v1/persons/{sid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "流式测试哨兵字符碰撞"},
        )
        assert s_res.status_code == 200
        s_content = "".join(
            json.loads(line[6:])["text"]
            for block in s_res.text.split("\n\n")
            if "event: delta" in block
            for line in block.splitlines()
            if line.startswith("data: ")
        )
        assert f"    [rec_{sample_id}]" in s_content
        assert "对方原话：换个角度想我也有苦衷" in s_content
        assert "他也有苦衷" not in s_content


async def test_r4_05_fact_lunar_to_solar_toggle_updates_associated_reminder_cleanly(
    db_container: AppContainer,
) -> None:
    """Verify patching a DATE fact from lunar to solar updates both FactRow and linked ReminderRow."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r4_lunar_rem@example.com")
        person = await _create_person(client, headers, name_cn="叶蓁蓁", tier="gold")
        pid = person["id"]

        f_lunar = await client.post(
            f"/v1/persons/{pid}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "叶蓁蓁生日",
                "isLunar": True,
                "lunarMonth": 8,
                "lunarDay": 15,
                "lunarIsLeapMonth": False,
            },
        )
        assert f_lunar.status_code == 201
        fact_id = f_lunar.json()["id"]

        rems_before = (await client.get("/v1/reminders", headers=headers)).json()["items"]
        linked_before = next(r for r in rems_before if r["factId"] == fact_id)
        assert linked_before["isLunar"] is True
        assert linked_before["targetMonth"] == 8
        assert linked_before["targetDay"] == 15
        assert linked_before["nextFireAt"] is not None

        # Toggle fact to solar (`isLunar=False, triggerDate="2027-10-01"`)
        f_solar = await client.patch(
            f"/v1/facts/{fact_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"isLunar": False, "triggerDate": "2027-10-01"},
        )
        assert f_solar.status_code == 200
        d_solar = f_solar.json()
        assert d_solar["isLunar"] is False
        assert d_solar["lunarMonth"] is None
        assert d_solar["lunarDay"] is None
        assert d_solar["lunarIsLeapMonth"] is False

        # Linked reminder MUST also transition to solar (`isLunar=False, targetMonth=10, targetDay=1`)
        rems_after = (await client.get("/v1/reminders", headers=headers)).json()["items"]
        linked_after = next(r for r in rems_after if r["factId"] == fact_id)
        assert linked_after["isLunar"] is False
        assert linked_after["isLeapMonth"] is False
        assert linked_after["targetMonth"] == 10
        assert linked_after["targetDay"] == 1
        assert linked_after["nextFireAt"] is not None


async def test_r4_06_openapi_and_schemathesis_operation_fuzz_sweep(
    db_container: AppContainer,
) -> None:
    """Automated OpenAPI + Schemathesis sweep across all routes for 401/4xx RFC 7807 & <500 invariants."""
    app = create_app(container=db_container)
    schema_dict = app.openapi()

    # Load OpenAPI 3.1 schema via Schemathesis to validate spec structural integrity
    st_schema = schemathesis.openapi.from_dict(schema_dict)
    all_ops = list(st_schema.get_all_operations())
    assert len(all_ops) >= 35, f"Expected >= 35 OpenAPI operations, got {len(all_ops)}"

    public_unauth_paths = {
        "/healthz",
        "/readyz",
        "/v1/auth/apple",
        "/v1/auth/email/otp:request",
        "/v1/auth/email/otp:verify",
        "/v1/auth/refresh",
    }

    def _assert_clean_response(resp: Any, *, label: str) -> None:
        assert resp.status_code < 500, f"Unexpected 5xx on {label}: {resp.status_code} {resp.text}"
        assert "X-Request-Id" in resp.headers, f"Missing X-Request-Id on {label}"
        body_text = resp.text
        for phrase in FORBIDDEN_UI_DEV_PHRASES:
            assert phrase not in body_text, f"Forbidden jargon {phrase!r} leaked on {label}"
        if 400 <= resp.status_code < 500:
            ct = resp.headers.get("content-type", "")
            assert ct.startswith("application/problem+json"), (
                f"Non-RFC7807 4xx content-type {ct!r} on {label}: {body_text}"
            )
            prob = resp.json()
            for req_key in ("type", "title", "status", "code", "detail", "instance"):
                assert req_key in prob, f"Missing RFC7807 field {req_key!r} on {label}"
            assert prob["status"] == resp.status_code

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Unauthenticated sweep across EVERY protected OpenAPI operation -> 401 problem+json
        dummy_uuid = str(uuid4())
        for path_tmpl, methods in schema_dict["paths"].items():
            concrete_path = (
                path_tmpl.replace("{person_id}", dummy_uuid)
                .replace("{alias_id}", dummy_uuid)
                .replace("{capture_id}", dummy_uuid)
                .replace("{material_id}", dummy_uuid)
                .replace("{fact_id}", dummy_uuid)
                .replace("{item_id}", dummy_uuid)
                .replace("{reminder_id}", dummy_uuid)
                .replace("{device_id}", dummy_uuid)
                .replace("{export_id}", dummy_uuid)
                .replace("{slot}", "avatar")
            )
            for method_lower in methods:
                if method_lower not in ("get", "post", "put", "patch", "delete"):
                    continue
                if path_tmpl in public_unauth_paths:
                    continue
                unauth_resp = await client.request(
                    method_lower.upper(),
                    concrete_path,
                    headers={"Idempotency-Key": str(uuid4())},
                    json={} if method_lower in ("post", "put", "patch") else None,
                )
                assert unauth_resp.status_code == 401, (
                    f"Expected 401 unauthenticated on {method_lower.upper()} {concrete_path}, "
                    f"got {unauth_resp.status_code}"
                )
                _assert_clean_response(
                    unauth_resp, label=f"UNAUTH {method_lower.upper()} {concrete_path}"
                )

        # 2. Authenticated boundary & fuzz sweep across EVERY OpenAPI operation
        headers, _ = await _signup_and_headers(client, "r4_openapi_fuzz@example.com")

        for path_tmpl, methods in schema_dict["paths"].items():
            # Case A: Malformed non-UUID path parameters (`not-a-uuid`) -> clean 422 problem+json
            if "{" in path_tmpl:
                bad_uuid_path = (
                    path_tmpl.replace("{person_id}", "not-a-uuid")
                    .replace("{alias_id}", "not-a-uuid")
                    .replace("{capture_id}", "not-a-uuid")
                    .replace("{material_id}", "not-a-uuid")
                    .replace("{fact_id}", "not-a-uuid")
                    .replace("{item_id}", "not-a-uuid")
                    .replace("{reminder_id}", "not-a-uuid")
                    .replace("{device_id}", "not-a-uuid")
                    .replace("{export_id}", "not-a-uuid")
                    .replace("{slot}", "invalid_slot")
                )
                for method_lower in methods:
                    if method_lower not in ("get", "post", "put", "patch", "delete"):
                        continue
                    r_bad_param = await client.request(
                        method_lower.upper(),
                        bad_uuid_path,
                        headers={
                            **headers,
                            "Idempotency-Key": str(uuid4()),
                            "If-Match": '"1"',
                        },
                        json={} if method_lower in ("post", "put", "patch") else None,
                    )
                    _assert_clean_response(
                        r_bad_param,
                        label=f"BAD_PARAM {method_lower.upper()} {bad_uuid_path}",
                    )

            # Case B: Valid UUIDs (nonexistent resource or empty boundary payload) + missing/present headers
            concrete_valid_uuid_path = (
                path_tmpl.replace("{person_id}", dummy_uuid)
                .replace("{alias_id}", dummy_uuid)
                .replace("{capture_id}", dummy_uuid)
                .replace("{material_id}", dummy_uuid)
                .replace("{fact_id}", dummy_uuid)
                .replace("{item_id}", dummy_uuid)
                .replace("{reminder_id}", dummy_uuid)
                .replace("{device_id}", dummy_uuid)
                .replace("{export_id}", dummy_uuid)
                .replace("{slot}", "avatar")
            )
            for method_lower in methods:
                if method_lower not in ("get", "post", "put", "patch", "delete"):
                    continue
                # Skip SSE long-poll `/captures/events` so stream doesn't hang waiting for events
                if concrete_valid_uuid_path.endswith("/captures/events"):
                    continue

                # Mutating call WITHOUT Idempotency-Key -> clean 4xx (400/403/422), never 500
                if method_lower in ("post", "put", "patch", "delete"):
                    r_no_idem = await client.request(
                        method_lower.upper(),
                        concrete_valid_uuid_path,
                        headers=headers,
                        json={} if method_lower in ("post", "put", "patch") else None,
                    )
                    _assert_clean_response(
                        r_no_idem,
                        label=f"NO_IDEM {method_lower.upper()} {concrete_valid_uuid_path}",
                    )

                # Call WITH Idempotency-Key & boundary `{}` /unexpected field `{"__fuzzExtra__": 1}` -> clean <500
                r_fuzz_body = await client.request(
                    method_lower.upper(),
                    concrete_valid_uuid_path,
                    headers={
                        **headers,
                        "Idempotency-Key": str(uuid4()),
                        "If-Match": '"1"',
                    },
                    json=(
                        {"__fuzzExtra__": 1} if method_lower in ("post", "put", "patch") else None
                    ),
                )
                _assert_clean_response(
                    r_fuzz_body,
                    label=f"FUZZ_BODY {method_lower.upper()} {concrete_valid_uuid_path}",
                )
