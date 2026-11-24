"""P0 0.6 / D1 pencil drafts: capture splits, person page, glance, auto-ink, scoped alias."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.integration.test_capture_inbox_flow import (
    ProgrammableGateway,
    _auth,
    _build_test_app,
    _provision_user,
    _seed_person,
)

from mindweave.ai.gateway import ModelGateway
from mindweave.ai.schemas.contracts import ExtractedFactItem, ExtractRecordOutput
from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.orm import FactRow, InboxItemRow, ReminderRow
from mindweave.modules.bootstrap.api.router import router as bootstrap_router
from mindweave.modules.capture.api import router as capture_router
from mindweave.modules.memory.api import router as memory_router
from mindweave.modules.memory.application.service import MemoryService
from mindweave.modules.safety.application.service import SafetyService
from mindweave.shared.enums import FactType, Polarity, Tier


def _capture_body(
    text: str, person_id: UUID | None = None, route: str = "current"
) -> dict[str, Any]:
    body: dict[str, Any] = {"source": "text", "text": text, "routeMode": route}
    if person_id is not None:
        body["activePersonId"] = str(person_id)
    return body


async def _materials(
    client: AsyncClient, token: str, pid: UUID, *, held: bool = False
) -> list[Any]:
    suffix = "?includeHeld=true" if held else ""
    resp = await client.get(f"/v1/persons/{pid}/materials{suffix}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    items: list[Any] = resp.json()["items"]
    return items


@pytest.mark.asyncio
async def test_date_capture_is_pencil_with_active_reminder_and_glance_turns_it_to_ink(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi,
                    fact_type=FactType.DATE,
                    title="生日",
                    content="林溪5月12日生日",
                    trigger_date="2027-05-12",
                    confidence=0.95,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="pencil-date"),
            json=_capture_body("林溪5月12日生日", linxi),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["pendingCount"] == 0
        assert body["inboxItemIds"] == []
        (split,) = body["splits"]
        assert split["presentation"] == "pencil"
        assert split["glanceHint"] == "5月12日 · 点一下确认日期"
        assert split["inboxItemId"] is None
        (fact_id,) = split["factIds"]

        async with db_container.db.user_session(scope) as sess:
            rem = await sess.scalar(select(ReminderRow).where(ReminderRow.person_id == linxi))
            assert rem is not None and rem.status == "active"

        (mat,) = await _materials(client, token, linxi)
        assert mat["presentation"] == "pencil"
        assert mat["pencilReason"] == "date_glance"
        assert mat["autoInkAt"] is None
        (pf,) = mat["pencilFacts"]
        assert pf["id"] == fact_id and pf["needsGlance"] is True

        single = await client.get(f"/v1/materials/{mat['id']}", headers=_auth(token))
        assert single.json()["presentation"] == "pencil"

        # A glance: confirm the date as-is (no other change) -> ink
        glance = await client.patch(
            f"/v1/facts/{fact_id}",
            headers=_auth(token, idem="glance-1", if_match=f'"{pf["version"]}"'),
            json={"status": "confirmed"},
        )
        assert glance.status_code == 200, glance.text
        assert glance.json()["needsGlance"] is False
        assert glance.json()["status"] == "confirmed"

        (mat_after,) = await _materials(client, token, linxi)
        assert mat_after["presentation"] == "ink"
        assert mat_after["glanceHint"] is None
        assert mat_after["pencilFacts"] == []

        async with db_container.db.user_session(scope) as sess:
            rem = await sess.scalar(select(ReminderRow).where(ReminderRow.person_id == linxi))
            assert rem is not None and rem.status == "active"


@pytest.mark.asyncio
async def test_glance_with_corrected_date_moves_reminder(db_container: AppContainer) -> None:
    scope, token, dek = await _provision_user(db_container)
    mom = await _seed_person(db_container, scope, dek, display_name="妈妈", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="妈妈",
                    person_id=mom,
                    fact_type=FactType.PROMISE,
                    title="体检",
                    content="陪妈妈去体检",
                    trigger_date="2027-03-01",
                    confidence=0.95,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="pencil-promise"),
                json=_capture_body("下个月一号陪妈妈去体检", mom),
            )
        ).json()
        (fact_id,) = body["splits"][0]["factIds"]
        fix = await client.patch(
            f"/v1/facts/{fact_id}",
            headers=_auth(token, idem="glance-fix", if_match='"1"'),
            json={"triggerDate": "2027-03-02"},
        )
        assert fix.status_code == 200, fix.text
        assert fix.json()["needsGlance"] is False
        assert fix.json()["triggerDate"] == "2027-03-02"
        async with db_container.db.user_session(scope) as sess:
            rem = await sess.scalar(select(ReminderRow).where(ReminderRow.fact_id == UUID(fact_id)))
            assert rem is not None and rem.status == "active"
            assert rem.once_date is not None and rem.once_date.isoformat() == "2027-03-02"


@pytest.mark.asyncio
async def test_low_confidence_pencil_is_visible_and_auto_inks_after_48h(
    db_container: AppContainer, manual_clock: ManualClock
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="电影",
                    content="好像喜欢看悬疑片",
                    confidence=0.55,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)
    service = MemoryService(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="pencil-low"),
                json=_capture_body("林溪好像喜欢看悬疑片", linxi),
            )
        ).json()
        (split,) = body["splits"]
        assert split["presentation"] == "pencil"
        assert split["glanceHint"] == "没太听清 · 点一下确认"
        (inbox_id,) = body["inboxItemIds"]
        assert split["inboxItemId"] == inbox_id

        (mat,) = await _materials(client, token, linxi)
        assert mat["presentation"] == "pencil"
        assert mat["pencilReason"] == "low_confidence"
        assert mat["inboxItemId"] == inbox_id
        assert mat["autoInkAt"] is not None
        assert mat["pencilFacts"][0]["status"] == "pending"

        manual_clock.advance(hours=47)
        assert await service.auto_ink_due_pencil_drafts() == 0

        manual_clock.advance(hours=2)
        assert await service.auto_ink_due_pencil_drafts() == 1
        assert await service.auto_ink_due_pencil_drafts() == 0
        # access tokens are short-lived; mint a fresh one at the new clock time
        token, _ = db_container.tokens.create_access_token(
            scope.user_id, now=db_container.clock.now()
        )

        (mat_after,) = await _materials(client, token, linxi)
        assert mat_after["presentation"] == "ink"
        facts = (
            await client.get(f"/v1/persons/{linxi}/facts?status=confirmed", headers=_auth(token))
        ).json()["items"]
        assert [f["content"] for f in facts] == ["好像喜欢看悬疑片"]
        async with db_container.db.user_session(scope) as sess:
            item = await sess.get(InboxItemRow, UUID(inbox_id))
            assert item is not None
            assert item.status == "resolved"
            assert item.resolution_action == "confirm"


@pytest.mark.asyncio
async def test_user_settled_pencil_is_not_auto_inked(
    db_container: AppContainer, manual_clock: ManualClock
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi,
                    fact_type=FactType.PREFERENCE,
                    title="饮料",
                    content="可能爱喝乌龙",
                    confidence=0.5,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="pencil-discard"),
                json=_capture_body("林溪可能爱喝乌龙", linxi),
            )
        ).json()
        (inbox_id,) = body["inboxItemIds"]
        res = await client.post(
            f"/v1/inbox/{inbox_id}:resolve",
            headers=_auth(token, idem="pencil-discard-res"),
            json={"action": "keep_as_note"},
        )
        assert res.status_code == 200, res.text
        manual_clock.advance(hours=72)
        assert await MemoryService(container=db_container).auto_ink_due_pencil_drafts() == 0
        async with db_container.db.user_session(scope) as sess:
            fact = await sess.scalar(select(FactRow).where(FactRow.person_id == linxi))
            assert fact is not None and fact.status == "rejected"


@pytest.mark.asyncio
async def test_gold_venting_is_held_pencil_and_never_auto_inks(
    db_container: AppContainer, manual_clock: ManualClock
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            is_venting_or_irony=True,
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi,
                    fact_type=FactType.EVENT,
                    polarity=Polarity.NEGATIVE,
                    title="吵架",
                    content="又放我鸽子",
                    confidence=0.95,
                )
            ],
        )
    )
    app = _build_test_app(db_container, gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="pencil-vent"),
                json=_capture_body("林溪又放我鸽子，气死了", linxi),
            )
        ).json()
        (split,) = body["splits"]
        assert split["presentation"] == "pencil"
        assert split["glanceHint"] == "先替你收在冷静区"

        assert await _materials(client, token, linxi) == []
        (held,) = await _materials(client, token, linxi, held=True)
        assert held["presentation"] == "pencil"
        assert held["pencilReason"] == "venting"
        assert held["autoInkAt"] is None

        manual_clock.advance(hours=60)
        assert await MemoryService(container=db_container).auto_ink_due_pencil_drafts() == 0


@pytest.mark.asyncio
async def test_scoped_route_mode_alias_is_accepted(db_container: AppContainer) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi,
                    fact_type=FactType.TABOO,
                    title="忌口",
                    content="不吃香菜",
                    confidence=0.95,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        js = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="scoped-json"),
            json=_capture_body("她不吃香菜", linxi, route="scoped"),
        )
        assert js.status_code == 201, js.text
        assert js.json()["routeMode"] == "current"
        assert js.json()["splits"][0]["presentation"] == "ink"

        mp = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="scoped-multipart"),
            data={
                "source": "text",
                "text": "她还不吃葱",
                "routeMode": "scoped",
                "activePersonId": str(linxi),
                "sync": "true",
            },
        )
        assert mp.status_code == 201, mp.text
        assert mp.json()["routeMode"] == "current"


@pytest.mark.asyncio
async def test_fake_provider_onboarding_mom_becomes_question_then_gold_person(
    db_container: AppContainer,
) -> None:
    """Frontend onboarding path with the dev `fake` AI provider (no ProgrammableGateway)."""
    _scope, token, _dek = await _provision_user(db_container)
    gateway = ModelGateway(db_container.settings, clock=db_container.clock)
    db_container.extras["gateway"] = gateway
    db_container.extras["safety"] = SafetyService(
        gateway, timeout_seconds=db_container.settings.safety_timeout_seconds
    )
    app = FastAPI()
    app.state.container = db_container
    register_exception_handlers(app)
    app.include_router(capture_router, prefix="/v1")
    app.include_router(memory_router, prefix="/v1")
    app.include_router(bootstrap_router, prefix="/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="onboard-mom"),
            json=_capture_body("妈妈最近总说膝盖疼", route="global"),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        (split,) = body["splits"]
        assert split["personName"] == "妈妈"
        assert split["isNewPerson"] is True
        assert split["presentation"] == "question"
        assert split["glanceHint"] == "第一次提到这位朋友 · 轻点确认"
        assert split["inboxItemId"] is not None

        boot = await client.get("/v1/bootstrap", headers=_auth(token))
        assert boot.status_code == 200, boot.text
        assert boot.json()["questionCount"] == 1

        res = await client.post(
            f"/v1/inbox/{split['inboxItemId']}:resolve",
            headers=_auth(token, idem="onboard-mom-res"),
            json={"action": "create_person", "tier": "gold"},
        )
        assert res.status_code == 200, res.text
        pid = UUID(res.json()["targetPersonId"])
        (mat,) = await _materials(client, token, pid)
        assert mat["presentation"] == "ink"

        boot2 = (await client.get("/v1/bootstrap", headers=_auth(token))).json()
        assert boot2["questionCount"] == 0
        assert boot2["people"][str(pid)]["tier"] == "gold"
