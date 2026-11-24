"""Adversarial Red-Team Reproduction Suite (Principal Auditor).

Each test in this module reproduces a concrete security, data-isolation, state-machine,
or V10 contract defect discovered during the full-codebase red-team audit.

- Run `uv run pytest tests/integration/test_redteam_adversarial.py --runxfail -v`
  to observe the raw failing assertions proving each defect.
- Promoted to permanent strict CI regression tests after Round 1 remediation.
"""

from __future__ import annotations

import asyncio
import configparser
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select

from mindweave.ai.schemas.contracts import (
    ExtractedFactItem,
    ExtractRecordInput,
    ExtractRecordOutput,
    SafetyClassifyOutput,
)
from mindweave.ai.validators.citations import validate_citations
from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import FactRow, IdempotencyKeyRow
from mindweave.main import create_app
from mindweave.modules.companion.application.dto import SendChatMessageRequest
from mindweave.modules.companion.application.service import CompanionService
from mindweave.modules.companion.application.writeback import writeback_facts_from_chat_message
from mindweave.modules.reminders.application.service import scan_and_deliver_due_reminders
from mindweave.modules.reminders.infra.push import PushDeliveryReceipt, PushMessage
from mindweave.modules.safety.domain.l1_rules import L1SafetyEngine
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    Polarity,
    ReminderStatus,
    SafetyCategory,
    SourceSpeaker,
    TimeStatus,
)
from mindweave.shared.scopes import PersonScope, UserScope


async def _signup_and_user(
    client: AsyncClient, email: str = "redteam@example.com"
) -> tuple[dict[str, str], UUID]:
    otp_req = await client.post("/v1/auth/email/otp:request", json={"email": email})
    assert otp_req.status_code == 200
    body = otp_req.json()
    verify = await client.post(
        "/v1/auth/email/otp:verify",
        json={"challengeId": body["challengeId"], "email": email, "code": body["devOtpCode"]},
    )
    assert verify.status_code == 200
    vjson = verify.json()
    return {"Authorization": f"Bearer {vjson['accessToken']}"}, UUID(vjson["userId"])


async def _signup_and_headers(
    client: AsyncClient, email: str = "redteam@example.com"
) -> dict[str, str]:
    headers, _ = await _signup_and_user(client, email)
    return headers


async def _step_up_reauth(
    client: AsyncClient, headers: dict[str, str], email: str = "redteam@example.com"
) -> str:
    otp_req = await client.post(
        "/v1/auth/email/otp:request",
        json={"email": email, "purpose": "reauth"},
    )
    assert otp_req.status_code == 200
    body = otp_req.json()
    verify = await client.post(
        "/v1/auth/email/otp:verify",
        headers=headers,
        json={
            "challengeId": body["challengeId"],
            "email": email,
            "code": body["devOtpCode"],
        },
    )
    assert verify.status_code == 200
    return str(verify.json()["reauthToken"])


@pytest.mark.asyncio
async def test_rt01_phantom_unassigned_person_leaks_into_persons_list_and_export(
    db_container: AppContainer,
) -> None:
    class _FailingGateway:
        async def extract_record(self, **kwargs: Any) -> Any:
            raise TimeoutError("simulated LLM outage")

    db_container.extras["model_gateway"] = _FailingGateway()
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _signup_and_headers(client, "rt01@example.com")

        # Pronoun mention in global mode with zero known people -> creates '__unassigned__' ('待归档') placeholder
        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(new_id())},
            data={"routeMode": "global", "text": "她不能吃香菜"},
        )
        assert cap.status_code in (200, 201, 202)

        boot = (await client.get("/v1/bootstrap", headers=headers)).json()
        boot_names = [p.get("nameCn") for p in boot["people"].values()]
        plist = (await client.get("/v1/persons", headers=headers)).json()
        person_slugs = [p.get("slug") for p in plist["items"]]

        # '__unassigned__' ('待归档') internal bucket must NEVER appear in GET /v1/bootstrap or GET /v1/persons!
        assert "待归档" not in boot_names, (
            f"Internal placeholder '待归档' leaked into GET /v1/bootstrap: {boot_names}"
        )
        assert "__unassigned__" not in person_slugs, (
            f"Internal placeholder '__unassigned__' leaked into GET /v1/persons: {person_slugs}"
        )


@pytest.mark.asyncio
async def test_rt02_held_venting_and_ambiguous_records_leak_onto_scrapbook(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _signup_and_headers(client, "rt02@example.com")

        p_gold = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "林溪", "tier": "gold"},
        )
        linxi_id = p_gold.json()["id"]

        # Venting capture on Gold person -> GatePolicy marks RecordRow.review_state = 'held'
        cap = await client.post(
            "/v1/captures?sync=true",
            headers={**headers, "Idempotency-Key": str(new_id())},
            data={
                "routeMode": "current",
                "activePersonId": linxi_id,
                "text": "气死我了林溪每次约会都迟到，再也不想理她了",
            },
        )
        assert cap.status_code in (200, 201, 202)

        # Held venting record MUST NOT appear on active scrapbook bootstrap before 72h hold resolution!
        boot = (await client.get("/v1/bootstrap", headers=headers)).json()
        linxi_mats = boot["people"][linxi_id]["materials"]
        assert linxi_mats == [], (
            f"Held venting record leaked into bootstrap materials: {linxi_mats}"
        )


@pytest.mark.asyncio
async def test_rt03_archived_person_still_fires_due_push_notifications(
    db_container: AppContainer,
    manual_clock: Any,
) -> None:
    class _SpyPushProvider:
        def __init__(self) -> None:
            self.sent_messages: list[PushMessage] = []

        async def send(self, message: PushMessage) -> PushDeliveryReceipt:
            self.sent_messages.append(message)
            return PushDeliveryReceipt(delivered=True, channel=message.platform.value)

    push_spy = _SpyPushProvider()
    db_container.extras["push_provider"] = push_spy
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _signup_and_headers(client, "rt03@example.com")

        # Register push device
        dev = await client.post(
            "/v1/devices",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"platform": "ios_apns", "pushToken": "apns-dev-token-rt03"},
        )
        assert dev.status_code == 201

        # Create person & active reminder due tomorrow
        person_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "前任", "tier": "gold"},
        )
        person_id = person_resp.json()["id"]

        target_day = (manual_clock.now() + timedelta(days=1)).date().isoformat()
        rem_resp = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={
                "personId": person_id,
                "title": "恋爱纪念日",
                "rule": "once",
                "onceDate": target_day,
                "leadDays": [0],
                "fireHour": 0,
                "fireMinute": 0,
                "timezone": "UTC",
                "status": "active",
            },
        )
        assert rem_resp.status_code == 201

        # User archives the ex-partner!
        arch_resp = await client.patch(
            f"/v1/persons/{person_id}",
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": str(new_id())},
            json={"archived": True},
        )
        assert arch_resp.status_code == 200

        # Advance clock past reminder fire time
        manual_clock.advance(days=2)
        scan_res = await scan_and_deliver_due_reminders(db_container, push_provider=push_spy)

        assert scan_res.pushed_messages == 0
        assert len(push_spy.sent_messages) == 0


@pytest.mark.asyncio
async def test_rt04_unauthenticated_export_download_and_plaintext_token_in_idempotency(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _signup_and_headers(client, "rt04@example.com")
        reauth = await _step_up_reauth(client, headers, "rt04@example.com")

        idem_key = str(new_id())
        exp = await client.post(
            "/v1/me/exports",
            headers={
                **headers,
                "X-Reauth-Token": reauth,
                "Idempotency-Key": idem_key,
            },
            json={"asyncMode": False},
        )
        assert exp.status_code == 201
        dl_path = exp.json()["downloadPath"]

        # Unauthenticated attacker who observed URL in proxy/browser history (NO Authorization header!)
        unauth_dl = await client.get(dl_path)
        assert unauth_dl.status_code in (401, 403), (
            f"Unauthenticated download succeeded with {unauth_dl.status_code} ({len(unauth_dl.content)} bytes)!"
        )


def test_rt05_safety_l1_whitespace_evasion_and_charcoal_crisis_gap() -> None:
    engine = L1SafetyEngine()
    spaced = engine.evaluate("我 想 自 杀，真的撑不下去了")
    charcoal = engine.evaluate("门窗都封好了，今晚准备烧炭走了")

    assert spaced.category == SafetyCategory.CRISIS
    assert charcoal.category == SafetyCategory.CRISIS


@pytest.mark.asyncio
async def test_rt06_chat_writeback_contradiction_and_blank_inbox_summary(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, "rt06@example.com")

        p_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "小陈", "tier": "gold"},
        )
        person_id = UUID(p_resp.json()["id"])

        # Seed existing confirmed preference: "喜欢吃香菜"
        f_resp = await client.post(
            f"/v1/persons/{person_id}/facts",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"type": "preference", "polarity": "positive", "content": "喜欢吃香菜"},
        )
        assert f_resp.status_code == 201

        # Send chat turn with a contradicting taboo + a promise
        turn = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"message": "小陈现在对香菜严重过敏不能吃香菜了，另外我答应她下周五陪她看展"},
        )
        assert turn.status_code == 200
        user_msg_id = UUID(turn.json()["userMessage"]["id"])

        class _StubExtractGateway:
            async def extract_record(
                self,
                *,
                scope: UserScope,
                payload: ExtractRecordInput,
                session: Any = None,
            ) -> tuple[ExtractRecordOutput, str, str]:
                out = ExtractRecordOutput(
                    facts=[
                        ExtractedFactItem(
                            person_mention="小陈",
                            person_id=person_id,
                            fact_type=FactType.TABOO,
                            polarity=Polarity.NEGATIVE,
                            time_status=TimeStatus.CURRENT,
                            source_speaker=SourceSpeaker.NARRATOR,
                            title="香菜忌口",
                            content="对香菜严重过敏，不能吃香菜",
                            confidence=0.95,
                        ),
                        ExtractedFactItem(
                            person_mention="小陈",
                            person_id=person_id,
                            fact_type=FactType.PROMISE,
                            polarity=Polarity.POSITIVE,
                            time_status=TimeStatus.CURRENT,
                            source_speaker=SourceSpeaker.USER_SELF,
                            title="陪看展",
                            content="答应下周五陪小陈看展",
                            trigger_date="2026-12-20",
                            confidence=0.95,
                        ),
                    ],
                )
                return out, "extract_record@1.1.0", "fake-test"

        wb_res = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=person_id),
            message_id=user_msg_id,
            model_gateway=_StubExtractGateway(),  # type: ignore[arg-type]
        )

        # 1. Inbox item summary must NOT be blank!
        inbox_resp = await client.get("/v1/inbox", headers=headers)
        assert inbox_resp.status_code == 200
        summaries = [item["summary"] for item in inbox_resp.json()["items"]]
        assert all(bool(s.strip()) for s in summaries), f"Blank inbox summary returned: {summaries}"

        # 2. Polarity flip on 香菜 MUST route to pending OR supersede old positive fact!
        async with db_container.db.user_session(UserScope(user_id=user_id)) as sess:
            active_confirmed = (
                await sess.scalars(
                    select(FactRow).where(
                        FactRow.user_id == user_id,
                        FactRow.person_id == person_id,
                        FactRow.status == FactStatus.CONFIRMED.value,
                        FactRow.invalid_at.is_(None),
                    )
                )
            ).all()
        assert len(active_confirmed) <= 1, (
            f"Both contradicting facts ('喜欢吃香菜' and '不能吃香菜') are active confirmed! wb={wb_res}"
        )


@pytest.mark.asyncio
async def test_rt07_lunar_promise_proposed_reminder_crashes_on_activation(
    db_container: AppContainer,
) -> None:
    from mindweave.modules.memory.infra.reminders_helper import upsert_reminder_for_fact

    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, "rt07@example.com")
        scope = UserScope(user_id=user_id)

        p_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "阿哲", "tier": "gold"},
        )
        person_id = UUID(p_resp.json()["id"])

        async with db_container.db.user_session(scope) as sess:
            dek = await db_container.load_user_dek(sess, scope)
            fid = new_id()
            now = db_container.clock.now()
            fact = FactRow(
                id=fid,
                user_id=user_id,
                person_id=person_id,
                record_id=None,
                capture_id=None,
                type=FactType.PROMISE.value,
                polarity=Polarity.POSITIVE.value,
                time_status=TimeStatus.CURRENT.value,
                source_speaker=SourceSpeaker.USER_SELF.value,
                content_enc=db_container.cipher.encrypt_str(
                    dek,
                    "答应中秋节陪阿哲赏月",
                    user_id=user_id,
                    table="facts",
                    column="content_enc",
                    row_id=fid,
                ),
                trigger_date=None,
                is_lunar=True,
                lunar_month=8,
                lunar_day=15,
                lunar_is_leap_month=False,
                status=FactStatus.CONFIRMED.value,
                created_by="user",
                valid_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
            sess.add(fact)
            await sess.flush()
            rem = await upsert_reminder_for_fact(
                sess,
                scope=scope,
                dek=dek,
                cipher=db_container.cipher,
                fact=fact,
                fact_content="答应中秋节陪阿哲赏月",
                desired_status=ReminderStatus.PROPOSED,
                now=now,
            )
            assert rem is not None
            rem_id = rem.id

        # Activating this proposed reminder via PATCH /v1/reminders/{id} crashes with 422!
        act = await client.patch(
            f"/v1/reminders/{rem_id}",
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": str(new_id())},
            json={"status": "active"},
        )
        assert act.status_code == 200, f"Activation failed: {act.status_code} {act.text}"


@pytest.mark.asyncio
async def test_rt08_sse_cancellation_wedges_idempotency_key_in_progress(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, "rt08@example.com")

        p_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "沈清秋", "tier": "gold"},
        )
        person_id = UUID(p_resp.json()["id"])

    class _SlowStreamGateway:
        async def classify_safety(
            self, *, scope: UserScope, text: str, session: Any = None
        ) -> SafetyClassifyOutput:
            return SafetyClassifyOutput(category=SafetyCategory.NORMAL, reason="ok")

        async def stream_companion_reply(
            self, *, scope: PersonScope, payload: Any, session: Any = None
        ) -> Any:
            yield "<reply>第一段，"
            await asyncio.sleep(10)
            yield "第二段</reply>"

    svc = CompanionService(db_container, model_gateway=_SlowStreamGateway())  # type: ignore[arg-type]
    scope = PersonScope(user_id=user_id, person_id=person_id)
    idem_key = "sse-cancel-idem-key"
    req = SendChatMessageRequest(message="周末去哪玩？")

    gen = await svc.prepare_sse_stream(
        scope=scope,
        message=req.message,
        idem_key=idem_key,
        idem_path=f"/v1/persons/{person_id}/chat/messages",
    )

    # Pull first delta, then simulate SSE client disconnect via CancelledError
    it = aiter(gen)
    first_chunk = await anext(it)
    assert "delta" in str(first_chunk)
    with pytest.raises(asyncio.CancelledError):
        await gen.athrow(asyncio.CancelledError())  # type: ignore[attr-defined]

    # Inspect IdempotencyKeyRow: must NOT remain wedged in 'in_progress'!
    async with db_container.db.user_session(UserScope(user_id=user_id)) as sess:
        rows = (
            await sess.scalars(
                select(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.user_id == user_id,
                    IdempotencyKeyRow.idem_key == idem_key,
                )
            )
        ).all()
    assert all(r.status != "in_progress" for r in rows), (
        f"IdempotencyKeyRow wedged in 'in_progress' after SSE disconnect: {[r.status for r in rows]}"
    )


@pytest.mark.asyncio
async def test_rt09_silver_sse_stream_bypasses_excusing_guardrail(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _signup_and_user(client, "rt09@example.com")

        p_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "林知夏", "tier": "silver"},
        )
        person_id = UUID(p_resp.json()["id"])

    class _PreachySilverGateway:
        async def classify_safety(
            self, *, scope: UserScope, text: str, session: Any = None
        ) -> SafetyClassifyOutput:
            return SafetyClassifyOutput(category=SafetyCategory.NORMAL, reason="ok")

        async def stream_companion_reply(
            self, *, scope: PersonScope, payload: Any, session: Any = None
        ) -> Any:
            yield "<reply>也许对方也有苦衷，换个角度想他也不是故意的。建议把对接文档发群里留痕。</reply>"

    svc = CompanionService(db_container, model_gateway=_PreachySilverGateway())  # type: ignore[arg-type]
    gen = await svc.prepare_sse_stream(
        scope=PersonScope(user_id=user_id, person_id=person_id),
        message="她周五下班又推活给我",
        idem_key=str(new_id()),
        idem_path=f"/v1/persons/{person_id}/chat/stream",
    )
    chunks = [str(chunk) async for chunk in gen]
    joined = "".join(chunks)
    assert "换个角度想" not in joined
    assert "也有苦衷" not in joined


def test_rt10_importlinter_missing_mindweave_ai_in_vendor_sdk_contract() -> None:
    valid_id = new_id()
    res = validate_citations(
        f"她不能吃海鲜 [重点：忌口 [rec_{valid_id}] 必看]。",
        declared_record_ids=(),
        allowed_record_ids={valid_id},
        strip_valid_tags=True,
    )
    assert f"rec_{valid_id}" not in res.cleaned_text

    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).resolve().parents[2] / ".importlinter")
    sec = cfg["importlinter:contract:vendor-sdk-isolated-to-ai-adapters"]
    sources = {line.strip() for line in sec["source_modules"].splitlines() if line.strip()}
    assert "mindweave.ai" in sources, (
        f"mindweave.ai missing from vendor-sdk-isolated-to-ai-adapters source_modules: {sources}"
    )


@pytest.mark.asyncio
async def test_rt11_reissued_email_otp_fails_to_invalidate_older_otp(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = (
            await client.post("/v1/auth/email/otp:request", json={"email": "rt11@example.com"})
        ).json()
        r2 = await client.post("/v1/auth/email/otp:request", json={"email": "rt11@example.com"})
        assert r2.status_code == 200

        # Older challenge1 MUST be invalidated once challenge2 was issued for the same email!
        v1 = await client.post(
            "/v1/auth/email/otp:verify",
            json={
                "challengeId": r1["challengeId"],
                "email": "rt11@example.com",
                "code": r1["devOtpCode"],
            },
        )
        assert v1.status_code == 401


@pytest.mark.asyncio
async def test_rt12_v10_bootstrap_missing_gleb_app_fields(
    db_container: AppContainer,
) -> None:
    import io

    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _signup_and_headers(client, "rt12@example.com")
        p_resp = await client.post(
            "/v1/persons",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"nameCn": "沈清秋", "tier": "gold"},
        )
        pid = p_resp.json()["id"]

        buf = io.BytesIO()
        Image.new("RGB", (16, 16), color=(200, 150, 100)).save(buf, format="JPEG")
        up = await client.post(
            f"/v1/persons/{pid}/photos/avatar",
            headers={**headers, "Idempotency-Key": str(new_id())},
            files={"file": ("avatar.jpg", buf.getvalue(), "image/jpeg")},
        )
        assert up.status_code == 200

        mat = await client.post(
            f"/v1/persons/{pid}/materials",
            headers={**headers, "Idempotency-Key": str(new_id())},
            json={"kind": "note", "title": "拿铁手札", "body": "半糖温热燕麦奶"},
        )
        assert mat.status_code == 201

        boot = (await client.get("/v1/bootstrap", headers=headers)).json()
        person_obj = boot["people"][pid]

        # gleb-app.mjs reads p.avatar, p.sealBg, p.tabColor, and mat.thumb directly!
        assert "avatar" in person_obj
        assert "sealBg" in person_obj
        assert "tabColor" in person_obj
        assert "thumb" in person_obj["materials"][0]
