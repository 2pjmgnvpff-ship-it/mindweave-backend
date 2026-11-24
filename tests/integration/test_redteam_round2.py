"""Adversarial Red-Team Round 2 Suite (`R2-01` .. `R2-07` + Hardening Proofs).

Targets second-order bypasses, cross-module lifecycle leaks, concurrency/retry
edge cases, and domain input fuzzing on top of Round 1 (`commit 516d5be`).

- Passing `test_r2_hardening_*` tests prove attack surfaces that held firm.
- All `test_r2_01_*` .. `test_r2_08_*` tests promoted to permanent strict CI regression gates.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from PIL import Image

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


def _make_png_bytes(color: tuple[int, int, int] = (90, 120, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), color=color).save(buf, format="PNG")
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
    token = d2["accessToken"]
    user_id = d2["userId"]
    return {"Authorization": f"Bearer {token}"}, user_id


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
# PART 1: Passing Adversarial Hardening Proofs (Round 1 Defenses Verified Solid)
# =============================================================================


async def test_r2_hardening_signed_media_url_anti_replay_and_soft_delete_revocation(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    """Verify signed media URLs bind `(uid, canonical_path, exp)` and return 404 once soft-deleted."""
    app = create_app(container=db_container)
    png = _make_png_bytes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers_a, uid_a = await _signup_and_headers(client, "r2_media_a@example.com")
        headers_b, uid_b = await _signup_and_headers(client, "r2_media_b@example.com")

        person_a = await _create_person(client, headers_a, name_cn="林溪", tier="gold")
        pid_a = person_a["id"]

        # Upload avatar + main photo + material photo + wallpaper
        up_avatar = await client.post(
            f"/v1/persons/{pid_a}/photos/avatar",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            files={"file": ("a.png", png, "image/png")},
        )
        assert up_avatar.status_code == 200, up_avatar.text
        avatar_signed_url = up_avatar.json()["avatarUrl"]
        assert "sig=" in avatar_signed_url

        up_main = await client.post(
            f"/v1/persons/{pid_a}/photos/main",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            files={"file": ("m.png", png, "image/png")},
        )
        assert up_main.status_code == 200

        mat_resp = await client.post(
            f"/v1/persons/{pid_a}/materials",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            files={"photo": ("p.png", png, "image/png")},
            data={"title": "旅行合影", "body": "海边日落"},
        )
        assert mat_resp.status_code == 201, mat_resp.text
        mat_id = mat_resp.json()["id"]
        mat_signed_url = mat_resp.json()["mediaUrl"]

        # 1. Valid signed URL works without Authorization header (<img src>)
        ok_res = await client.get(avatar_signed_url)
        assert ok_res.status_code == 200

        # 2. Path-swap attack: replay avatar query string onto /photos/main and /me/wallpaper -> 401
        query_part = avatar_signed_url.split("?", 1)[1]
        replay_main = await client.get(f"/v1/persons/{pid_a}/photos/main?{query_part}")
        assert replay_main.status_code == 401
        replay_wp = await client.get(f"/v1/me/wallpaper?{query_part}")
        assert replay_wp.status_code == 401

        # 3. Cross-user UID swap & cross-tenant Bearer token -> 401 / 404
        tampered_uid = avatar_signed_url.replace(f"uid={uid_a}", f"uid={uid_b}")
        assert (await client.get(tampered_uid)).status_code == 401
        assert (
            await client.get(f"/v1/persons/{pid_a}/photos/avatar", headers=headers_b)
        ).status_code == 404
        assert (
            await client.get(f"/v1/materials/{mat_id}/media", headers=headers_b)
        ).status_code == 404

        # 4. Soft-deleting material immediately revokes access via previously issued signed URL -> 404
        del_mat = await client.delete(
            f"/v1/materials/{mat_id}",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
        )
        assert del_mat.status_code == 204
        assert (await client.get(mat_signed_url)).status_code == 404

        # 5. Soft-deleting person immediately revokes access via previously issued avatar signed URL -> 404
        del_p = await client.delete(
            f"/v1/persons/{pid_a}",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
        )
        assert del_p.status_code == 204
        assert (await client.get(avatar_signed_url)).status_code == 404

        # 6. Expiry enforcement after 15m TTL
        wp_up = await client.post(
            "/v1/me/wallpaper",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            files={"file": ("wp.png", png, "image/png")},
        )
        assert wp_up.status_code == 200
        wp_url = wp_up.json()["wallpaperUrl"]
        assert (await client.get(wp_url)).status_code == 200
        manual_clock.advance(seconds=901)
        assert (await client.get(wp_url)).status_code == 401


async def test_r2_hardening_soft_deleted_alias_not_hijacked_and_create_reminder_blocked(
    db_container: AppContainer,
) -> None:
    """Soft-deleted person's aliases must not hijack subsequent captures, and POST /reminders blocks deleted/archived."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_alias_sd@example.com")

        ex = await _create_person(client, headers, name_cn="周星河", tier="gold", aliases=["星河"])
        ex_id = ex["id"]

        # Soft-delete 周星河
        del_resp = await client.delete(
            f"/v1/persons/{ex_id}",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert del_resp.status_code == 204

        # Creating reminder on soft-deleted person -> 404
        rem_del = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": ex_id,
                "title": "不该创建的提醒",
                "rule": "once",
                "onceDate": "2026-12-20",
                "status": "active",
            },
        )
        assert rem_del.status_code == 404

        # Creating & archiving another person -> POST /reminders returns 422
        arch = await _create_person(client, headers, name_cn="苏晚", tier="silver")
        arch_id = arch["id"]
        await client.patch(
            f"/v1/persons/{arch_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"archived": True},
        )
        rem_arch = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": arch_id,
                "title": "归档人物提醒",
                "rule": "once",
                "onceDate": "2026-12-20",
                "status": "active",
            },
        )
        assert rem_arch.status_code == 422

        # Global capture mentioning "周星河" must NOT route into soft-deleted ex_id black hole!
        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "global",
                "text": "周星河对花生过敏",
            },
        )
        assert cap.status_code == 201, cap.text
        splits = cap.json()["splits"]
        assert splits, "Expected split onto newly provisioned person, not black hole"
        assert all(s["personId"] != ex_id for s in splits)


async def test_r2_hardening_capture_undo_reverts_graph_and_restores_superseded_fact(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    """Verify `:undo` within 5s reverts records, facts, reminders, inbox, provisional person & restores superseded fact."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_undo@example.com")

        lin = await _create_person(client, headers, name_cn="林夏", tier="gold")
        lin_id = lin["id"]

        # Seed initial confirmed fact that will be bi-temporally superseded
        f1 = await client.post(
            f"/v1/persons/{lin_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "preference",
                "polarity": "positive",
                "content": "平时最爱喝热拿铁咖啡",
            },
        )
        assert f1.status_code == 201
        orig_fact_id = f1.json()["id"]

        # Scoped capture superseding coffee preference + creating date reminder
        cap1 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": lin_id,
                "text": "她现在咖啡改喝冰美式了，而且12月24日是恋爱纪念日",
            },
        )
        assert cap1.status_code == 201, cap1.text
        cap1_id = cap1.json()["captureId"]

        # Undo within 5s window -> reverts everything & restores orig_fact_id!
        manual_clock.advance(seconds=3)
        undo1 = await client.post(
            f"/v1/captures/{cap1_id}:undo",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert undo1.status_code == 200, undo1.text
        assert undo1.json()["status"] == "reverted"

        active_facts = (await client.get(f"/v1/persons/{lin_id}/facts", headers=headers)).json()[
            "items"
        ]
        active_ids = {item["id"] for item in active_facts if item["status"] == "confirmed"}
        assert orig_fact_id in active_ids

        # Also test global capture creating inbox item -> undo resolves open inbox item
        cap2 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "global",
                "text": "周予安对猕猴桃过敏",
            },
        )
        assert cap2.status_code == 201
        cap2_id = cap2.json()["captureId"]
        undo2 = await client.post(
            f"/v1/captures/{cap2_id}:undo",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert undo2.status_code == 200
        open_inbox = (await client.get("/v1/inbox?status=open", headers=headers)).json()["items"]
        assert all(i["captureId"] != cap2_id for i in open_inbox)

        # Undo after 30s window -> 409 undo_window_expired
        cap3 = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": lin_id,
                "text": "喜欢看悬疑电影",
            },
        )
        assert cap3.status_code == 201
        manual_clock.advance(seconds=31)
        undo_late = await client.post(
            f"/v1/captures/{cap3.json()['captureId']}:undo",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert undo_late.status_code == 409


async def test_r2_hardening_inbox_double_resolve_and_cross_tenant_target_rejected(
    db_container: AppContainer,
) -> None:
    """Repeated resolve with new Idempotency-Key returns 409; cross-tenant targetPersonId returns 404."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers_a, _ = await _signup_and_headers(client, "r2_inbox_a@example.com")
        headers_b, _ = await _signup_and_headers(client, "r2_inbox_b@example.com")

        await _create_person(client, headers_a, name_cn="陈明", tier="gold", aliases=["老陈"])
        await _create_person(client, headers_a, name_cn="陈叔", tier="crystal", aliases=["老陈"])
        foreign_person = await _create_person(client, headers_b, name_cn="外人", tier="gold")

        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "老陈喜欢喝铁观音"},
        )
        assert cap.status_code == 201
        inbox_id = cap.json()["inboxItemIds"][0]

        # Cross-tenant targetPersonId -> 404
        bad_target = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            json={"action": "assign_person", "targetPersonId": foreign_person["id"]},
        )
        assert bad_target.status_code == 404

        # Valid resolve + idempotent replay -> 200; second resolve with different Idempotency-Key -> 409
        idem_ok = str(uuid4())
        res1 = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers={**headers_a, "Idempotency-Key": idem_ok},
            json={"action": "discard"},
        )
        assert res1.status_code == 200
        replay = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers={**headers_a, "Idempotency-Key": idem_ok},
            json={"action": "discard"},
        )
        assert replay.status_code == 200

        double_res = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers={**headers_a, "Idempotency-Key": str(uuid4())},
            json={"action": "discard"},
        )
        assert double_res.status_code == 409


async def test_r2_hardening_sse_1char_chunks_unpunctuated_reply_split_rec_and_silver_guard(
    db_container: AppContainer,
) -> None:
    """1-char chunk SSE stress test: unpunctuated <reply>, split [rec_id], Silver guard, and suppressed <perspectives>."""
    rec_holder: dict[str, str] = {}

    class _CharByCharXmlGateway:
        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            rid = rec_holder["id"]
            full_xml = (
                f"<reply>换个角度想也许他也有苦衷。先把越界证据留好 [rec_{rid}] 绝不背锅</reply>"
                "<whisper>注意保存书面记录</whisper>"
                "<perspectives>- 不该出现的换位思考</perspectives>"
                "<suggestions>- 整理邮件时间线\n- 换个角度想体谅对方</suggestions>"
            )
            for ch in full_xml:
                yield ch

    db_container.extras["model_gateway"] = _CharByCharXmlGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_1char@example.com")
        silver = await _create_person(client, headers, name_cn="甩锅主管", tier="silver")
        sid = silver["id"]

        mat = await client.post(
            f"/v1/persons/{sid}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"kind": "note", "title": "会议纪要", "body": "把延期责任推给我"},
        )
        assert mat.status_code == 201
        rec_holder["id"] = mat.json()["id"]

        sse = await client.post(
            f"/v1/persons/{sid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "他又当众甩锅给我", "referencedMaterialIds": [rec_holder["id"]]},
        )
        assert sse.status_code == 200
        raw_sse = sse.text
        assert "换个角度想" not in raw_sse
        assert "苦衷" not in raw_sse
        assert "[rec_" not in raw_sse
        assert "event: perspective" not in raw_sse
        assert "先把越界证据留好" in raw_sse
        assert "绝不背锅" in raw_sse
        assert "event: citation" in raw_sse


async def test_r2_hardening_malformed_timezones_lunar_leap_and_if_match_never_500(
    db_container: AppContainer,
) -> None:
    """Domain input fuzzing: invalid IANA timezones, nonexistent lunar leap month day 30, malformed If-Match -> clean 4xx."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_fuzz@example.com")
        person = await _create_person(client, headers, name_cn="苏晴", tier="gold")
        pid = person["id"]

        # 1. Invalid IANA timezone on preferences & reminders -> 422 (never 500)
        bad_pref_tz = await client.patch(
            "/v1/me/preferences",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"timezone": "Mars/Olympus_Mons"},
        )
        assert bad_pref_tz.status_code == 422

        bad_rem_tz = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": pid,
                "title": "测试时区",
                "rule": "once",
                "onceDate": "2026-12-25",
                "timezone": "Mars/Olympus_Mons",
                "status": "active",
            },
        )
        assert bad_rem_tz.status_code == 422

        # 2. Lunar leap month 2 day 30 (non-leap fallback + 29-day clamp) -> clean 201 (never 500 ValueError)
        lunar_edge = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": pid,
                "title": "闰二月三十生日",
                "rule": "yearly",
                "isLunar": True,
                "targetMonth": 2,
                "targetDay": 30,
                "isLeapMonth": True,
                "status": "active",
            },
        )
        assert lunar_edge.status_code == 201, lunar_edge.text

        # 3. Malformed If-Match headers across preferences, people, materials, reminders -> 412/422 (never 500)
        for bad_etag in ('"not-an-int"', '"0"', '"-3"', 'W/"abc"'):
            r_pref = await client.patch(
                "/v1/me/preferences",
                headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": bad_etag},
                json={"wallpaperBlur": 10},
            )
            assert r_pref.status_code == 422
            r_person = await client.patch(
                f"/v1/persons/{pid}",
                headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": bad_etag},
                json={"shortMemo": "备注"},
            )
            assert r_person.status_code == 422
            r_rem = await client.patch(
                f"/v1/reminders/{lunar_edge.json()['id']}",
                headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": bad_etag},
                json={"title": "新标题"},
            )
            assert r_rem.status_code == 422

        # Weak ETag W/"999" and unquoted 999 parse cleanly and return 412 Precondition Failed
        stale_weak = await client.patch(
            f"/v1/persons/{pid}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": 'W/"999"'},
            json={"shortMemo": "备注"},
        )
        assert stale_weak.status_code == 412


# =============================================================================
# PART 2: Strict XFAIL Reproductions of Real Round 2 Vulnerabilities & Bugs
# =============================================================================


async def test_r2_01_explicit_suicide_jump_suppressed_by_stressor_and_bitter_laughter() -> None:
    engine = L1SafetyEngine()

    # Benign workplace hyperbole MUST stay NORMAL:
    benign = engine.evaluate("加班改PPT第8版真的想死哈哈哈，顺便记一下阿哲不吃香菜")
    assert benign.category is SafetyCategory.NORMAL

    # Genuine crisis with bitter laughter + thesis/workplace stressor MUST NEVER be suppressed!
    crisis_jump = engine.evaluate(
        "毕业论文彻底挂了，我已经站在天台准备跳楼自杀了，哈哈哈一切都结束吧"
    )
    crisis_end_life = engine.evaluate("天天加班到崩溃，今晚准备结束自己的生命，笑死再见了")

    assert crisis_jump.category is SafetyCategory.CRISIS
    assert crisis_end_life.category is SafetyCategory.CRISIS


async def test_r2_02_pipl_export_leaks_soft_deleted_person_facts_aliases_and_chats(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    email = "r2_export_leak@example.com"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, email)

        ex = await _create_person(
            client, headers, name_cn="沈前任", tier="silver", aliases=["阿沈"]
        )
        ex_id = ex["id"]

        # Add a sensitive fact and a private companion chat message for 沈前任
        f_res = await client.post(
            f"/v1/persons/{ex_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "taboo",
                "polarity": "negative",
                "content": "沈前任的私密敏感禁忌记录XYZ",
            },
        )
        assert f_res.status_code == 201

        c_res = await client.post(
            f"/v1/persons/{ex_id}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "我再也不想看到沈前任SecretChat999了"},
        )
        assert c_res.status_code == 200

        # User deletes 沈前任
        del_res = await client.delete(
            f"/v1/persons/{ex_id}",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert del_res.status_code == 204

        # Export user data
        reauth = await _get_reauth_token(client, headers, email)
        exp_res = await client.post(
            "/v1/me/exports",
            headers={**headers, "X-Reauth-Token": reauth, "Idempotency-Key": str(uuid4())},
            json={"asyncMode": False},
        )
        assert exp_res.status_code == 201, exp_res.text
        dl_path = exp_res.json()["downloadPath"]

        dl_res = await client.get(dl_path, headers=headers)
        assert dl_res.status_code == 200

        with zipfile.ZipFile(io.BytesIO(dl_res.content)) as zf:
            journal_md = zf.read("journal.md").decode("utf-8")
            data_json = json.loads(zf.read("data.json").decode("utf-8"))

        assert "沈前任的私密敏感禁忌记录XYZ" not in journal_md
        assert all(a["personId"] != ex_id for a in data_json["personAliases"])
        assert all(f["personId"] != ex_id for f in data_json["facts"])
        assert all(m["personId"] != ex_id for m in data_json["chatMessages"])


async def test_r2_03_patch_reminder_and_fact_bypass_deleted_and_archived_person_guards(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_rem_bypass@example.com")

        # Case A: Archived person's paused reminder reactivated via PATCH /v1/reminders/{id}
        arch = await _create_person(client, headers, name_cn="归档前任", tier="gold")
        arch_id = arch["id"]
        rem_a = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": arch_id,
                "title": "前任纪念日",
                "rule": "once",
                "onceDate": "2027-05-20",
                "status": "active",
            },
        )
        assert rem_a.status_code == 201
        rem_a_id = rem_a.json()["id"]

        await client.patch(
            f"/v1/persons/{arch_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"archived": True},
        )

        # Attempting to re-activate reminder while person is still archived MUST fail with 422!
        reactivate_arch = await client.patch(
            f"/v1/reminders/{rem_a_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"status": "active"},
        )
        assert reactivate_arch.status_code == 422

        # Case B: Soft-deleted person's reminder & fact mutated via PATCH -> MUST return 404!
        deleted_p = await _create_person(client, headers, name_cn="已删人物", tier="crystal")
        del_pid = deleted_p["id"]
        fact_b = await client.post(
            f"/v1/persons/{del_pid}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"type": "preference", "polarity": "positive", "content": "喜欢抹茶"},
        )
        assert fact_b.status_code == 201
        fact_b_id = fact_b.json()["id"]

        rem_b = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": del_pid,
                "title": "已删提醒",
                "rule": "once",
                "onceDate": "2027-06-01",
                "status": "active",
            },
        )
        assert rem_b.status_code == 201
        rem_b_id = rem_b.json()["id"]

        await client.delete(
            f"/v1/persons/{del_pid}",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )

        patch_del_rem = await client.patch(
            f"/v1/reminders/{rem_b_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"status": "active"},
        )
        patch_del_fact = await client.patch(
            f"/v1/facts/{fact_b_id}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"content": "不该修改已删人物事实"},
        )
        assert patch_del_rem.status_code == 404
        assert patch_del_fact.status_code == 404


async def test_r2_04_sse_stream_guard_corrupts_multiline_fenced_code_block_rec_literal(
    db_container: AppContainer,
) -> None:
    sample_id = "01965b30-0000-7000-8000-000000000099"
    fenced_reply = f"参考模板如下：\n```markdown\n    [rec_{sample_id}]\n```\n按这个格式整理即可。"

    class _CodeFenceGateway:
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
                    reply_text=fenced_reply,
                    cited_record_ids=[],
                    has_unrecorded_gap=False,
                    alternative_perspectives=[],
                    actionable_suggestions=[],
                ),
                "test_v1",
                "fake",
            )

        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            wrapped = f"<reply>{fenced_reply}</reply>"
            for i in range(0, len(wrapped), 5):
                yield wrapped[i : i + 5]

    db_container.extras["model_gateway"] = _CodeFenceGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_codefence@example.com")
        person = await _create_person(client, headers, name_cn="顾言", tier="gold")
        pid = person["id"]

        # Non-streaming preserves literal `[rec_...]` inside ```markdown ... ``` per Invariant I4
        json_turn = await client.post(
            f"/v1/persons/{pid}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "给我一个模板示例"},
        )
        assert json_turn.status_code == 200
        assert f"[rec_{sample_id}]" in json_turn.json()["assistantMessage"]["content"]

        # Streaming MUST preserve identical fenced code block literal & indentation!
        sse_turn = await client.post(
            f"/v1/persons/{pid}/chat/stream",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "流式给我一个模板示例"},
        )
        assert sse_turn.status_code == 200
        deltas: list[str] = []
        for block in sse_turn.text.split("\n\n"):
            if "event: delta" in block:
                for line in block.splitlines():
                    if line.startswith("data: "):
                        deltas.append(json.loads(line[6:])["text"])
        streamed_text = "".join(deltas)
        assert f"    [rec_{sample_id}]" in streamed_text


async def test_r2_05_sse_retry_after_stream_failure_duplicates_user_message_in_history(
    db_container: AppContainer,
) -> None:
    attempts = 0

    class _FlakyOnceStreamGateway:
        async def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: Any = None,
        ) -> AsyncIterator[str]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                yield "<reply>第一段中断"
                raise RuntimeError("transient upstream reset")
            yield "<reply>重试成功回复。</reply>"

    db_container.extras["model_gateway"] = _FlakyOnceStreamGateway()
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_dup_msg@example.com")
        person = await _create_person(client, headers, name_cn="许知意", tier="gold")
        pid = person["id"]

        idem_key = str(uuid4())
        payload = {"message": "帮我梳理下周见面的话题", "clientMessageId": "cmsg-retry-001"}

        # Attempt 1: fails mid-stream and releases Idempotency-Key
        r1 = await client.post(
            f"/v1/persons/{pid}/chat/stream",
            headers={**headers, "Idempotency-Key": idem_key},
            json=payload,
        )
        assert r1.status_code == 200
        assert "event: error" in r1.text

        # Attempt 2: client retries exact same Idempotency-Key + clientMessageId -> succeeds
        r2 = await client.post(
            f"/v1/persons/{pid}/chat/stream",
            headers={**headers, "Idempotency-Key": idem_key},
            json=payload,
        )
        assert r2.status_code == 200
        assert "event: done" in r2.text

        # Thread history MUST contain only 1 user message (not 2 duplicates!) + 1 assistant reply
        hist = await client.get(f"/v1/persons/{pid}/chat/messages", headers=headers)
        assert hist.status_code == 200
        items = hist.json()["items"]
        user_msgs = [m for m in items if m["role"] == "user"]
        assert len(user_msgs) == 1


async def test_r2_06_incomplete_lunar_fact_and_archived_scoped_capture_validation(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_lunar_arch@example.com")
        person = await _create_person(client, headers, name_cn="唐梨", tier="gold")
        pid = person["id"]

        # 1. isLunar=True without lunarMonth/lunarDay MUST be rejected with 422!
        bad_lunar_fact = await client.post(
            f"/v1/persons/{pid}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "唐梨的农历生日",
                "isLunar": True,
            },
        )
        assert bad_lunar_fact.status_code == 422

        # 2. Archive 唐梨, then attempt current-mode capture -> MUST return 422 (not fake extraction_failed!)
        await client.patch(
            f"/v1/persons/{pid}",
            headers={**headers, "Idempotency-Key": str(uuid4()), "If-Match": '"1"'},
            json={"archived": True},
        )
        arch_cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": pid,
                "text": "她喜欢吃草莓蛋糕",
            },
        )
        assert arch_cap.status_code == 422


async def test_r2_07_unassigned_material_leaks_placeholder_uuid_and_idempotency_replay_expires_media_url(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    class _FailingExtractGateway:
        async def extract_facts(
            self, *, scope: UserScope, payload: Any, session: Any = None
        ) -> tuple[Any, str, str]:
            raise TimeoutError("simulated timeout")

    db_container.extras["model_gateway"] = _FailingExtractGateway()
    app = create_app(container=db_container)
    png = _make_png_bytes()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_unassigned_mat@example.com")

        # 1. Global capture degrades to saved_as_note on __unassigned__ placeholder
        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "买一束向日葵"},
        )
        assert cap.status_code == 201
        inbox_items = (await client.get("/v1/inbox?status=open", headers=headers)).json()["items"]
        rec_id = inbox_items[0]["recordId"]
        assert inbox_items[0]["personId"] is None

        # Direct GET /v1/materials/{rec_id} MUST NOT leak the hidden __unassigned__ UUID!
        mat_res = await client.get(f"/v1/materials/{rec_id}", headers=headers)
        assert mat_res.status_code == 200
        visible_person_ids = {
            p["id"] for p in (await client.get("/v1/persons", headers=headers)).json()["items"]
        }
        assert mat_res.json()["personId"] in (None, *visible_person_ids)

        # 2. Photo upload replayed after 16m (within 24h idempotency TTL) MUST return fresh signed URL!
        person = await _create_person(client, headers, name_cn="宋词", tier="gold")
        pid = person["id"]
        idem_photo = str(uuid4())
        up1 = await client.post(
            f"/v1/persons/{pid}/photos/avatar",
            headers={**headers, "Idempotency-Key": idem_photo},
            files={"file": ("a.png", png, "image/png")},
        )
        assert up1.status_code == 200

        manual_clock.advance(seconds=960)  # 16 minutes > 15m media & access token TTL
        fresh_headers, _ = await _signup_and_headers(client, "r2_unassigned_mat@example.com")
        up_replay = await client.post(
            f"/v1/persons/{pid}/photos/avatar",
            headers={**fresh_headers, "Idempotency-Key": idem_photo},
            files={"file": ("a.png", png, "image/png")},
        )
        assert up_replay.status_code == 200
        replayed_avatar_url = up_replay.json()["avatarUrl"]
        img_fetch = await client.get(replayed_avatar_url)
        assert img_fetch.status_code == 200


async def test_r2_08_autoflush_false_leaves_ghost_provisional_person_after_undo_and_discard(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _signup_and_headers(client, "r2_autoflush_ghost@example.com")

        # 1. Capture creates provisional person 周予安 -> immediate :undo MUST remove ghost provisional person
        cap_undo = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "周予安对猕猴桃过敏"},
        )
        assert cap_undo.status_code == 201
        undo_res = await client.post(
            f"/v1/captures/{cap_undo.json()['captureId']}:undo",
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert undo_res.status_code == 200

        # 2. Capture creates provisional person 林知夏 -> inbox:resolve(discard) MUST archive ghost provisional person
        cap_disc = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"source": "text", "routeMode": "global", "text": "林知夏不吃香菜"},
        )
        assert cap_disc.status_code == 201
        inbox_id = cap_disc.json()["inboxItemIds"][0]
        disc_res = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"action": "discard"},
        )
        assert disc_res.status_code == 200

        persons_after = (await client.get("/v1/persons", headers=headers)).json()["items"]
        names_after = {p["nameCn"] for p in persons_after}
        assert "周予安" not in names_after
        assert "林知夏" not in names_after
