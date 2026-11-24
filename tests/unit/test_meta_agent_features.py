"""Comprehensive tests for Meta AI Agent backend capabilities:
1. Streaming truthful `agent_step` SSE execution trace (zero hardcoded counts, never on crisis)
2. Executable 1-tap `action_proposal` SSE cards + `POST /v1/people/{id}/reminders` (RLS + AES-256-GCM AAD + pause/resume)
3. Voice-first conversational ASR `POST /v1/voice/transcribe` (0 DB records/facts + homophone/self-correction + security)
4. Proactive Relationship Care Brief `GET /v1/people/{id}/brief` (Gold / Silver / Crystal + strict isolation)
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from scripts.seed_demo import build_demo_wav_bytes
from sqlalchemy import func, select

from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.gateway import ModelGateway
from mindweave.core.clock import ManualClock
from mindweave.core.crypto import CryptoError
from mindweave.core.dependencies import AppContainer
from mindweave.core.dto import FORBIDDEN_UI_DEV_PHRASES
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    FactRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.main import create_app
from mindweave.modules.companion.domain.output_guard import FORBIDDEN_PSEUDOSCIENCE_PHRASES
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderRule,
    ReminderStatus,
    SourceSpeaker,
    Tier,
    TimeStatus,
)
from mindweave.shared.scopes import UserScope


def _parse_sse_frames(raw_body: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw_body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = "message"
        data_str = "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_str = line.removeprefix("data:").strip()
        frames.append((event_name, json.loads(data_str)))
    return frames


def _make_wav_with_text_fixture(text: str) -> bytes:
    """Create a valid binary WAV header followed by a `TEXT:` payload for FakeAdapter ASR tests."""
    payload = f"TEXT:{text}".encode()
    riff_size = (4 + len(payload)).to_bytes(4, "little")
    return b"RIFF" + riff_size + b"WAVE" + payload


async def _provision_user(
    container: AppContainer,
    *,
    tz_name: str = "Asia/Shanghai",
) -> tuple[UUID, str, bytes]:
    user_id = new_id()
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=user_id, status="active"))

    scope = UserScope(user_id)
    async with container.db.user_session(scope) as sess:
        sess.add(UserKeyRow(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id))
        sess.add(
            UserPreferenceRow(
                user_id=user_id,
                timezone=tz_name,
                late_night_care_enabled=True,
            )
        )
        await sess.flush()
        dek = await container.load_user_dek(sess, scope)

    token, _ = container.tokens.create_access_token(
        user_id=user_id,
        now=container.clock.now(),
    )
    return user_id, token, dek


async def _seed_person(
    container: AppContainer,
    *,
    user_id: UUID,
    dek: bytes,
    name: str,
    tier: Tier,
    role_tag: str = "",
) -> UUID:
    pid = new_id()
    async with container.db.user_session(UserScope(user_id)) as sess:
        sess.add(
            PersonRow(
                id=pid,
                user_id=user_id,
                tier=tier.value,
                rank="i",
                state="active",
                display_name_enc=container.cipher.encrypt_str(
                    dek,
                    name,
                    user_id=user_id,
                    table="persons",
                    column="display_name_enc",
                    row_id=pid,
                ),
                monogram=name[0],
                role_tag_enc=(
                    container.cipher.encrypt_str(
                        dek,
                        role_tag,
                        user_id=user_id,
                        table="persons",
                        column="role_tag_enc",
                        row_id=pid,
                    )
                    if role_tag
                    else None
                ),
                traits_enc=container.cipher.encrypt_json(
                    dek,
                    [],
                    user_id=user_id,
                    table="persons",
                    column="traits_enc",
                    row_id=pid,
                ),
            )
        )
    return pid


async def _seed_record_and_fact(
    container: AppContainer,
    *,
    user_id: UUID,
    person_id: UUID,
    dek: bytes,
    title: str,
    body: str,
    fact_type: FactType | None = FactType.PREFERENCE,
    fact_content: str | None = None,
    fact_status: FactStatus = FactStatus.CONFIRMED,
    needs_glance: bool = False,
    is_lunar: bool = False,
    lunar_month: int | None = None,
    lunar_day: int | None = None,
    trigger_date: date | None = None,
    review_state: RecordReviewState = RecordReviewState.NORMAL,
    deleted_at: datetime | None = None,
    happened_on: date = date(2026, 11, 16),
) -> tuple[UUID, UUID | None]:
    rec_id = new_id()
    fact_id: UUID | None = None
    async with container.db.user_session(UserScope(user_id)) as sess:
        sess.add(
            RecordRow(
                id=rec_id,
                user_id=user_id,
                person_id=person_id,
                kind=RecordKind.NOTE.value,
                title_enc=container.cipher.encrypt_str(
                    dek, title, user_id=user_id, table="records", column="title_enc", row_id=rec_id
                ),
                body_enc=container.cipher.encrypt_str(
                    dek, body, user_id=user_id, table="records", column="body_enc", row_id=rec_id
                ),
                snippet_enc=container.cipher.encrypt_str(
                    dek,
                    body[:50],
                    user_id=user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                happened_on=happened_on,
                pinned=True,
                review_state=review_state.value,
                deleted_at=deleted_at,
                version=1,
            )
        )
        await sess.flush()
        if fact_type is not None and fact_content is not None:
            fact_id = new_id()
            sess.add(
                FactRow(
                    id=fact_id,
                    user_id=user_id,
                    person_id=person_id,
                    record_id=rec_id,
                    type=fact_type.value,
                    polarity=(
                        Polarity.NEGATIVE.value
                        if fact_type is FactType.TABOO
                        else Polarity.POSITIVE.value
                    ),
                    time_status=TimeStatus.CURRENT.value,
                    source_speaker=SourceSpeaker.NARRATOR.value,
                    content_enc=container.cipher.encrypt_str(
                        dek,
                        fact_content,
                        user_id=user_id,
                        table="facts",
                        column="content_enc",
                        row_id=fact_id,
                    ),
                    trigger_date=trigger_date,
                    is_lunar=is_lunar,
                    lunar_month=lunar_month,
                    lunar_day=lunar_day,
                    status=fact_status.value,
                    needs_glance=needs_glance,
                    confidence=0.95,
                    created_by="user",
                    version=1,
                )
            )
    return rec_id, fact_id


async def _seed_reminder(
    container: AppContainer,
    *,
    user_id: UUID,
    person_id: UUID,
    dek: bytes,
    title: str,
    once_date: date,
    status: ReminderStatus = ReminderStatus.ACTIVE,
    fact_id: UUID | None = None,
) -> UUID:
    rid = new_id()
    fire_utc = datetime(once_date.year, once_date.month, once_date.day, 1, 0, tzinfo=UTC)
    async with container.db.user_session(UserScope(user_id)) as sess:
        sess.add(
            ReminderRow(
                id=rid,
                user_id=user_id,
                person_id=person_id,
                fact_id=fact_id,
                title_enc=container.cipher.encrypt_str(
                    dek, title, user_id=user_id, table="reminders", column="title_enc", row_id=rid
                ),
                rule=ReminderRule.ONCE.value,
                is_lunar=False,
                once_date=once_date,
                fire_hour=9,
                fire_minute=0,
                lead_days=[1, 0],
                timezone="Asia/Shanghai",
                next_fire_at=fire_utc if status is ReminderStatus.ACTIVE else None,
                next_lead_day=0 if status is ReminderStatus.ACTIVE else None,
                status=status.value,
                version=1,
            )
        )
    return rid


# ============================================================================
# 1. Streaming Truthful Agent Execution Trace (`agent_step` SSE events)
# ============================================================================


@pytest.mark.asyncio
async def test_streaming_agent_step_truthful_counts_recall_dates_and_crisis_isolation(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))  # 10:00 Asia/Shanghai
    app = create_app(container=db_container)

    user_id, token, dek = await _provision_user(db_container)
    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    chen_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="陈经理", tier=Tier.SILVER, role_tag="上司"
    )

    # Seed 3 eligible records + 2 confirmed facts for 妈妈
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="冬至橱窗围巾心愿",
        body="去年冬至路过橱窗她摸着浅驼色羊绒围巾说软软的真暖和",
        fact_type=FactType.WISH,
        fact_content="喜欢浅驼色羊绒围巾",
        happened_on=date(2025, 12, 21),
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="右膝盖复查安排",
        body="11月28日上午陪妈妈去市一院骨科复查右膝盖，记得带医保卡",
        fact_type=FactType.DATE,
        fact_content="11月28日去市一院复查右膝盖",
        trigger_date=date(2026, 11, 28),
        happened_on=date(2026, 11, 16),
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="日常笔记",
        body="喜欢在南阳台晒太阳喝桂花乌龙茶",
        fact_type=None,
        fact_content=None,
        review_state=RecordReviewState.NOTE_ONLY,
        happened_on=date(2026, 10, 10),
    )
    # Ineligible records that MUST NEVER be scanned or counted (I4):
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="气话暂存",
        body="浅驼色羊绒围巾气话暂存绝不可被扫到",
        fact_type=FactType.NOTE,
        fact_content="气话暂存内容",
        review_state=RecordReviewState.HELD,
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="已软删记录",
        body="已删除围巾内容",
        fact_type=FactType.NOTE,
        fact_content="已删除内容",
        deleted_at=datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="待确认草稿",
        body="未确认的围巾偏好",
        fact_type=FactType.PREFERENCE,
        fact_content="待确认事实不计入已确认数",
        fact_status=FactStatus.PENDING,
    )
    # 1 active reminder + 1 paused reminder for 妈妈
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="陪妈妈去市一院做右膝盖骨科复查",
        once_date=date(2026, 11, 28),
        status=ReminderStatus.ACTIVE,
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="已暂停旧提醒",
        once_date=date(2026, 12, 1),
        status=ReminderStatus.PAUSED,
    )
    # Other person's record & reminder MUST NOT leak into 妈妈's counts
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=chen_id,
        dek=dek,
        title="陈经理周会记录",
        body="陈经理周五发邮件改需求",
        fact_type=FactType.EVENT,
        fact_content="周五发邮件临时改需求",
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=chen_id,
        dek=dek,
        title="陈经理季度复盘会",
        once_date=date(2026, 11, 26),
        status=ReminderStatus.ACTIVE,
    )

    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Grounded recall query -> emits 3 truthful agent_step events BEFORE first delta
        resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "meta-step-1"},
            json={"message": "她去年冬至提过想要什么围巾"},
        )
        assert resp.status_code == 200
        frames = _parse_sse_frames(resp.text)
        event_names = [n for n, _ in frames]

        first_delta_idx = event_names.index("delta")
        step_indices = [i for i, n in enumerate(event_names) if n == "agent_step"]
        assert len(step_indices) == 3
        assert all(i < first_delta_idx for i in step_indices)

        steps = [frames[i][1] for i in step_indices]
        assert steps[0] == {
            "step": 1,
            "phase": "scan",
            "label": "正在翻看关于「妈妈」的手帐…",
            "count": 3,
        }
        assert steps[1] == {
            "step": 2,
            "phase": "facts",
            "label": "正在整理相关细节与日程…",
            "count": 3,
        }
        assert steps[2]["step"] == 3
        assert steps[2]["phase"] == "recall"
        assert steps[2]["count"] >= 1
        assert "2025-12-21" in steps[2]["detail"]
        assert steps[2]["label"] == "已找到相关手帐记录"

        # Verify zero dev jargon or UUID leakage in agent_step frames
        step_blob = json.dumps(steps, ensure_ascii=False)
        for bad in FORBIDDEN_UI_DEV_PHRASES:
            assert bad not in step_blob

        # 2. Zero-overlap query -> truthful 0 recall hits message
        miss_resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "meta-step-miss"},
            json={"message": "量子纠缠与火星殖民飞船"},
        )
        assert miss_resp.status_code == 200
        miss_steps = [p for n, p in _parse_sse_frames(miss_resp.text) if n == "agent_step"]
        assert miss_steps[-1]["phase"] == "recall"
        assert miss_steps[-1]["count"] == 0
        assert miss_steps[-1]["label"] == "未找到更早的直接原话，将如实告知"

        # 3. Crisis message -> NEVER emits agent_step or action_proposal!
        crisis_resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "meta-crisis-1"},
            json={"message": "我觉得活着一点意义都没有，今晚把安眠药全吃了再也不醒来"},
        )
        assert crisis_resp.status_code == 200
        crisis_frames = _parse_sse_frames(crisis_resp.text)
        assert [n for n, _ in crisis_frames] == ["crisis", "done"]


# ============================================================================
# 2. Executable 1-Tap Action Proposals & `POST /v1/people/{id}/reminders`
# ============================================================================


@pytest.mark.asyncio
async def test_action_proposals_gold_reminder_silver_save_note_and_person_reminders_security(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))  # 10:00 Asia/Shanghai
    app = create_app(container=db_container)

    user_a, token_a, dek_a = await _provision_user(db_container)
    user_b, token_b, dek_b = await _provision_user(db_container)

    mom_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    chen_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="陈经理", tier=Tier.SILVER, role_tag="上司"
    )
    other_b_person = await _seed_person(
        db_container, user_id=user_b, dek=dek_b, name="林屿", tier=Tier.GOLD
    )

    _, mom_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="右膝盖复查安排",
        body="11月28日陪妈妈去市一院骨科复查右膝盖，记得带医保卡和旧片子",
        fact_type=FactType.DATE,
        fact_content="11月28日去市一院做右膝盖复查",
        trigger_date=date(2026, 11, 28),
    )
    _, chen_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chen_id,
        dek=dek_a,
        title="Q3需求冻结邮件",
        body="10月9日16:22陈经理邮件确认Q3交付范围冻结，但周五17:45又临时加需求",
        fact_type=FactType.EVENT,
        fact_content="10月9日邮件确认范围冻结后又临时加需求",
        happened_on=date(2026, 10, 9),
    )
    await _seed_reminder(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="陪妈妈去市一院做右膝盖骨科复查",
        once_date=date(2026, 11, 28),
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Gold planning query emits `action_proposal` (create_reminder) BEFORE `done`
        gold_sse = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers_a, "Idempotency-Key": "meta-prop-gold"},
            json={"message": "11月28日陪妈妈做右膝盖复查，结合我记下的手帐，帮我列一份准备清单"},
        )
        assert gold_sse.status_code == 200
        g_frames = _parse_sse_frames(gold_sse.text)
        g_names = [n for n, _ in g_frames]
        assert g_names[-1] == "done"
        prop_indices = [i for i, n in enumerate(g_names) if n == "action_proposal"]
        assert 1 <= len(prop_indices) <= 2
        assert all(i < len(g_names) - 1 for i in prop_indices)

        reminder_prop = next(
            p for n, p in g_frames if n == "action_proposal" and p["kind"] == "create_reminder"
        )
        assert reminder_prop["label"] == "提前设一个轻提醒"
        assert 1 <= len(reminder_prop["title"]) <= 36
        assert reminder_prop["triggerDate"] == "2026-11-27"
        assert reminder_prop["whenLabel"] == "11月27日"

        # 1b. Upcoming reminder within 14d without prep reminder also triggers action_proposal on general query
        auto_sse = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers_a, "Idempotency-Key": "meta-prop-upcoming-auto"},
            json={"message": "妈妈最近有什么要留心的事吗？"},
        )
        assert auto_sse.status_code == 200
        auto_props = [p for n, p in _parse_sse_frames(auto_sse.text) if n == "action_proposal"]
        assert len(auto_props) == 1
        assert auto_props[0]["kind"] == "create_reminder"
        assert auto_props[0]["triggerDate"] == "2026-11-27"

        # 2. Execute the 1-tap `action_proposal` against real `POST /v1/people/{id}/reminders`!
        create_resp = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers={**headers_a, "Idempotency-Key": "meta-create-rem-1"},
            json={
                "title": reminder_prop["title"],
                "triggerDate": reminder_prop["triggerDate"],
                "factId": str(mom_fact_id),
            },
        )
        assert create_resp.status_code == 201
        assert create_resp.headers.get("ETag") == '"1"'
        rem_dto = create_resp.json()
        rid = UUID(rem_dto["id"])
        assert rem_dto["personId"] == str(mom_id)
        assert rem_dto["factId"] == str(mom_fact_id)
        assert rem_dto["title"] == reminder_prop["title"]
        assert rem_dto["rule"] == "once"
        assert rem_dto["onceDate"] == "2026-11-27"
        assert rem_dto["isLunar"] is False
        assert rem_dto["status"] == "active"
        assert rem_dto["nextFireAt"] is not None
        assert rem_dto["version"] == 1

        # Idempotent replay returns identical reminder
        replay_resp = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers={**headers_a, "Idempotency-Key": "meta-create-rem-1"},
            json={
                "title": reminder_prop["title"],
                "triggerDate": reminder_prop["triggerDate"],
                "factId": str(mom_fact_id),
            },
        )
        assert replay_resp.status_code == 201
        assert replay_resp.json()["id"] == str(rid)

        # Verify `/v1/persons/{id}/reminders` alias works seamlessly too
        alias_rem = await client.post(
            f"/v1/persons/{mom_id}/reminders",
            headers=headers_a,
            json={"title": "提醒带医保卡", "triggerDate": "2026-11-27"},
        )
        assert alias_rem.status_code == 201
        assert alias_rem.json()["personId"] == str(mom_id)

        # Verify AES-256-GCM ciphertext at rest + exact AAD binding (`reminders.title_enc`)
        async with db_container.db.user_session(UserScope(user_a)) as sess:
            rem_row = await sess.get(ReminderRow, rid)
            assert rem_row is not None
            assert reminder_prop["title"].encode("utf-8") not in rem_row.title_enc
            decrypted_title = db_container.cipher.decrypt_str(
                dek_a,
                rem_row.title_enc,
                user_id=user_a,
                table="reminders",
                column="title_enc",
                row_id=rid,
            )
            assert decrypted_title == reminder_prop["title"]
            # Tampered row_id AAD MUST fail authentication!
            with pytest.raises(CryptoError):
                db_container.cipher.decrypt_str(
                    dek_a,
                    rem_row.title_enc,
                    user_id=user_a,
                    table="reminders",
                    column="title_enc",
                    row_id=new_id(),
                )

        # Verify pause & resume via `PATCH /v1/reminders/{reminder_id}`
        pause_resp = await client.patch(
            f"/v1/reminders/{rid}",
            headers={**headers_a, "If-Match": '"1"', "Idempotency-Key": "meta-pause-1"},
            json={"status": "paused"},
        )
        assert pause_resp.status_code == 200
        assert pause_resp.json()["status"] == "paused"
        assert pause_resp.json()["nextFireAt"] is None
        assert pause_resp.json()["version"] == 2

        resume_resp = await client.patch(
            f"/v1/reminders/{rid}",
            headers={**headers_a, "If-Match": '"2"', "Idempotency-Key": "meta-resume-1"},
            json={"status": "active"},
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "active"
        assert resume_resp.json()["nextFireAt"] is not None
        assert resume_resp.json()["version"] == 3

        # Security: Cross-tenant creation (User B -> User A's person) -> 404!
        cross_tenant = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=headers_b,
            json={"title": "越权提醒", "triggerDate": "2026-11-27"},
        )
        assert cross_tenant.status_code == 404

        # Security: Cross-person factId spoofing (陈经理's factId attached to 妈妈's reminder) -> 404!
        cross_fact = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=headers_a,
            json={
                "title": "跨人物关联测试",
                "triggerDate": "2026-11-27",
                "factId": str(chen_fact_id),
            },
        )
        assert cross_fact.status_code == 404

        # Security: User A cannot create reminder on User B's person -> 404!
        cross_b = await client.post(
            f"/v1/people/{other_b_person}/reminders",
            headers=headers_a,
            json={"title": "越权测试", "triggerDate": "2026-11-27"},
        )
        assert cross_b.status_code == 404

        # 3. Silver tier timeline query emits calm objective `save_note` action proposal (NEVER reconciliation!)
        silver_sse = await client.post(
            f"/v1/persons/{chen_id}/chat/stream",
            headers={**headers_a, "Idempotency-Key": "meta-prop-silver"},
            json={"message": "按时间线帮我整理和陈经理的关键客观事实与书面留痕要点"},
        )
        assert silver_sse.status_code == 200
        s_frames = _parse_sse_frames(silver_sse.text)
        assert all(n != "perspective" for n, _ in s_frames)
        save_props = [p for n, p in s_frames if n == "action_proposal" and p["kind"] == "save_note"]
        assert len(save_props) == 1
        assert save_props[0]["label"] == "把这份时间线要点存进手帐"
        assert save_props[0]["title"] == "陈经理关键时间线留痕"
        assert save_props[0]["body"].strip() != ""
        assert save_props[0]["body"].startswith("• 10月9日：")
        assert "[2026-10-09]" not in save_props[0]["body"]
        assert "10月9日：10月9日" not in save_props[0]["body"]
        for forbidden_preach in ("换位思考", "苦衷", "原谅", "体谅"):
            assert forbidden_preach not in save_props[0]["body"]


# ============================================================================
# 3. Voice-First Conversational ASR (`POST /v1/voice/transcribe`)
# ============================================================================


@pytest.mark.asyncio
async def test_voice_transcribe_conversational_asr_zero_records_and_homophone_self_correction(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    app = create_app(container=db_container)

    user_a, token_a, dek_a = await _provision_user(db_container)
    user_b, _token_b, dek_b = await _provision_user(db_container)

    chen_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="陈经理", tier=Tier.SILVER, role_tag="上司"
    )
    mom_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    other_b_person = await _seed_person(
        db_container, user_id=user_b, dek=dek_b, name="林屿", tier=Tier.GOLD
    )

    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chen_id,
        dek=dek_a,
        title="茶饮偏好",
        body="陈经理开会喜欢喝桂花乌龙",
        fact_type=FactType.PREFERENCE,
        fact_content="喜欢喝桂花乌龙",
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="右膝盖复查",
        body="下周陪妈妈去骨科复查右膝盖",
        fact_type=FactType.EVENT,
        fact_content="右膝盖需要骨科复查",
    )

    async def _db_counts(uid: UUID) -> tuple[int, int, int]:
        async with db_container.db.user_session(UserScope(uid)) as sess:
            caps = int(await sess.scalar(select(func.count(CaptureRow.id))) or 0)
            recs = int(await sess.scalar(select(func.count(RecordRow.id))) or 0)
            facts = int(await sess.scalar(select(func.count(FactRow.id))) or 0)
            return caps, recs, facts

    before_counts = await _db_counts(user_a)
    headers_a = {"Authorization": f"Bearer {token_a}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Homophone restoration (`程经理`->`陈经理`, `桂花乌笼`->`桂花乌龙`) + spoken self-correction (`周五，不对，是周六`)
        wav_with_text = _make_wav_with_text_fixture("程经理说约了周五，不对，是周六喝桂花乌笼")
        resp = await client.post(
            "/v1/voice/transcribe",
            headers=headers_a,
            files={"audio": ("question.wav", wav_with_text, "audio/wav")},
            data={"active_person_id": str(chen_id)},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["corrected"] == "陈经理说约了周六喝桂花乌龙"
        assert body["transcript"] == "陈经理说约了周六喝桂花乌龙"
        assert isinstance(body["durationMs"], int) and body["durationMs"] > 0

        # 2. Real opaque PCM WAV audio on FakeAdapter -> Invariant I13 forbids fabricating fake text!
        opaque_wav = build_demo_wav_bytes()
        resp_q = await client.post(
            "/v1/voice/transcribe",
            headers=headers_a,
            files={"audio": ("cupid_q.wav", opaque_wav, "audio/wav")},
            data={"activePersonId": str(mom_id)},
        )
        assert resp_q.status_code == 502
        q_body = resp_q.json()
        assert q_body["code"] in {"ai_upstream_error", "asr_provider_unavailable"}

        # 3. Silent fixture -> empty transcript (zero hallucination)
        silent_wav = _make_wav_with_text_fixture("SILENCE")
        resp_s = await client.post(
            "/v1/voice/transcribe",
            headers=headers_a,
            files={"audio": ("silent.wav", silent_wav, "audio/wav")},
        )
        assert resp_s.status_code == 200
        assert resp_s.json()["transcript"] == ""
        assert resp_s.json()["corrected"] == ""

        # 4. Security: invalid magic bytes -> 422
        bad_mime = await client.post(
            "/v1/voice/transcribe",
            headers=headers_a,
            files={"audio": ("bad.pdf", b"%PDF-1.7-fake-document", "application/pdf")},
        )
        assert bad_mime.status_code == 422

        # 5. Security: oversized audio -> 413
        orig_limit = db_container.settings.max_audio_bytes
        db_container.settings.max_audio_bytes = 64
        try:
            too_big = await client.post(
                "/v1/voice/transcribe",
                headers=headers_a,
                files={"audio": ("big.wav", opaque_wav, "audio/wav")},
            )
            assert too_big.status_code == 413
        finally:
            db_container.settings.max_audio_bytes = orig_limit

        # 6. Security: cross-tenant active_person_id -> 404!
        cross_person = await client.post(
            "/v1/voice/transcribe",
            headers=headers_a,
            files={"audio": ("q.wav", opaque_wav, "audio/wav")},
            data={"active_person_id": str(other_b_person)},
        )
        assert cross_person.status_code == 404

    # 7. CRITICAL INVARIANT: ZERO CaptureRow, RecordRow, or FactRow created by transcribe_only!
    after_counts = await _db_counts(user_a)
    assert after_counts == before_counts


# ============================================================================
# 4. Proactive Relationship Care Brief (`GET /v1/people/{id}/brief`)
# ============================================================================


@pytest.mark.asyncio
async def test_person_brief_gold_silver_crystal_tiers_and_strict_rls_isolation(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))  # 10:00 Asia/Shanghai
    app = create_app(container=db_container)

    user_a, token_a, dek_a = await _provision_user(db_container)
    user_b, token_b, dek_b = await _provision_user(db_container)

    mom_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    grandma_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="外婆", tier=Tier.GOLD, role_tag="家人"
    )
    chen_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="陈经理", tier=Tier.SILVER, role_tag="上司"
    )
    dr_lin_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="林医生", tier=Tier.CRYSTAL, role_tag="骨科"
    )
    user_b_person = await _seed_person(
        db_container, user_id=user_b, dek=dek_b, name="跨租户秘密人物", tier=Tier.GOLD
    )

    # Gold #1 (妈妈): 5-day upcoming checkup reminder + lunar May 12 D1 pencil fact + taboo + held/deleted decoys
    rec_checkup, fact_checkup = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="右膝盖骨科复查",
        body="11月28日陪妈妈去市一院做右膝盖骨科复查",
        fact_type=FactType.DATE,
        fact_content="11月28日陪妈妈去市一院做右膝盖骨科复查",
        trigger_date=date(2026, 11, 28),
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="农历生日轻记",
        body="妈妈农历五月十二过生日",
        fact_type=FactType.DATE,
        fact_content="农历五月十二过生日",
        fact_status=FactStatus.CONFIRMED,
        needs_glance=True,
        is_lunar=True,
        lunar_month=5,
        lunar_day=12,
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="饮食禁忌",
        body="严重香菜过敏，菜里绝不能放香菜",
        fact_type=FactType.TABOO,
        fact_content="对香菜严重过敏",
    )
    # Held & deleted records MUST NOT inflate recordCount or surface in brief!
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="冷静区气话",
        body="气话暂存绝不能进简报",
        fact_type=FactType.NOTE,
        fact_content="气话暂存绝不能进简报",
        review_state=RecordReviewState.HELD,
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="已删除记录",
        body="已删除内容",
        fact_type=FactType.NOTE,
        fact_content="已删除内容",
        deleted_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    await _seed_reminder(
        db_container,
        user_id=user_a,
        person_id=mom_id,
        dek=dek_a,
        title="陪妈妈去市一院做右膝盖骨科复查",
        once_date=date(2026, 11, 28),
        fact_id=fact_checkup,
    )

    # Gold #2 (外婆): lunar D1 pencil draft only (no reminder within 14d)
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=grandma_id,
        dek=dek_a,
        title="外婆农历生日",
        body="外婆农历八月十五过寿",
        fact_type=FactType.DATE,
        fact_content="外婆农历八月十五过寿",
        fact_status=FactStatus.CONFIRMED,
        needs_glance=True,
        is_lunar=True,
        lunar_month=8,
        lunar_day=15,
    )

    # Silver (陈经理): 6 objective records, ZERO reconciliation
    for idx in range(1, 7):
        await _seed_record_and_fact(
            db_container,
            user_id=user_a,
            person_id=chen_id,
            dek=dek_a,
            title=f"客观工作留痕 #{idx}",
            body=f"10月{idx}日周会客观沟通与书面邮件留痕记录 #{idx}",
            fact_type=FactType.EVENT,
            fact_content=f"10月{idx}日书面留痕事件 #{idx}",
            happened_on=date(2026, 10, idx),
        )

    # Crystal (林医生): 2 crisp notes
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=dr_lin_id,
        dek=dek_a,
        title="门诊时间",
        body="每周二、周六上午在市一院骨科专家门诊坐诊",
        fact_type=FactType.PREFERENCE,
        fact_content="周二周六上午专家门诊",
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=dr_lin_id,
        dek=dek_a,
        title="挂号提示",
        body="提前七天早上七点在公众号放号",
        fact_type=FactType.NOTE,
        fact_content="提前七天早七点放号",
    )

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Gold with upcoming reminder within 14 days
        mom_resp = await client.get(f"/v1/people/{mom_id}/brief", headers=headers_a)
        assert mom_resp.status_code == 200
        mom_brief = mom_resp.json()
        assert mom_brief["personId"] == str(mom_id)
        assert mom_brief["tier"] == "gold"
        assert mom_brief["upcomingCount"] == 1
        assert mom_brief["pendingCount"] == 1
        assert mom_brief["recordCount"] == 3  # Strictly excludes held + deleted!
        assert mom_brief["headline"] == "还有 5 天 · 陪妈妈去市一院做右膝盖骨科复查"
        assert "11月28日" in mom_brief["prompt"]
        assert "准备清单" in mom_brief["prompt"]
        assert len(mom_brief["items"]) == 3
        assert {it["kind"] for it in mom_brief["items"]} == {"reminder", "pencil", "fact"}
        rem_item = next(it for it in mom_brief["items"] if it["kind"] == "reminder")
        assert rem_item["recordId"] == str(rec_checkup)
        # Zero cross-person bleed
        mom_blob = json.dumps(mom_brief, ensure_ascii=False)
        assert "陈经理" not in mom_blob
        assert "林医生" not in mom_blob
        assert "气话暂存" not in mom_blob

        # Also verify `/v1/persons/{id}/brief` alias returns identical response
        alias_resp = await client.get(f"/v1/persons/{mom_id}/brief", headers=headers_a)
        assert alias_resp.status_code == 200
        assert alias_resp.json() == mom_brief

        # 2. Gold with pending lunar D1 pencil draft (no reminder <= 14d)
        gm_resp = await client.get(f"/v1/people/{grandma_id}/brief", headers=headers_a)
        assert gm_resp.status_code == 200
        gm_brief = gm_resp.json()
        assert gm_brief["headline"] == "有 1 条农历日子轻记，点一下即可确认落墨"
        assert gm_brief["pendingCount"] == 1

        # 3. Silver tier: calm factual count & timeline prompt, ZERO reconciliation
        chen_resp = await client.get(f"/v1/people/{chen_id}/brief", headers=headers_a)
        assert chen_resp.status_code == 200
        chen_brief = chen_resp.json()
        assert chen_brief["tier"] == "silver"
        assert chen_brief["recordCount"] == 6
        assert chen_brief["headline"] == "已冷静留存 6 份客观事实与时间线记录"
        assert "时间线" in chen_brief["prompt"]
        assert "书面留痕" in chen_brief["prompt"]
        chen_blob = json.dumps(chen_brief, ensure_ascii=False)
        assert "妈妈" not in chen_blob
        for forbidden_preach in ("换位思考", "原谅", "体谅", "苦衷"):
            assert forbidden_preach not in chen_blob

        # 4. Crystal tier: clean points & todos
        lin_resp = await client.get(f"/v1/people/{dr_lin_id}/brief", headers=headers_a)
        assert lin_resp.status_code == 200
        lin_brief = lin_resp.json()
        assert lin_brief["tier"] == "crystal"
        assert lin_brief["recordCount"] == 2
        assert lin_brief["headline"] == "已收好关于「林医生」的要点与待办"
        assert "妈妈" not in json.dumps(lin_brief, ensure_ascii=False)

        # 5. Zero pseudo-science & zero forbidden dev jargon across ALL brief payloads
        all_briefs_blob = json.dumps(
            [mom_brief, gm_brief, chen_brief, lin_brief], ensure_ascii=False
        )
        for bad in (*FORBIDDEN_UI_DEV_PHRASES, *FORBIDDEN_PSEUDOSCIENCE_PHRASES):
            assert bad not in all_briefs_blob

        # 6. Cross-tenant FORCE RLS isolation: User A -> User B's person & User B -> User A's person -> 404!
        assert (
            await client.get(f"/v1/people/{user_b_person}/brief", headers=headers_a)
        ).status_code == 404
        assert (
            await client.get(f"/v1/people/{mom_id}/brief", headers=headers_b)
        ).status_code == 404


# ============================================================================
# 5. FakeAdapter Direct Unit Guard: Context-Aware Question Synthesis
# ============================================================================


@pytest.mark.asyncio
async def test_fake_adapter_transcribe_preserves_existing_and_supports_chat_audio() -> None:
    from mindweave.ai.schemas.contracts import TranscribeInput
    from mindweave.core.config import Settings
    from mindweave.core.errors import UpstreamAIError

    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="fake"),
        adapters={"fake": FakeAdapter()},
    )
    scope = UserScope(new_id())
    wav_bytes = build_demo_wav_bytes()

    # Invariant I13: Opaque binary audio MUST raise an honest UpstreamAIError (NEVER fabricate fake text!)
    with pytest.raises(UpstreamAIError):
        await gw.transcribe(
            scope=scope,
            payload=TranscribeInput(
                audio_bytes=wav_bytes,
                mime_type="audio/wav",
                active_person_name="妈妈",
                known_person_names=["妈妈", "陈经理"],
                recent_context_snippets=["11月28日骨科复查右膝盖"],
            ),
        )

    # Explicit TEXT: test fixture still passes through deterministically with ASR post-processing
    fixture_wav = _make_wav_with_text_fixture("妈妈下周六复查要注意什么？")
    out = await gw.transcribe(
        scope=scope,
        payload=TranscribeInput(
            audio_bytes=fixture_wav,
            mime_type="audio/wav",
            active_person_name="妈妈",
            known_person_names=["妈妈", "陈经理"],
        ),
    )
    assert "妈妈" in out.corrected_transcript
    assert "复查" in out.corrected_transcript
    assert out.corrected_transcript.endswith("？")


# ============================================================================
# 6. Round 2 Mandatory Remediation Regression Tests (Defects 1-7)
# ============================================================================


def test_r2_defect1_writeback_rejects_starter_prompts_brief_prompts_and_assistant_requests() -> (
    None
):
    from mindweave.modules.companion.domain.tier_policy import should_writeback_chat_message
    from mindweave.shared.enums import SafetyCategory

    # ALL 8 STARTER_PROMPTS from web/scripts/views/chat-render.js MUST return False
    starter_prompts = [
        "最近有什么要替 TA 留意的？",
        "结合手帐，帮我挑份贴心小礼物",
        "帮我理理和 TA 最近的细节",
        "按时间线帮我理清客观事实",
        "起草一封不卑不亢的书面留痕",
        "提醒我守好哪些边界",
        "整理一下和 TA 的待办与礼数",
        "最近记了哪些要点？",
    ]
    for prompt in starter_prompts:
        assert not should_writeback_chat_message(prompt), (
            f"Starter prompt leaked writeback: {prompt}"
        )

    # ALL brief.prompt & item action_prompt templates MUST return False
    brief_prompts = [
        "11月28日陪妈妈去市一院做右膝盖骨科复查，结合我记下的手帐，帮我列一份准备清单",
        "12月6日妈妈生日，结合我记下的手帐，帮我列一份准备清单",
        "结合我为「妈妈」记下的手帐，最近有什么贴心细节值得提前留意？",
        "结合手帐里关于「妈妈」的偏好与忌口，最近可以准备什么贴心小事？",
        "我想开始记录关于「妈妈」的日常细节，可以从哪些小事记起？",
        "按时间线帮我整理和「陈经理」的关键客观事实与书面留痕要点",
        "帮我快速梳理关于「林医生」的要点、偏好与近期待办",
        "围绕「11月28日骨科复查」，帮我梳理关键时间节点与书面留痕要点",
        "结合「10月9日书面留痕事件」，按时间线整理客观事实与防雷回复要点",
        "核对「待确认要点」这条记录并纳入时间线要点",
        "关于「农历五月十二过生日」，结合手帐帮我确认一下安排与需要注意的细节",
        "围绕「对香菜严重过敏」，帮我看看和「妈妈」相处或准备时有什么讲究",
    ]
    for prompt in brief_prompts:
        assert not should_writeback_chat_message(prompt), f"Brief prompt leaked writeback: {prompt}"

    # Exact judge failing compound query + generic assistant imperatives & non-factual long clause
    assert not should_writeback_chat_message(
        "去年冬至妈妈说想要什么围巾？另外11月28日骨科复查帮我提前设个提醒准备一下"
    )
    assert not should_writeback_chat_message("提醒我下周二带医保卡去医院")
    assert not should_writeback_chat_message("天气真晴朗云朵飘在天空中")

    # Real declarative personal facts & explicit recording cues MUST still return True
    assert should_writeback_chat_message("顺便记一下，清秋最近迷上了桂花乌龙，而且对芒果过敏")
    assert should_writeback_chat_message("帮我记进手帐：妈妈喜欢浅驼色羊绒围巾")
    assert should_writeback_chat_message("记进手帐：陈教授下周二上午九点签字")
    assert should_writeback_chat_message("刚跟陈教授约好下周二早九点签字，怎么发确认消息好？")
    assert should_writeback_chat_message("陈经理今天周五17:45又发邮件临时加需求了")
    assert should_writeback_chat_message(
        "记一下苏晚晴最近特别喜欢抹茶千层蛋糕，另外她有什么饮食忌口要注意？"
    )
    assert not should_writeback_chat_message("活着没意义想吃安眠药", SafetyCategory.CRISIS)


@pytest.mark.asyncio
async def test_r2_defect2_live_streaming_xml_override_and_json_fallback_zero_leak(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    from collections.abc import AsyncIterator

    from mindweave.ai.adapters.base import AdapterCallRequest, AdapterCallResponse, ProviderAdapter
    from mindweave.ai.schemas.contracts import CompanionChatInput
    from mindweave.core.config import Settings
    from mindweave.modules.companion.domain.output_guard import StreamingReplyExtractor

    class _SpyStreamingJsonAdapter(ProviderAdapter):
        provider_name = "gemini"

        def __init__(self, chunks: list[str]) -> None:
            self.recorded_stream_prompts: list[str] = []
            self.recorded_generate_prompts: list[str] = []
            self._chunks = chunks

        async def complete(self, request: AdapterCallRequest) -> AdapterCallResponse:
            self.recorded_generate_prompts.append(request.prompt)
            return AdapterCallResponse(
                text=json.dumps(
                    {
                        "safety_flag": "normal",
                        "reply_text": "非流式回复",
                        "whisper_note": "提示",
                        "cited_record_ids": [],
                        "has_unrecorded_gap": False,
                        "alternative_perspectives": [],
                        "actionable_suggestions": [],
                        "disclaimer": None,
                    },
                    ensure_ascii=False,
                ),
                model=request.model,
                input_tokens=10,
                output_tokens=10,
            )

        async def stream(self, request: AdapterCallRequest) -> AsyncIterator[str]:
            self.recorded_stream_prompts.append(request.prompt)
            for chunk in self._chunks:
                yield chunk

    # 1. ModelGateway.stream_companion_reply appends XML wire-format override ONLY on stream path
    spy = _SpyStreamingJsonAdapter(["<reply>你好</reply>"])
    gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="gemini", ai_fallback_provider="none"),
        adapters={"gemini": spy},
    )
    scope = UserScope(new_id())
    gold_payload = CompanionChatInput(
        person_id=new_id(),
        person_name="妈妈",
        tier=Tier.GOLD,
        user_message="最近要留意什么？",
    )
    silver_payload = CompanionChatInput(
        person_id=new_id(),
        person_name="陈经理",
        tier=Tier.SILVER,
        user_message="整理时间线",
    )
    _ = [c async for c in gw.stream_companion_reply(scope=scope, payload=gold_payload)]
    _ = [c async for c in gw.stream_companion_reply(scope=scope, payload=silver_payload)]
    await gw.generate_companion_reply(scope=scope, payload=gold_payload)

    assert len(spy.recorded_stream_prompts) == 2
    assert "<reply>" in spy.recorded_stream_prompts[0]
    assert "严禁" in spy.recorded_stream_prompts[0]
    assert "<perspectives>" in spy.recorded_stream_prompts[0]
    assert "严禁**输出 `<perspectives>`" in spy.recorded_stream_prompts[1]
    assert "流式输出协议覆盖指令" not in spy.recorded_generate_prompts[0]

    # 2. StreamingReplyExtractor incremental JSON `"reply_text"` parser across 1, 2, 3, 7 char splits
    raw_json_variants = [
        (
            '{"meta":{"reply_text":"DECOY_NESTED"},"safety_flag":"normal",'
            '"reply_text":"妈妈想要\\"浅驼色\\"围巾\\n\\u4f60\\u597d\\uD83D\\uDE0A",'
            '"whisper_note":"私密提示","actionable_suggestions":["「妈，给您挑了条围巾～」"]}'
        ),
        (
            '```json\n{"safety_flag": "normal", '
            '"reply_text": "妈妈想要\\"浅驼色\\"围巾\\n\\u4f60\\u597d\\uD83D\\uDE0A", '
            '"whisper_note": "私密提示"}\n```'
        ),
        (
            '```JSON\r\n{"safety_flag":"normal",'
            '"reply_text":"妈妈想要\\"浅驼色\\"围巾\\n\\u4f60\\u597d\\uD83D\\uDE0A"}\r\n```'
        ),
    ]
    expected_reply = '妈妈想要"浅驼色"围巾\n你好😊'
    for raw_payload in raw_json_variants:
        for chunk_size in (1, 2, 3, 7, 100):
            ext = StreamingReplyExtractor()
            pieces = [
                ext.feed(raw_payload[i : i + chunk_size])
                for i in range(0, len(raw_payload), chunk_size)
            ]
            pieces.append(ext.finalize())
            got = "".join(pieces)
            assert got == expected_reply
            assert ext.used_json_fallback is True
            assert "{" not in got and "reply_text" not in got and "DECOY_NESTED" not in got
            parsed = ext.parsed_json_payload()
            assert parsed is not None
            assert parsed["safety_flag"] == "normal"

    # Truncated JSON never leaks raw braces or keys
    trunc_ext = StreamingReplyExtractor()
    trunc_out = trunc_ext.feed('{"safety_flag":"normal","reply_te') + trunc_ext.finalize()
    assert trunc_out == ""

    # 3. End-to-end SSE test: live adapter stubbornly emitting raw JSON chunks NEVER leaks JSON to SSE delta
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    user_id, token, dek = await _provision_user(db_container)
    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    rec_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="围巾心愿",
        body="去年冬至摸着浅驼色羊绒围巾说真暖和",
        fact_type=FactType.WISH,
        fact_content="想要浅驼色羊绒围巾",
        happened_on=date(2025, 12, 21),
    )
    raw_live_json = json.dumps(
        {
            "safety_flag": "normal",
            "reply_text": f"去年冬至妈妈说过喜欢浅驼色羊绒围巾 [rec_{rec_id}]。",
            "whisper_note": "已核对冬至围巾心愿",
            "cited_record_ids": [str(rec_id)],
            "has_unrecorded_gap": False,
            "alternative_perspectives": [],
            "actionable_suggestions": ["「妈，天凉了给您挑了条软软的浅驼色羊绒围巾～」"],
            "disclaimer": None,
        },
        ensure_ascii=False,
    )
    chunked_live = [raw_live_json[i : i + 3] for i in range(0, len(raw_live_json), 3)]
    live_gw = ModelGateway(
        Settings(environment="test", ai_primary_provider="gemini", ai_fallback_provider="none"),
        adapters={"gemini": _SpyStreamingJsonAdapter(chunked_live)},
    )
    orig_gw = db_container.extras.get("model_gateway")
    db_container.extras["model_gateway"] = live_gw
    try:
        app = create_app(container=db_container)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            sse_resp = await client.post(
                f"/v1/persons/{mom_id}/chat/stream",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "r2-json-leak"},
                json={"message": "妈妈想要什么围巾？"},
            )
        assert sse_resp.status_code == 200
        frames = _parse_sse_frames(sse_resp.text)
        delta_text = "".join(p["text"] for n, p in frames if n == "delta")
        assert "浅驼色羊绒围巾" in delta_text
        assert "{" not in delta_text
        assert '"reply_text"' not in delta_text
        assert '"safety_flag"' not in delta_text
        whispers = [p["text"] for n, p in frames if n == "whisper"]
        assert whispers == ["已核对冬至围巾心愿"]
        suggs = [p["items"] for n, p in frames if n == "suggestion"]
        assert suggs == [["「妈，天凉了给您挑了条软软的浅驼色羊绒围巾～」"]]
    finally:
        if orig_gw is None:
            db_container.extras.pop("model_gateway", None)
        else:
            db_container.extras["model_gateway"] = orig_gw


@pytest.mark.asyncio
async def test_r2_defect3_offline_streaming_xml_supplements_and_copyable_drafts() -> None:
    from mindweave.ai.adapters.base import AdapterCallRequest
    from mindweave.ai.schemas.contracts import CompanionChatInput, ContextFactItem
    from mindweave.modules.companion.domain.tier_policy import derive_stream_supplements

    adapter = FakeAdapter()
    forbidden_meta = (
        "结合关于",
        "顺着这些",
        "最省心",
        "守住边界：",
        "按时间线留存客观沟通记录",
        "对照",
    )

    # 1. Gold conflict streams <reply>, <whisper>, <perspectives>, and real copyable <suggestions>
    gold_conflict_in = CompanionChatInput(
        person_id=new_id(),
        person_name="林屿",
        tier=Tier.GOLD,
        confirmed_facts=[
            ContextFactItem(
                record_id=new_id(),
                fact_id=new_id(),
                fact_type=FactType.EVENT,
                content="最近连续高强度评审会议偏头痛发作",
            )
        ],
        user_message="今天带蛋糕给他却冷脸没理我，好委屈",
    )
    gold_stream = "".join(
        [
            c
            async for c in adapter.stream(
                AdapterCallRequest(
                    task="ask_relationship",
                    model="fake",
                    prompt="",
                    structured_context={"payload": gold_conflict_in.model_dump(mode="json")},
                )
            )
        ]
    )
    assert "<reply>" in gold_stream and "</reply>" in gold_stream
    assert "<perspectives>" in gold_stream and "</perspectives>" in gold_stream
    assert "<suggestions>" in gold_stream and "</suggestions>" in gold_stream
    for bad in forbidden_meta:
        assert bad not in gold_stream

    # 2. Silver tier streams <reply>, <suggestions>, NEVER <perspectives>, and copyable drafts
    silver_in = CompanionChatInput(
        person_id=new_id(),
        person_name="陈经理",
        tier=Tier.SILVER,
        confirmed_facts=[
            ContextFactItem(
                record_id=new_id(),
                fact_id=new_id(),
                fact_type=FactType.EVENT,
                content="周五17:45发邮件临时加需求",
            )
        ],
        user_message="按时间线帮我整理陈经理临时加需求的事实",
    )
    silver_stream = "".join(
        [
            c
            async for c in adapter.stream(
                AdapterCallRequest(
                    task="ask_relationship",
                    model="fake",
                    prompt="",
                    structured_context={"payload": silver_in.model_dump(mode="json")},
                )
            )
        ]
    )
    assert "<reply>" in silver_stream and "</reply>" in silver_stream
    assert "<suggestions>" in silver_stream
    assert "<perspectives>" not in silver_stream
    assert "「" in silver_stream
    for bad in forbidden_meta:
        assert bad not in silver_stream

    # 3. V2 derive_stream_supplements NEVER fabricates canned suggestions or kitsch whispers;
    # it accurately flags has_unrecorded_gap while deferring genuine suggestions to the LLM stream.
    for tier_enum in (Tier.GOLD, Tier.SILVER, Tier.CRYSTAL):
        ci = CompanionChatInput(
            person_id=new_id(),
            person_name="妈妈",
            tier=tier_enum,
            confirmed_facts=[
                ContextFactItem(
                    record_id=new_id(),
                    fact_id=new_id(),
                    fact_type=FactType.PREFERENCE,
                    content="喜欢桂花乌龙茶",
                )
            ],
            user_message="聊聊近况",
        )
        sup = derive_stream_supplements(
            ci, cleaned_reply="记着她喜欢桂花乌龙茶。", verified_record_ids=[]
        )
        assert sup.has_unrecorded_gap is False
        assert sup.whisper_note is None
        assert sup.actionable_suggestions == []

        sup_gap = derive_stream_supplements(
            ci,
            cleaned_reply="目前还没有记录过这项具体信息。",
            verified_record_ids=[],
        )
        assert sup_gap.has_unrecorded_gap is True
        assert sup_gap.whisper_note is None
        assert sup_gap.actionable_suggestions == []


@pytest.mark.asyncio
async def test_r2_defect4_venting_held_deleted_rejected_superseded_reminders_excluded(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    from mindweave.modules.companion.infra.context_builder import ContextBuilder
    from mindweave.shared.enums import PendingReason
    from mindweave.shared.scopes import PersonScope

    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    app = create_app(container=db_container)
    user_id, token, dek = await _provision_user(db_container)
    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )

    # 1. Valid confirmed fact-backed reminder (MUST be included)
    _, valid_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="骨科复查",
        body="11月28日骨科复查",
        fact_type=FactType.DATE,
        fact_content="11月28日骨科复查",
        trigger_date=date(2026, 11, 28),
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="合法复查提醒",
        once_date=date(2026, 11, 28),
        fact_id=valid_fact_id,
    )
    # 2. Standalone manual reminder with fact_id=None (MUST be included)
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="独立手动提醒",
        once_date=date(2026, 11, 29),
        fact_id=None,
    )
    # 3. Venting fact-backed reminder (MUST be excluded)
    _, venting_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="气话记录",
        body="气话里随口说再也不去了",
        fact_type=FactType.DATE,
        fact_content="气话日期",
        fact_status=FactStatus.PENDING,
    )
    assert venting_fact_id is not None
    async with db_container.db.user_session(UserScope(user_id)) as sess:
        vf = await sess.get(FactRow, venting_fact_id)
        assert vf is not None
        vf.pending_reason = PendingReason.VENTING.value
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="绝不该泄露的气话提醒",
        once_date=date(2026, 11, 25),
        fact_id=venting_fact_id,
    )
    # 4. Held record's fact-backed reminder (MUST be excluded)
    _, held_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="冷静区暂存",
        body="冷静区暂存内容",
        fact_type=FactType.DATE,
        fact_content="冷静区日期",
        review_state=RecordReviewState.HELD,
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="绝不该泄露的冷静区提醒",
        once_date=date(2026, 11, 25),
        fact_id=held_fact_id,
    )
    # 5. Soft-deleted record's fact-backed reminder (MUST be excluded)
    _, del_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="已软删记录",
        body="已软删内容",
        fact_type=FactType.DATE,
        fact_content="已软删日期",
        deleted_at=datetime(2026, 11, 20, tzinfo=UTC),
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="绝不该泄露的软删提醒",
        once_date=date(2026, 11, 25),
        fact_id=del_fact_id,
    )
    # 6. Rejected fact-backed reminder (MUST be excluded)
    _, rej_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="被拒绝事实",
        body="已拒绝内容",
        fact_type=FactType.DATE,
        fact_content="已拒绝日期",
        fact_status=FactStatus.REJECTED,
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="绝不该泄露的已拒绝提醒",
        once_date=date(2026, 11, 25),
        fact_id=rej_fact_id,
    )
    # 7. Superseded (`invalid_at IS NOT NULL`) fact-backed reminder (MUST be excluded)
    _, sup_fact_id = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="已覆盖旧事实",
        body="旧日期",
        fact_type=FactType.DATE,
        fact_content="旧日期",
    )
    assert sup_fact_id is not None
    async with db_container.db.user_session(UserScope(user_id)) as sess:
        sf = await sess.get(FactRow, sup_fact_id)
        assert sf is not None
        sf.invalid_at = datetime(2026, 11, 21, tzinfo=UTC)
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="绝不该泄露的已覆盖提醒",
        once_date=date(2026, 11, 25),
        fact_id=sup_fact_id,
    )

    # Verify ContextBuilder.build() strictly excludes all 5 invalid reminders
    cb = ContextBuilder(
        cipher=db_container.cipher,
        default_timezone="Asia/Shanghai",
    )
    async with db_container.db.user_session(UserScope(user_id)) as sess:
        built = await cb.build(
            sess,
            scope=PersonScope(user_id=user_id, person_id=mom_id),
            dek=dek,
            user_message="妈妈最近有什么日程？",
        )
    assert built.active_reminders_count == 2
    built_titles = {r["title"] for r in built.upcoming_reminders}
    assert built_titles == {"合法复查提醒", "独立手动提醒"}

    # Verify GET /v1/people/{id}/brief strictly excludes all 5 invalid reminders
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        brief_resp = await client.get(
            f"/v1/people/{mom_id}/brief",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert brief_resp.status_code == 200
    brief = brief_resp.json()
    assert brief["upcomingCount"] == 2
    brief_blob = json.dumps(brief, ensure_ascii=False)
    assert "绝不该泄露" not in brief_blob
    assert "合法复查提醒" in brief_blob


@pytest.mark.asyncio
async def test_r2_defect5_6_proposal_restraint_d4_precision_and_title_body_dedup(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    app = create_app(container=db_container)
    user_id, token, dek = await _provision_user(db_container)

    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    chen_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="陈经理", tier=Tier.SILVER, role_tag="上司"
    )

    rec_scarf, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="冬至橱窗围巾",
        body="去年冬至路过橱窗她摸着浅驼色羊绒围巾说软软的真暖和",
        fact_type=FactType.WISH,
        fact_content="想要浅驼色羊绒围巾",
        happened_on=date(2025, 12, 21),
    )
    rec_checkup, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="右膝盖复查安排",
        body="11月28日上午陪妈妈去市一院骨科复查右膝盖，记得带医保卡和旧片子",
        fact_type=FactType.DATE,
        fact_content="11月28日骨科复查右膝盖",
        trigger_date=date(2026, 11, 28),
        happened_on=date(2026, 11, 16),
    )
    # Decoy record containing stop-word "提醒" AND overlapping title/body!
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="对香菜过敏要提醒免香菜",
        body="对香菜过敏，点菜时记得提醒免香菜",
        fact_type=FactType.TABOO,
        fact_content="对香菜过敏要提醒免香菜",
        happened_on=date(2026, 10, 5),
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="茶饮偏好",
        body="平时下午爱在阳台喝桂花乌龙茶",
        fact_type=FactType.PREFERENCE,
        fact_content="平时爱喝桂花乌龙茶",
        happened_on=date(2026, 10, 12),
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="陪妈妈去市一院做右膝盖骨科复查",
        once_date=date(2026, 11, 28),
    )

    # Silver record with duplicate leading date `[2026-10-18] 2026年10月18日...`
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=chen_id,
        dek=dek,
        title="周日临时改需求",
        body="[2026-10-18] 2026年10月18日周日晚十一点陈经理在群里要求周一早八点前重写方案，且拒绝调整排期。",
        fact_type=FactType.EVENT,
        fact_content="2026年10月18日周日晚临时要求重写方案并拒绝调排期",
        happened_on=date(2026, 10, 18),
    )

    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Exact judge scarf + checkup query: high precision (<= 3 hits, NO cilantro leak!),
        #    NO writebackQueued, real sendable drafts, and 1-tap reminder proposal for 2026-11-27
        q_resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "r2-scarf-checkup"},
            json={
                "message": "去年冬至妈妈说想要什么围巾？另外11月28日骨科复查帮我提前设个提醒准备一下"
            },
        )
        assert q_resp.status_code == 200
        q_frames = _parse_sse_frames(q_resp.text)
        q_delta = "".join(p["text"] for n, p in q_frames if n == "delta")
        q_cits = {p["materialId"] for n, p in q_frames if n == "citation"}
        q_done = next(p for n, p in q_frames if n == "done")
        q_props = [p for n, p in q_frames if n == "action_proposal"]
        q_suggs = [item for n, p in q_frames if n == "suggestion" for item in p["items"]]

        assert str(rec_scarf) in q_cits
        assert str(rec_checkup) in q_cits
        assert len(q_cits) <= 3
        assert "香菜" not in q_delta
        assert "writebackQueued" not in q_done
        assert len(q_props) == 1 and q_props[0]["triggerDate"] == "2026-11-27"
        assert q_suggs and all(s.startswith("「") for s in q_suggs)

        # 2. Direct cilantro allergy query -> NO duplicated `对香菜过敏要提醒免香菜：对香菜过敏`
        c_resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "r2-cilantro-dedup"},
            json={"message": "妈妈对什么过敏？"},
        )
        assert c_resp.status_code == 200
        c_delta = "".join(p["text"] for n, p in _parse_sse_frames(c_resp.text) if n == "delta")
        assert "香菜" in c_delta
        assert "对香菜过敏要提醒免香菜：对香菜过敏" not in c_delta

        # 3. Unrecorded gap query (`穿几码鞋`) -> ZERO action_proposal even with upcoming reminder in 5d!
        gap_resp = await client.post(
            f"/v1/persons/{mom_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "r2-unrecorded-gap"},
            json={"message": "妈妈平时穿几码鞋？"},
        )
        assert gap_resp.status_code == 200
        gap_frames = _parse_sse_frames(gap_resp.text)
        assert [p for n, p in gap_frames if n == "action_proposal"] == []

        # 4. Non-planning / bare single-char `带|买|送` queries -> ZERO spurious action_proposal!
        for idx_q, non_plan_msg in enumerate(
            [
                "妈妈平时爱喝什么茶？",
                "她平时出门带什么保温杯？",
                "她之前买过什么牌子的茶？",
                "去年她送过我什么书？",
            ]
        ):
            np_resp = await client.post(
                f"/v1/persons/{mom_id}/chat/stream",
                headers={**headers, "Idempotency-Key": f"r2-nonplan-{idx_q}"},
                json={"message": non_plan_msg},
            )
            assert np_resp.status_code == 200
            np_props = [p for n, p in _parse_sse_frames(np_resp.text) if n == "action_proposal"]
            assert np_props == [], f"Spurious proposal on `{non_plan_msg}`: {np_props}"

        # 5. Silver save_note strips `[2026-10-18] 2026年10月18日` duplication into clean natural `• 10月18日：...`
        s_resp = await client.post(
            f"/v1/persons/{chen_id}/chat/stream",
            headers={**headers, "Idempotency-Key": "r2-silver-clean-note"},
            json={"message": "按时间线帮我理清陈经理临时改需求的客观经过"},
        )
        assert s_resp.status_code == 200
        s_props = [
            p
            for n, p in _parse_sse_frames(s_resp.text)
            if n == "action_proposal" and p["kind"] == "save_note"
        ]
        assert len(s_props) == 1
        assert s_props[0]["title"] == "陈经理关键时间线留痕"
        assert s_props[0]["body"].startswith("• 10月18日：")
        assert "[2026-10-18]" not in s_props[0]["body"]
        assert "2026年10月18日" not in s_props[0]["body"]


@pytest.mark.asyncio
async def test_r2_defect7_polish_step2_labels_brief_date_dedup_and_openapi_schema(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    from mindweave.ai.schemas.contracts import CompanionChatInput
    from mindweave.modules.companion.application.service import _build_agent_step_events
    from mindweave.modules.companion.infra.context_builder import BuiltCompanionContext
    from mindweave.shared.enums import PersonState
    from mindweave.shared.scopes import PersonScope

    def _make_ctx(facts_cnt: int, rem_cnt: int) -> BuiltCompanionContext:
        uid = new_id()
        pid = new_id()
        return BuiltCompanionContext(
            scope=PersonScope(user_id=uid, person_id=pid),
            person_name="妈妈",
            tier=Tier.GOLD,
            person_state=PersonState.ACTIVE,
            role_tag="家人",
            traits=[],
            chat_input=CompanionChatInput(
                person_id=pid,
                person_name="妈妈",
                tier=Tier.GOLD,
                user_message="你好",
            ),
            allowed_record_ids=frozenset(),
            record_catalog={},
            timezone="Asia/Shanghai",
            late_night_care_enabled=True,
            total_records_scanned=4,
            confirmed_facts_count=facts_cnt,
            active_reminders_count=rem_cnt,
            recalled_records_count=1,
            recalled_dates=["2026-11-16"],
        )

    # 1. Step 2 cleanly adapts to facts-only / reminders-only / both without zero-telemetry counts
    steps_facts_only = _build_agent_step_events(_make_ctx(facts_cnt=3, rem_cnt=0))
    assert steps_facts_only[1]["label"] == "正在整理相关细节…"
    assert "0 项日程" not in steps_facts_only[1]["label"]

    steps_rems_only = _build_agent_step_events(_make_ctx(facts_cnt=0, rem_cnt=2))
    assert steps_rems_only[1]["label"] == "正在查看近期日程…"
    assert "0 条" not in steps_rems_only[1]["label"]

    steps_neither = _build_agent_step_events(_make_ctx(facts_cnt=0, rem_cnt=0))
    assert [s["phase"] for s in steps_neither] == ["scan", "recall"]

    steps_both = _build_agent_step_events(_make_ctx(facts_cnt=2, rem_cnt=1))
    assert steps_both[1]["label"] == "正在整理相关细节与日程…"

    # 2. Brief prompt & item actionPrompt never duplicate date (`12月6日12月6日...`)
    manual_clock.set(datetime(2026, 11, 25, 2, 0, 0, tzinfo=UTC))
    app = create_app(container=db_container)
    user_id, token, dek = await _provision_user(db_container)
    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    await _seed_reminder(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="12月6日妈妈生日聚餐",
        once_date=date(2026, 12, 6),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        brief_resp = await client.get(
            f"/v1/people/{mom_id}/brief",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert brief_resp.status_code == 200
    brief = brief_resp.json()
    assert brief["prompt"].count("12月6日") == 1
    assert "12月6日12月6日" not in brief["prompt"]
    rem_item = next(it for it in brief["items"] if it["kind"] == "reminder")
    assert rem_item["actionPrompt"].count("12月6日") == 1
    assert "12月6日12月6日" not in rem_item["actionPrompt"]

    # 3. Canonical `/v1/people/{person_id}/brief` is exposed in OpenAPI schema while `/persons/` alias stays hidden
    openapi_paths = app.openapi()["paths"]
    assert "/v1/people/{person_id}/brief" in openapi_paths
    assert "get" in openapi_paths["/v1/people/{person_id}/brief"]
    assert "/v1/persons/{person_id}/brief" not in openapi_paths
