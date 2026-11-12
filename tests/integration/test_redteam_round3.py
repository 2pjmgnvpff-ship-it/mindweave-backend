"""Adversarial Red-Team Round 3 Suite — Third-Order Bypasses, API Fuzzing & Zero-PII Sweep.

Targets third-order interactions across Round 2 fixes (`commit 214083d`), boundary/malformed
input fuzzing, `/api/v1/*` V10 compatibility parity, and database at-rest PII inspection:

- Part 1 (`test_r3_hardening_*`): 6 comprehensive passing suites proving Round 2 fixes and
  API boundaries that held firm under adversarial stress.
- Part 2 (`test_r3_01_*` .. `test_r3_05_*`): Promoted to permanent strict CI regression gates.
"""

from __future__ import annotations

import io
import json
import struct
import wave
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
)
from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.main import create_app
from mindweave.modules.safety.domain.l1_rules import L1SafetyEngine
from mindweave.shared.enums import SafetyCategory
from mindweave.shared.scopes import UserScope


def _make_png_bytes(color: tuple[int, int, int] = (70, 130, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes(duration_ms: int = 400, sample_rate: int = 8000) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{num_samples}h", *([0] * num_samples)))
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
            "roleTag": "朋友",
            "aliases": aliases or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# =============================================================================
# PART 1: Passing Third-Order Adversarial Hardening & Boundary Fuzzing Proofs
# =============================================================================


async def test_r3_hardening_turn1_ok_turn2_fail_retry_stream_and_json_preserves_history_and_quota(
    db_container: AppContainer,
) -> None:
    """Verify multi-turn failure+retry on BOTH SSE stream and stream=false preserves prior turns & quota."""
    stream_calls = 0
    json_calls = 0

    class _MultiTurnFlakyGateway:
        async def generate_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> tuple[CompanionChatOutput, str, str]:
            nonlocal json_calls
            json_calls += 1
            if json_calls == 2:
                raise RuntimeError("transient json upstream failure on turn 3")
            return (
                CompanionChatOutput(
                    safety_flag=SafetyCategory.NORMAL,
                    reply_text=f"JSON回复#{json_calls}。",
                    cited_record_ids=[],
                    has_unrecorded_gap=False,
                    alternative_perspectives=[],
                    actionable_suggestions=[],
                ),
                "r3_test",
                "fake",
            )

        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls == 1:
                yield "<reply>半截流式中断"
                raise RuntimeError("transient stream upstream failure on turn 2")
            yield "<reply>第二轮流式重试成功。</reply>"

    db_container.extras["model_gateway"] = _MultiTurnFlakyGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, uid = await _signup_and_headers(client, "r3_multiturn@example.com")
        person = await _create_person(client, headers, name_cn="温以凡", tier="gold")
        pid = person["id"]

        # Turn 1 (JSON): succeeds cleanly -> [user1, assistant1]
        t1 = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "第一轮正常提问", "clientMessageId": "cmsg-t1"},
        )
        assert t1.status_code == 200

        # Turn 2 (SSE): fails mid-stream, then retries with same Idempotency-Key -> [user1, asst1, user2, asst2]
        idem_t2 = str(uuid4())
        t2_fail = await client.post(
            f"/v1/persons/{pid}/chat/stream",
            headers={**headers, "Idempotency-Key": idem_t2},
            json={"message": "第二轮流式提问", "clientMessageId": "cmsg-t2"},
        )
        assert t2_fail.status_code == 200
        assert "event: error" in t2_fail.text

        t2_ok = await client.post(
            f"/v1/persons/{pid}/chat/stream",
            headers={**headers, "Idempotency-Key": idem_t2},
            json={"message": "第二轮流式提问", "clientMessageId": "cmsg-t2"},
        )
        assert t2_ok.status_code == 200
        assert "event: done" in t2_ok.text

        # Turn 3 (JSON stream=false): fails upstream (500), then retries with same Idempotency-Key -> succeeds!
        idem_t3 = str(uuid4())
        t3_fail = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": idem_t3},
            json={"message": "第三轮JSON提问", "clientMessageId": "cmsg-t3"},
        )
        assert t3_fail.status_code == 500

        t3_ok = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": idem_t3},
            json={"message": "第三轮JSON提问", "clientMessageId": "cmsg-t3"},
        )
        assert t3_ok.status_code == 200

        # Verify exact message sequence: [user1, asst1, user2, asst2, user3, asst3] == 6 messages, 3 user quota used
        hist = await client.get(f"/v1/persons/{pid}/chat/messages", headers=headers)
        assert hist.status_code == 200
        msgs = hist.json()["items"]
        assert [m["role"] for m in msgs] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert msgs[0]["content"] == "第一轮正常提问"
        assert msgs[2]["content"] == "第二轮流式提问"
        assert msgs[4]["content"] == "第三轮JSON提问"

        async with db_container.db.system_session() as sys_sess:
            msg_cnt = await sys_sess.scalar(
                text(
                    "SELECT message_count FROM chat_threads "
                    "WHERE user_id = :uid AND person_id = :pid"
                ),
                {"uid": uid, "pid": pid},
            )
            assert msg_cnt == 6


async def test_r3_hardening_archive_unarchive_cycle_and_soft_deleted_subresources_404(
    db_container: AppContainer,
) -> None:
    """Verify archive -> pause -> unarchive -> reactivate cycle and 404 across all soft-deleted subresources."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r3_unarchive@example.com")
        person = await _create_person(client, headers, name_cn="桑延", tier="gold")
        pid = person["id"]

        rem = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": pid,
                "title": "桑延生日",
                "rule": "once",
                "onceDate": "2027-08-18",
                "status": "active",
            },
        )
        assert rem.status_code == 201
        rem_id = rem.json()["id"]

        # 1. Archive -> pauses reminder; reactivating while archived returns 422
        r_arch = await client.patch(
            f"/v1/persons/{pid}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"archived": True},
        )
        assert r_arch.status_code == 200
        assert (
            await client.patch(
                f"/v1/reminders/{rem_id}",
                headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
                json={"status": "active"},
            )
        ).status_code == 422

        # 2. Unarchive -> restores active state, allows reminder reactivation & scoped capture!
        r_unarch = await client.patch(
            f"/v1/persons/{pid}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"2"'},
            json={"archived": False},
        )
        assert r_unarch.status_code == 200
        assert r_unarch.json()["state"] == "active"

        r_react = await client.patch(
            f"/v1/reminders/{rem_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"status": "active"},
        )
        assert r_react.status_code == 200
        assert r_react.json()["status"] == "active"

        cap_ok = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": pid,
                "text": "喜欢喝冻柠茶",
            },
        )
        assert cap_ok.status_code == 201

        # 3. Soft-delete -> all person sub-endpoints and reminder endpoints return 404
        assert (
            await client.delete(
                f"/v1/persons/{pid}",
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
        ).status_code == 204
        assert (await client.get(f"/v1/persons/{pid}/facts", headers=headers)).status_code == 404
        assert (
            await client.get(f"/v1/persons/{pid}/materials", headers=headers)
        ).status_code == 404
        rem_list = (await client.get("/v1/reminders", headers=headers)).json()["items"]
        assert all(r["id"] != rem_id for r in rem_list)
        assert (
            await client.post(
                f"/v1/reminders/{rem_id}:done",
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/v1/reminders/{rem_id}",
                headers={**headers, "Idempotency-Key": str(uuid4())},
            )
        ).status_code == 404


async def test_r3_hardening_inbox_keep_as_note_across_new_person_disambiguation_and_venting(
    db_container: AppContainer,
) -> None:
    """Verify `action=keep_as_note` across new_person_confirm, person_disambiguation, and venting_hold."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r3_keepnote@example.com")

        chen1 = await _create_person(
            client, headers, name_cn="陈嘉树", tier="gold", aliases=["老陈"]
        )
        await _create_person(client, headers, name_cn="陈远山", tier="crystal", aliases=["老陈"])
        target_id = chen1["id"]

        # 1. new_person_confirm + keep_as_note reassigned to target_id -> cleans up provisional 周予安!
        c1 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "周予安喜欢收集黑胶唱片"},
        )
        assert c1.status_code == 201
        ib1 = c1.json()["inboxItemIds"][0]
        r1 = await client.post(
            f"/v1/inbox/{ib1}:resolve",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"action": "keep_as_note", "targetPersonId": target_id},
        )
        assert r1.status_code == 200
        names = {
            p["nameCn"] for p in (await client.get("/v1/persons", headers=headers)).json()["items"]
        }
        assert "周予安" not in names

        # 2. person_disambiguation + keep_as_note -> attaches note to target_id, rejects pending facts
        c2 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "老陈对海鲜过敏"},
        )
        assert c2.status_code == 201
        ib2 = c2.json()["inboxItemIds"][0]
        r2 = await client.post(
            f"/v1/inbox/{ib2}:resolve",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"action": "keep_as_note", "targetPersonId": target_id},
        )
        assert r2.status_code == 200
        chen_facts = (await client.get(f"/v1/persons/{target_id}/facts", headers=headers)).json()[
            "items"
        ]
        assert all("海鲜过敏" not in f["content"] for f in chen_facts)
        chen_mats = (
            await client.get(f"/v1/persons/{target_id}/materials", headers=headers)
        ).json()["items"]
        assert any("海鲜过敏" in m["body"] or "黑胶唱片" in m["body"] for m in chen_mats)

        # 3. venting_hold + keep_as_note -> keeps diary note without promoting angry sarcasm into facts
        c3 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": target_id,
                "text": "真是年度最佳大善人，每次约会都迟到两小时，其实刚才全是气话",
            },
        )
        assert c3.status_code == 201
        if c3.json()["inboxItemIds"]:
            ib3 = c3.json()["inboxItemIds"][0]
            r3 = await client.post(
                f"/v1/inbox/{ib3}:resolve",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"action": "keep_as_note"},
            )
            assert r3.status_code == 200


async def test_r3_hardening_material_upload_and_patch_idempotency_replay_resigns_media_url(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    """Verify replaying POST /materials and PATCH /materials after >15m issues fresh working mediaUrl."""
    app = create_app(container=db_container)
    png = _make_png_bytes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        email = "r3_mat_resign@example.com"
        headers, _ = await _signup_and_headers(client, email)
        person = await _create_person(client, headers, name_cn="乔晶晶", tier="gold")
        pid = person["id"]

        idem_create = str(uuid4())
        m1 = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": idem_create},
            files={"photo": ("p.png", png, "image/png")},
            data={"title": "拍立得", "body": "初雪纪念"},
        )
        assert m1.status_code == 201
        mat_id = m1.json()["id"]

        # Advance 16m (>15m media token & access JWT TTL), re-login, and replay POST /materials
        manual_clock.advance(seconds=960)
        headers, _ = await _signup_and_headers(client, email)
        m1_replay = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": idem_create},
            files={"photo": ("p.png", png, "image/png")},
            data={"title": "拍立得", "body": "初雪纪念"},
        )
        assert m1_replay.status_code == 201
        assert (await client.get(m1_replay.json()["mediaUrl"])).status_code == 200

        # PATCH /materials + advance another 16m + re-login + replay PATCH /materials
        idem_patch = str(uuid4())
        p1 = await client.patch(
            f"/v1/materials/{mat_id}",
            headers={**headers, "Idempotency-Key": idem_patch, "If-Match": '"1"'},
            json={"pinned": True},
        )
        assert p1.status_code == 200
        manual_clock.advance(seconds=960)
        headers, _ = await _signup_and_headers(client, email)
        p1_replay = await client.patch(
            f"/v1/materials/{mat_id}",
            headers={**headers, "Idempotency-Key": idem_patch, "If-Match": '"1"'},
            json={"pinned": True},
        )
        assert p1_replay.status_code == 200
        assert (await client.get(p1_replay.json()["mediaUrl"])).status_code == 200


async def test_r3_hardening_tilde_code_fence_with_silver_pre_and_post_preaching_scrubbed(
    db_container: AppContainer,
) -> None:
    """Verify `~~~` code fence preserves `[rec_...]` while scrubbing Silver preaching before & after."""
    sample_id = "01965b30-0000-7000-8000-000000000077"

    class _TildeSilverGateway:
        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            raw = (
                "<reply>换个角度想也许他也有苦衷。\n"
                f"~~~markdown\n    [rec_{sample_id}]\n~~~\n"
                "你可以试着体谅他。先把书面证据留好，绝不替他背锅。</reply>"
            )
            for i in range(0, len(raw), 4):
                yield raw[i : i + 4]

    db_container.extras["model_gateway"] = _TildeSilverGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r3_tilde@example.com")
        silver = await _create_person(client, headers, name_cn="越界同事", tier="silver")
        sid = silver["id"]

        sse = await client.post(
            f"/v1/persons/{sid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "帮我看看怎么固定证据"},
        )
        assert sse.status_code == 200
        deltas = [
            json.loads(line[6:])["text"]
            for block in sse.text.split("\n\n")
            if "event: delta" in block
            for line in block.splitlines()
            if line.startswith("data: ")
        ]
        streamed = "".join(deltas)
        assert f"    [rec_{sample_id}]" in streamed
        assert "苦衷" not in streamed
        assert "体谅" not in streamed
        assert "先把书面证据留好" in streamed


async def test_r3_hardening_sql_wildcards_pagination_fuzz_v10_aliases_and_zero_pii_tables(
    db_container: AppContainer,
) -> None:
    """Fuzz SQL wildcards, pagination bounds, cursors, malformed multipart, V10 /api/v1/* aliases & PII tables."""
    app = create_app(container=db_container)
    wav = _make_wav_bytes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, uid = await _signup_and_headers(client, "r3_fuzz_v10@example.com")

        # 1. SQL wildcard / injection metacharacters in person, alias, material, fact
        wild_person = await _create_person(
            client,
            headers,
            name_cn="林%_\\'溪",
            tier="gold",
            aliases=["100%_off\\' OR 1=1--"],
        )
        wpid = wild_person["id"]

        # 2. Pagination bounds (`limit=-1`, `limit=0`, `limit=999999`) & invalid cursors -> clean 422 (never 500)
        for bad_q in ("limit=-1", "limit=0", "limit=999999", "cursor=!!!bad!!!", "cursor=e30"):
            r_mat = await client.get(f"/v1/persons/{wpid}/materials?{bad_q}", headers=headers)
            assert r_mat.status_code == 422
            r_rem = await client.get(f"/v1/reminders?{bad_q}", headers=headers)
            assert r_rem.status_code == 422
            r_chat = await client.get(f"/v1/persons/{wpid}/chat/messages?{bad_q}", headers=headers)
            assert r_chat.status_code == 422

        # 3. Malformed JSON & truncated multipart boundary -> 400/422 (never 500)
        bad_json = await client.post(
            "/v1/persons",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "application/json",
            },
            content=b'{"nameCn": "unterminated',
        )
        assert bad_json.status_code in (400, 422)

        bad_mp = await client.post(
            "/v1/voice/ingest",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "multipart/form-data; boundary=MWBOUND",
            },
            content=b'--MWBOUND\r\nContent-Disposition: form-data; name="audio"\r\n\r\ntruncated',
        )
        assert bad_mp.status_code in (400, 422)

        # 4. V10 `/api/v1/*` compatibility alias parity (`/api/v1/bootstrap`, `/api/v1/voice/ingest`, `/api/v1/persons/{id}/tier`)
        boot_v10 = await client.get("/api/v1/bootstrap", headers=headers)
        assert boot_v10.status_code == 200
        assert wpid in boot_v10.json()["coreIds"]

        tier_v10 = await client.patch(
            f"/api/v1/persons/{wpid}/tier",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"tier": "crystal"},
        )
        assert tier_v10.status_code == 200
        assert tier_v10.json()["tier"] == "crystal"

        voice_v10 = await client.post(
            "/api/v1/voice/ingest",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            files={"audio": ("clip.wav", wav, "audio/wav")},
            data={"routeMode": "current", "activePersonId": wpid, "text": "PIIProbeSecretVoice777"},
        )
        assert voice_v10.status_code == 200
        assert "jobId" in voice_v10.json()

        # 5. Verify zero PII in `audit_log`, `telemetry_events`, and `background_jobs.payload_json`
        async with db_container.db.system_session() as sys_sess:
            for sql in (
                "SELECT COALESCE(metadata_json::text, '') FROM audit_log",
                "SELECT COALESCE(metadata_json::text, '') FROM telemetry_events WHERE user_id = :uid",
                "SELECT COALESCE(payload_json::text, '') FROM background_jobs WHERE user_id = :uid",
            ):
                blobs = (await sys_sess.scalars(text(sql), {"uid": uid})).all()
                joined = "\n".join(blobs)
                assert "r3_fuzz_v10@example.com" not in joined
                assert "PIIProbeSecretVoice777" not in joined
                assert "林%_" not in joined


# =============================================================================
# PART 2: Strict XFAIL Reproductions of Real Round 3 Third-Order Bugs
# =============================================================================


async def test_r3_01_idempotency_keys_response_body_leaks_plaintext_user_pii_at_rest(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, uid = await _signup_and_headers(client, "r3_idem_pii@example.com")

        person = await _create_person(client, headers, name_cn="楚晚宁SecretName", tier="gold")
        pid = person["id"]

        await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"kind": "note", "title": "手札", "body": "私密手札内容SecretDiary888"},
        )
        await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "树洞绝密倾诉SecretChat999"},
        )

        async with db_container.db.system_session() as sys_sess:
            raw_idem_rows = (
                await sys_sess.scalars(
                    text(
                        "SELECT COALESCE(response_body::text, '') "
                        "FROM idempotency_keys WHERE user_id = :uid"
                    ),
                    {"uid": uid},
                )
            ).all()
            raw_dump = "\n".join(raw_idem_rows)

        assert "楚晚宁SecretName" not in raw_dump
        assert "私密手札内容SecretDiary888" not in raw_dump
        assert "树洞绝密倾诉SecretChat999" not in raw_dump


async def test_r3_02_l1_safety_emoji_evasion_and_bungee_cold_medicine_false_positives() -> None:
    engine = L1SafetyEngine()

    # 1. Adversarial intra-CJK emoji / slash evasion MUST still trigger CRISIS:
    assert engine.evaluate("我已经站在天台准备跳🎉楼了").category is SafetyCategory.CRISIS
    assert engine.evaluate("今晚准备自/杀结束一切").category is SafetyCategory.CRISIS

    # 2. Benign everyday sport / cold medicine phrases MUST NOT false-positive lock as CRISIS:
    assert (
        engine.evaluate("周末和朋友去玩蹦极，站在高台往下跳太刺激了").category
        is SafetyCategory.NORMAL
    )
    assert engine.evaluate("重感冒嗓子疼，准备吞药睡一觉").category is SafetyCategory.NORMAL


async def test_r3_03_null_byte_in_json_strings_causes_500_internal_server_error(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r3_nullbyte@example.com")
        person = await _create_person(client, headers, name_cn="谢怜", tier="gold")
        pid = person["id"]

        r_person = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"nameCn": "花\x00城", "tier": "gold"},
        )
        r_chat = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "你好", "clientMessageId": "cmsg\x00bad"},
        )
        r_mat = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"kind": "note", "title": "标题", "body": "正文", "stampCode": "st\x001"},
        )

        assert r_person.status_code == 422
        assert r_chat.status_code == 422
        assert r_mat.status_code == 422


async def test_r3_04_quadruple_fence_unclosed_eof_fence_and_silver_code_quote_corruption(
    db_container: AppContainer,
) -> None:
    sample_id = "01965b30-0000-7000-8000-000000000088"
    replies: list[str] = [
        # Reply 1: quadruple-backtick fence containing nested triple-backticks + [rec_...]
        f"<reply>嵌套示例：\n````markdown\n```\n    [rec_{sample_id}]\n```\n````\n以上为原样代码。</reply>",
        # Reply 2: unclosed code fence truncated at EOF + Silver quoted opponent line inside fence
        f"<reply>留证模板：\n```text\n对方原话：换个角度想我也有苦衷\n    [rec_{sample_id}]</reply>",
    ]
    idx = 0

    class _EdgeFenceGateway:
        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            nonlocal idx
            raw = replies[idx]
            idx += 1
            for i in range(0, len(raw), 5):
                yield raw[i : i + 5]

    db_container.extras["model_gateway"] = _EdgeFenceGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r3_fences@example.com")
        silver = await _create_person(client, headers, name_cn="甩锅领导", tier="silver")
        sid = silver["id"]

        def _collect_deltas(sse_body: str) -> str:
            return "".join(
                json.loads(line[6:])["text"]
                for block in sse_body.split("\n\n")
                if "event: delta" in block
                for line in block.splitlines()
                if line.startswith("data: ")
            )

        s1 = await client.post(
            f"/v1/persons/{sid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "给我四反引号嵌套示例"},
        )
        assert s1.status_code == 200
        text1 = _collect_deltas(s1.text)
        assert f"    [rec_{sample_id}]" in text1

        s2 = await client.post(
            f"/v1/persons/{sid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "给我对方原话留证模板"},
        )
        assert s2.status_code == 200
        text2 = _collect_deltas(s2.text)
        assert "对方原话：换个角度想我也有苦衷" in text2
        assert f"    [rec_{sample_id}]" in text2


async def test_r3_05_multiperson_export_splits_summary_leak_and_fact_lunar_toggle_stale_fields(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    email = "r3_multisplit@example.com"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, email)

        keep_p = await _create_person(client, headers, name_cn="林溪", tier="gold")
        del_p = await _create_person(client, headers, name_cn="沈前任", tier="silver")
        keep_id = keep_p["id"]
        del_id = del_p["id"]

        # 1. Single global capture mentioning BOTH 林溪 and 沈前任, followed by soft-deleting 沈前任
        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "global",
                "text": "林溪喜欢茉莉花茶，而且沈前任对芒果严重过敏SecretDeletedSplit",
            },
        )
        assert cap.status_code == 201
        assert len(cap.json()["splits"]) == 2

        await client.delete(
            f"/v1/persons/{del_id}",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        # 2. Export -> Lin Xi's data survives, while deleted 沈前任's split is scrubbed from captures[].splitsSummary
        reauth = await _get_reauth_token(client, email=email, headers=headers)
        exp = await client.post(
            "/v1/me/exports",
            headers={**headers, "X-Reauth-Token": reauth, "Idempotency-Key": str(uuid4())},
            json={"asyncMode": False},
        )
        assert exp.status_code == 201
        dl = await client.get(exp.json()["downloadPath"], headers=headers)
        assert dl.status_code == 200

        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            data_doc = json.loads(zf.read("data.json").decode("utf-8"))

        assert any(m["personId"] == keep_id for m in data_doc["materials"])
        for c_item in data_doc["captures"]:
            summary = c_item.get("splitsSummary") or {}
            for s in summary.get("splits") or []:
                assert s.get("personId") != del_id
                assert "SecretDeletedSplit" not in json.dumps(s, ensure_ascii=False)

        # 3. Lunar fact toggled to solar via PATCH /facts/{id} (`isLunar=False`) clears lunarMonth/lunarDay
        lunar_f = await client.post(
            f"/v1/persons/{keep_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "林溪生日",
                "isLunar": True,
                "lunarMonth": 8,
                "lunarDay": 15,
            },
        )
        assert lunar_f.status_code == 201
        fid = lunar_f.json()["id"]

        solar_patch = await client.patch(
            f"/v1/facts/{fid}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"isLunar": False, "triggerDate": "2026-10-01"},
        )
        assert solar_patch.status_code == 200
        assert solar_patch.json()["lunarMonth"] is None
        assert solar_patch.json()["lunarDay"] is None
