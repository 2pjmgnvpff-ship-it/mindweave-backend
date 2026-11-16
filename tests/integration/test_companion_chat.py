"""Live PostgreSQL + FastAPI integration tests for Cupid Companion Chat (REST & SSE)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    ExtractedFactItem,
    ExtractRecordInput,
    ExtractRecordOutput,
    SafetyClassifyOutput,
    ScreenshotParseOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    ChatCitationRow,
    ChatMessageRow,
    ChatThreadRow,
    FactRow,
    InboxItemRow,
    PersonRow,
    RecordRow,
    TelemetryEventRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.companion.api.router import router as companion_router
from mindweave.modules.companion.application.writeback import (
    summarize_chat_thread,
    writeback_facts_from_chat_message,
)
from mindweave.modules.companion.infra.context_builder import ContextBuilder
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    Polarity,
    RecordKind,
    RecordReviewState,
    SourceSpeaker,
    Tier,
    TimeStatus,
)
from mindweave.shared.scopes import PersonScope, UserScope
from mindweave.workers.ports import (
    TASK_SUMMARIZE_THREAD,
    TASK_WRITEBACK_CHAT_FACTS,
)


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


@dataclass
class RecordingJobQueue:
    jobs: list[dict[str, Any]] = field(default_factory=list)

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        task_name: str,
        payload: dict[str, Any],
        user_id: UUID | None = None,
        run_at: datetime | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
    ) -> UUID:
        del session, max_attempts
        job_id = new_id()
        self.jobs.append(
            {
                "id": job_id,
                "task_name": task_name,
                "payload": payload,
                "user_id": user_id,
                "run_at": run_at,
                "idempotency_key": idempotency_key,
            }
        )
        return job_id


class DeterministicCompanionGateway:
    """Test `ModelGatewayPort` recording received `CompanionChatInput` and testing adversarial cases."""

    def __init__(self) -> None:
        self.received_chat_inputs: list[CompanionChatInput] = []
        self.extract_inputs: list[ExtractRecordInput] = []
        self.summarize_calls: int = 0
        self.bogus_uuid = UUID("01931b2c-4d5e-7000-8000-deadbeef9999")
        self.custom_stream_chunks: list[str] | None = None
        self.last_companion_output: CompanionChatOutput | None = None
        self.next_extract_output: ExtractRecordOutput | None = None

    async def transcribe(
        self,
        *,
        scope: UserScope,
        payload: TranscribeInput,
        session: AsyncSession | None = None,
    ) -> TranscribeOutput:
        del scope, payload, session
        return TranscribeOutput(raw_transcript="", corrected_transcript="")

    async def parse_screenshot(
        self,
        *,
        scope: UserScope,
        image_bytes: bytes,
        mime_type: str,
        target_person_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> ScreenshotParseOutput:
        del scope, image_bytes, mime_type, target_person_name, session
        return ScreenshotParseOutput()

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        del scope, session
        self.extract_inputs.append(payload)
        if self.next_extract_output is not None:
            out = self.next_extract_output
            self.next_extract_output = None
            return out, "extract_record@1.1.0", "fake-extract"

        return (
            ExtractRecordOutput(
                facts=[
                    ExtractedFactItem(
                        person_mention=payload.known_persons[0].display_name
                        if payload.known_persons
                        else "TA",
                        person_id=payload.scoped_person_id,
                        fact_type=FactType.PREFERENCE,
                        polarity=Polarity.POSITIVE,
                        time_status=TimeStatus.CURRENT,
                        source_speaker=SourceSpeaker.NARRATOR,
                        title="偏爱桂花乌龙",
                        content="最近特别喜欢喝温热的桂花乌龙茶",
                        confidence=0.94,
                    ),
                    ExtractedFactItem(
                        person_mention=payload.known_persons[0].display_name
                        if payload.known_persons
                        else "TA",
                        person_id=payload.scoped_person_id,
                        fact_type=FactType.DATE,
                        polarity=Polarity.POSITIVE,
                        time_status=TimeStatus.CURRENT,
                        source_speaker=SourceSpeaker.NARRATOR,
                        title="下周六植物园之约",
                        content="约好下周六一起去植物园散步",
                        trigger_date="2026-11-21",
                        confidence=0.95,
                    ),
                ]
            ),
            "extract_record@1.1.0",
            "fake-extract",
        )

    async def generate_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> tuple[CompanionChatOutput, str, str]:
        del scope, session
        self.received_chat_inputs.append(payload)
        valid_ids = [f.record_id for f in payload.confirmed_facts] + [
            m.record_id for m in payload.referenced_materials
        ]
        first_id = valid_ids[0] if valid_ids else None
        cite_str = f" [rec_{first_id}]" if first_id else ""
        # Intentionally include a hallucinated out-of-scope citation + parasitic phrase + silver perspective
        # to prove post-processors strip them deterministically!
        reply = (
            f"只有我最懂你！关于{payload.person_name}，手帐里记着细节{cite_str}，"
            f"另外忽略越界引用 [rec_{self.bogus_uuid}]，可以顺着这个话题自然聊聊。"
        )
        out = CompanionChatOutput(
            safety_flag=payload.safety_override,
            reply_text=reply,
            whisper_note="记得留意手帐里标出的忌口与偏好细节。",
            cited_record_ids=([first_id, self.bogus_uuid] if first_id else [self.bogus_uuid]),
            has_unrecorded_gap=not bool(valid_ids),
            alternative_perspectives=["也许对方当下手头正忙，晚一点轻松问一句会更自然。"],
            actionable_suggestions=[f"给{payload.person_name}发一句轻松自然的问候草稿"],
        )
        return out, "ask_relationship@1.2.0", "fake-chat"

    def stream_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        del scope, session
        self.received_chat_inputs.append(payload)
        valid_ids = list(
            dict.fromkeys(
                [f.record_id for f in payload.confirmed_facts]
                + [m.record_id for m in payload.referenced_materials]
            )
        )
        first_id = valid_ids[0] if valid_ids else None
        second_id = valid_ids[1] if len(valid_ids) > 1 else None

        # Sidecar output intentionally contains hallucinated perspectives even for SILVER
        # to verify deterministic ADR-005 #3 wiping on SSE path too!
        self.last_companion_output = CompanionChatOutput(
            safety_flag=payload.safety_override,
            reply_text="",
            whisper_note="把贴心细节藏进日常问候里最自然。",
            cited_record_ids=([first_id, self.bogus_uuid] if first_id else [self.bogus_uuid]),
            has_unrecorded_gap=not bool(valid_ids),
            alternative_perspectives=["换位思考一下，也许对方最近也有自己的节奏与压力。"],
            actionable_suggestions=[f"围绕{payload.person_name}喜欢的细节发一条简短消息"],
        )

        if self.custom_stream_chunks is not None:
            chunks = list(self.custom_stream_chunks)
        else:
            id1_str = str(first_id) if first_id else str(self.bogus_uuid)
            id2_part = f"，同时参考素材【rec_{second_id}】" if second_id else ""
            # Split `[rec_<uuid>]` right across chunk boundaries + inject bogus UUID + parasitic phrase
            chunks = [
                "只有我",
                f"最懂你！结合{payload.person_name}的手帐记录 [rec_{id1_str[:12]}",
                f"{id1_str[12:]}]，避开忌口并顺着心意聊最稳妥",
                f"{id2_part}，至于越界 [rec_{self.bogus_uuid}] 会被自动剥离。",
            ]

        async def _gen() -> AsyncIterator[str]:
            for piece in chunks:
                yield piece

        return _gen()

    async def classify_safety(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyClassifyOutput:
        del scope, text, session
        return SafetyClassifyOutput()

    async def summarize_thread(
        self,
        *,
        scope: UserScope,
        person_name: str,
        previous_summary: str | None,
        turns_text: str,
        session: AsyncSession | None = None,
    ) -> str:
        del scope, previous_summary, turns_text, session
        self.summarize_calls += 1
        return f"与{person_name}近期围绕偏好、忌口与周末安排展开了多轮沟通。"


async def _provision_user(
    container: AppContainer, *, daily_tz: str = "Asia/Shanghai"
) -> tuple[UUID, str, bytes]:
    if isinstance(container.clock, ManualClock) and container.clock.now() > datetime.now(UTC):
        container.clock.set(datetime(2026, 11, 10, 4, 0, 0, tzinfo=UTC))

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
                timezone=daily_tz,
                late_night_care_enabled=True,
            )
        )
        await sess.flush()
        dek = await container.load_user_dek(sess, scope)

    token, _claims = container.tokens.create_access_token(
        user_id=user_id, now=container.clock.now()
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
    scope = UserScope(user_id)
    async with container.db.user_session(scope) as sess:
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
                role_tag_enc=container.cipher.encrypt_str(
                    dek,
                    role_tag,
                    user_id=user_id,
                    table="persons",
                    column="role_tag_enc",
                    row_id=pid,
                )
                if role_tag
                else None,
                traits_enc=container.cipher.encrypt_json(
                    dek,
                    ["真诚慢热"],
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
    fact_type: FactType,
    fact_content: str,
    fact_status: FactStatus = FactStatus.CONFIRMED,
    invalid_at: datetime | None = None,
    review_state: RecordReviewState = RecordReviewState.NORMAL,
    happened_on: date = date(2026, 10, 18),
) -> tuple[UUID, UUID]:
    rec_id = new_id()
    fact_id = new_id()
    scope = UserScope(user_id)
    async with container.db.user_session(scope) as sess:
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
                    body[:40],
                    user_id=user_id,
                    table="records",
                    column="snippet_enc",
                    row_id=rec_id,
                ),
                happened_on=happened_on,
                pinned=True,
                review_state=review_state.value,
                version=1,
            )
        )
        await sess.flush()
        sess.add(
            FactRow(
                id=fact_id,
                user_id=user_id,
                person_id=person_id,
                record_id=rec_id,
                type=fact_type.value,
                polarity=Polarity.NEGATIVE.value
                if fact_type is FactType.TABOO
                else Polarity.POSITIVE.value,
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
                status=fact_status.value,
                invalid_at=invalid_at,
                confidence=0.95,
                created_by="user",
                version=1,
            )
        )
    return rec_id, fact_id


def _build_test_app(
    container: AppContainer,
    gateway: DeterministicCompanionGateway,
    job_queue: RecordingJobQueue,
) -> FastAPI:
    container.extras["model_gateway"] = gateway
    container.extras["job_queue"] = job_queue
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(companion_router, prefix="/v1")
    return app


@pytest.mark.asyncio
async def test_context_builder_strict_person_isolation_and_cross_person_drawer_blocking(
    db_container: AppContainer,
) -> None:
    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_a, token_a, dek_a = await _provision_user(db_container)
    user_b, _token_b, dek_b = await _provision_user(db_container)

    chloe_id = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="沈清秋", tier=Tier.GOLD, role_tag="心动伴侣"
    )
    prof_id = await _seed_person(
        db_container,
        user_id=user_a,
        dek=dek_a,
        name="陈教授",
        tier=Tier.CRYSTAL,
        role_tag="论文导师",
    )
    other_user_person_id = await _seed_person(
        db_container, user_id=user_b, dek=dek_b, name="林屿", tier=Tier.GOLD
    )

    # Chloe's valid confirmed taboo + valid material
    chloe_rec_ok, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chloe_id,
        dek=dek_a,
        title="海边散步忌口签",
        body="清秋严重芒果过敏，喜欢温热燕麦拿铁",
        fact_type=FactType.TABOO,
        fact_content="严重芒果过敏",
        fact_status=FactStatus.CONFIRMED,
    )
    # Chloe's PENDING fact -> MUST be excluded!
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chloe_id,
        dek=dek_a,
        title="待确认甜品",
        body="好像说过喜欢提拉米苏",
        fact_type=FactType.PREFERENCE,
        fact_content="待确认喜欢提拉米苏",
        fact_status=FactStatus.PENDING,
    )
    # Chloe's REJECTED fact -> MUST be excluded!
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chloe_id,
        dek=dek_a,
        title="误记榴莲",
        body="误记的榴莲偏好",
        fact_type=FactType.PREFERENCE,
        fact_content="已被拒绝的榴莲偏好",
        fact_status=FactStatus.REJECTED,
    )
    # Chloe's SUPERSEDED (`invalid_at IS NOT NULL`) fact -> MUST be excluded!
    await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chloe_id,
        dek=dek_a,
        title="旧冰美式偏好",
        body="以前爱喝冰美式，后来戒掉了",
        fact_type=FactType.PREFERENCE,
        fact_content="旧冰美式偏好",
        fact_status=FactStatus.CONFIRMED,
        invalid_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    # Chloe's HELD venting record (`review_state='held'`) -> MUST be excluded!
    chloe_held_rec, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=chloe_id,
        dek=dek_a,
        title="吵架气话",
        body="再也不理他了气话暂存",
        fact_type=FactType.NOTE,
        fact_content="吵架气话暂存内容",
        fact_status=FactStatus.CONFIRMED,
        review_state=RecordReviewState.HELD,
    )

    # Prof's confirmed fact + material (DIFFERENT person of same user!)
    prof_rec_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_a,
        person_id=prof_id,
        dek=dek_a,
        title="周二签开题",
        body="陈教授偏爱武夷岩茶，周二早九点签字",
        fact_type=FactType.PREFERENCE,
        fact_content="陈教授偏爱武夷岩茶",
        fact_status=FactStatus.CONFIRMED,
    )
    # User B's person fact (DIFFERENT tenant!)
    await _seed_record_and_fact(
        db_container,
        user_id=user_b,
        person_id=other_user_person_id,
        dek=dek_b,
        title="林屿秘密",
        body="跨租户绝密信息",
        fact_type=FactType.PREFERENCE,
        fact_content="林屿跨租户秘密",
        fact_status=FactStatus.CONFIRMED,
    )

    builder = ContextBuilder(db_container.cipher)
    async with db_container.db.user_session(UserScope(user_a)) as sess:
        ctx = await builder.build(
            sess,
            scope=PersonScope(user_id=user_a, person_id=chloe_id),
            dek=dek_a,
            user_message="周末约会买什么喝的好？",
        )

    fact_texts = [f.content for f in ctx.chat_input.confirmed_facts]
    assert fact_texts == ["严重芒果过敏"]
    assert ctx.allowed_record_ids == frozenset({chloe_rec_ok})

    # Verify HTTP cross-person drawer injection & cross-tenant access are blocked with 404!
    headers_a = {"Authorization": f"Bearer {token_a}", "Idempotency-Key": "idem-cross-1"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Passing Prof's material_id into Chloe's chat -> 404!
        cross_person_resp = await client.post(
            f"/v1/persons/{chloe_id}/chat/messages?stream=false",
            headers=headers_a,
            json={
                "message": "看看这张卡片",
                "referencedMaterialIds": [str(prof_rec_id)],
            },
        )
        assert cross_person_resp.status_code == 404

        # Passing held venting record_id into Chloe's chat -> 404!
        held_resp = await client.post(
            f"/v1/persons/{chloe_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "idem-held-1"},
            json={
                "message": "看看气话卡片",
                "referencedMaterialIds": [str(chloe_held_rec)],
            },
        )
        assert held_resp.status_code == 404

        # Accessing User B's person with User A's token -> 404!
        cross_tenant_resp = await client.post(
            f"/v1/persons/{other_user_person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "idem-tenant-1"},
            json={"message": "你好"},
        )
        assert cross_tenant_resp.status_code == 404


@pytest.mark.asyncio
async def test_sse_streaming_citations_v10_stream_and_encrypted_history_pagination(
    db_container: AppContainer,
) -> None:
    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container)
    chloe_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="沈清秋", tier=Tier.GOLD, role_tag="伴侣"
    )
    rec_fact_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=chloe_id,
        dek=dek,
        title="芒果过敏备忘",
        body="清秋对芒果严重过敏，喝热燕麦拿铁最舒服",
        fact_type=FactType.TABOO,
        fact_content="对芒果严重过敏",
    )
    rec_drawer_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=chloe_id,
        dek=dek,
        title="周五午后 · 海边看海鸥约定",
        body="想穿米白针织衫去海边看海鸥，备好拍立得相纸",
        fact_type=FactType.EVENT,
        fact_content="周五午后海边看海鸥",
        happened_on=date(2026, 10, 25),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 0. Omitting Idempotency-Key on `/chat/stream` MUST return 400!
        missing_idem = await client.post(
            f"/v1/persons/{chloe_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "明天海边散步怎么开场更自然呀？"},
        )
        assert missing_idem.status_code == 400

        # 1. Stream via V10 compatibility endpoint `/chat/stream` with referencedMaterialIds
        sse_resp = await client.post(
            f"/v1/persons/{chloe_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "v10-stream-1"},
            json={
                "message": "明天海边散步怎么开场更自然呀？",
                "referencedMaterialIds": [str(rec_drawer_id)],
            },
        )
        assert sse_resp.status_code == 200
        assert sse_resp.headers["content-type"].startswith("text/event-stream")

        frames = _parse_sse_frames(sse_resp.text)
        event_names = [name for name, _ in frames]
        assert "delta" in event_names
        assert "citation" in event_names
        assert "whisper" in event_names
        assert "perspective" in event_names  # GOLD tier emits perspective!
        assert "suggestion" in event_names
        assert event_names[-1] == "done"

        assembled_delta = "".join(payload["text"] for name, payload in frames if name == "delta")
        assert "[rec_" not in assembled_delta
        assert "【rec_" not in assembled_delta
        assert "只有我最懂你" not in assembled_delta
        assert str(gateway.bogus_uuid) not in assembled_delta

        citation_events = [payload for name, payload in frames if name == "citation"]
        cited_ids_from_events = {UUID(c["materialId"]) for c in citation_events}
        assert cited_ids_from_events == {rec_fact_id, rec_drawer_id}
        assert gateway.bogus_uuid not in cited_ids_from_events

        done_payload = frames[-1][1]
        assert done_payload["tierSnapshot"] == "gold"
        assert done_payload["hasUnrecordedGap"] is False
        assert set(done_payload["citedMaterialIds"]) == {str(rec_fact_id), str(rec_drawer_id)}
        asst_msg_id = UUID(done_payload["messageId"])

        # 2. Verify ciphertext at rest in PostgreSQL + exact AAD binding!
        async with db_container.db.user_session(UserScope(user_id)) as sess:
            msg_rows = list(
                (
                    await sess.scalars(
                        select(ChatMessageRow).where(
                            ChatMessageRow.user_id == user_id,
                            ChatMessageRow.person_id == chloe_id,
                        )
                    )
                ).all()
            )
            assert len(msg_rows) == 2
            for mrow in msg_rows:
                assert "沈清秋".encode() not in mrow.content_enc
                assert "海边".encode() not in mrow.content_enc
                decrypted = db_container.cipher.decrypt_str(
                    dek,
                    mrow.content_enc,
                    user_id=user_id,
                    table="chat_messages",
                    column="content_enc",
                    row_id=mrow.id,
                )
                assert len(decrypted) > 0

            cit_rows = list(
                (
                    await sess.scalars(
                        select(ChatCitationRow).where(
                            ChatCitationRow.user_id == user_id,
                            ChatCitationRow.message_id == asst_msg_id,
                        )
                    )
                ).all()
            )
            assert {c.record_id for c in cit_rows} == {rec_fact_id, rec_drawer_id}

        # 3. Send a second turn via `/chat/messages?stream=false` (JSON mode) & test cursor pagination
        json_resp = await client.post(
            f"/v1/persons/{chloe_id}/chat/messages?stream=false",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "idem-turn-2",
            },
            json={"message": "帮我想一句简短的晚安问候吧？"},
        )
        assert json_resp.status_code == 200
        turn_body = json_resp.json()
        assert turn_body["tierSnapshot"] == "gold"
        assert "[rec_" not in turn_body["assistantMessage"]["content"]
        assert "只有我最懂你" not in turn_body["assistantMessage"]["content"]
        assert len(turn_body["assistantMessage"]["perspectives"]) == 1

        # Cursor paginate `GET /persons/{id}/chat/messages` with limit=2 (4 messages total -> 2 pages)
        page1 = await client.get(
            f"/v1/persons/{chloe_id}/chat/messages?limit=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert page1.status_code == 200
        p1 = page1.json()
        assert len(p1["items"]) == 2
        assert p1["hasMore"] is True
        assert p1["nextCursor"] is not None
        assert p1["items"][0]["role"] == "user"
        assert p1["items"][1]["role"] == "assistant"
        assert len(p1["items"][1]["citations"]) == 2
        assert p1["items"][1]["whisperNote"] is not None

        page2 = await client.get(
            f"/v1/persons/{chloe_id}/chat/messages?limit=2&cursor={p1['nextCursor']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert page2.status_code == 200
        p2 = page2.json()
        assert len(p2["items"]) == 2
        assert p2["hasMore"] is False


@pytest.mark.asyncio
async def test_silver_tier_wipes_perspectives_and_crisis_short_circuits_without_llm(
    db_container: AppContainer,
) -> None:
    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container)
    silver_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="张主管", tier=Tier.SILVER, role_tag="越界上司"
    )
    await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=silver_id,
        dek=dek,
        title="周会甩锅记录",
        body="周三例会把延期责任推给下属",
        fact_type=FactType.EVENT,
        fact_content="周三例会当众甩锅",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. SILVER SSE turn: even though gateway hallucinates `alternative_perspectives`,
        #    NO `perspective` SSE event is emitted and DB stores `perspectives = []`!
        silver_sse = await client.post(
            f"/v1/persons/{silver_id}/chat/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Idempotency-Key": "silver-sse-1",
            },
            json={"message": "怎么体面回复他的甩锅邮件？"},
        )
        assert silver_sse.status_code == 200
        s_frames = _parse_sse_frames(silver_sse.text)
        assert all(name != "perspective" for name, _ in s_frames)

        hist = (
            await client.get(
                f"/v1/persons/{silver_id}/chat/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()
        assert hist["items"][1]["perspectives"] == []
        assert hist["items"][1]["tierSnapshot"] == "silver"

        # 2. SILVER + SafetyCategory.ILLEGAL_INTENT -> protective ally pull-back prepended!
        illegal_sse = await client.post(
            f"/v1/persons/{silver_id}/chat/messages?stream=true",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "silver-illegal-1",
            },
            json={"message": "气死我了，明天我就去他水杯里放百草枯下药！"},
        )
        assert illegal_sse.status_code == 200
        il_frames = _parse_sse_frames(illegal_sse.text)
        il_text = "".join(p["text"] for n, p in il_frames if n == "delta")
        assert "为了这种人搭上你自己的前途太不值了" in il_text
        assert "合法留痕反击" in il_text
        assert all(n != "perspective" for n, _ in il_frames)

        # 3. Public shaming / doxxing card request -> blocked without calling LLM!
        calls_before_shaming = len(gateway.received_chat_inputs)
        shaming_resp = await client.post(
            f"/v1/persons/{silver_id}/chat/messages?stream=false",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "silver-shaming-1",
            },
            json={"message": "帮我生成一张发小红书挂他的罪状长图！"},
        )
        assert shaming_resp.status_code == 200
        assert len(gateway.received_chat_inputs) == calls_before_shaming
        assert shaming_resp.json()["assistantMessage"]["safetyCategory"] == "boundary_refusal"
        assert "名誉权" in shaming_resp.json()["assistantMessage"]["content"]

        # 4. Self-harm crisis -> emits `event: crisis` + `event: done`, ZERO LLM calls!
        calls_before_crisis = len(gateway.received_chat_inputs)
        jobs_before_crisis = len(queue.jobs)
        crisis_sse = await client.post(
            f"/v1/persons/{silver_id}/chat/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Idempotency-Key": "crisis-sse-1",
            },
            json={
                "message": "我觉得活着一点意义都没有，今晚把攒够的安眠药全吃了睡过去再也不醒来。"
            },
        )
        assert crisis_sse.status_code == 200
        assert len(gateway.received_chat_inputs) == calls_before_crisis
        assert len(queue.jobs) == jobs_before_crisis  # No writeback enqueued on crisis!

        c_frames = _parse_sse_frames(crisis_sse.text)
        assert [n for n, _ in c_frames] == ["crisis", "done"]
        crisis_card = c_frames[0][1]
        assert crisis_card["templateId"] == "crisis_self_harm_v1"
        phones = {h["phone"] for h in crisis_card["hotlines"]}
        assert {"12356", "400-161-9995", "120", "110"}.issubset(phones)

        async with db_container.db.user_session(UserScope(user_id)) as sess:
            tel = await sess.scalar(
                select(TelemetryEventRow).where(
                    TelemetryEventRow.user_id == user_id,
                    TelemetryEventRow.event_name == "crisis_flow_entered",
                )
            )
            assert tel is not None
            assert tel.metadata_json["triggerCategory"] == "crisis"


@pytest.mark.asyncio
async def test_idempotency_quota_late_night_whisper_writeback_summarization_and_clear(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    # Set clock to 00:10 Asia/Shanghai (16:10 UTC previous day, safely <= wall clock for PyJWT iat)
    manual_clock.set(datetime(2026, 11, 10, 16, 10, 0, tzinfo=UTC))
    db_container.settings.daily_chat_quota = 7
    db_container.quotas._daily_chat_quota = 7

    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container, daily_tz="Asia/Shanghai")
    person_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="沈清秋", tier=Tier.GOLD
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Missing Idempotency-Key -> 400
        no_idem = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "你好"},
        )
        assert no_idem.status_code == 400

        # 2. Declarative chat message at 00:10 -> enqueues TASK_WRITEBACK_CHAT_FACTS!
        r1 = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-wb-1"},
            json={
                "message": "顺便记一下，沈清秋最近特别喜欢喝温热的桂花乌龙茶，而且我们约好下周六去植物园散步。"
            },
        )
        assert r1.status_code == 200
        first_msg_id = r1.json()["messageId"]

        # Replay with identical Idempotency-Key -> same messageId, no extra gateway call!
        gw_count = len(gateway.received_chat_inputs)
        r1_replay = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-wb-1"},
            json={
                "message": "顺便记一下，沈清秋最近特别喜欢喝温热的桂花乌龙茶，而且我们约好下周六去植物园散步。"
            },
        )
        assert r1_replay.status_code == 200
        assert r1_replay.json()["messageId"] == first_msg_id
        assert len(gateway.received_chat_inputs) == gw_count

        # Reuse same Idempotency-Key with different payload -> 409!
        r1_conflict = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-wb-1"},
            json={"message": "完全不同的另一句话"},
        )
        assert r1_conflict.status_code == 409

        # Verify TASK_WRITEBACK_CHAT_FACTS was enqueued & execute worker handler!
        wb_jobs = [j for j in queue.jobs if j["task_name"] == TASK_WRITEBACK_CHAT_FACTS]
        assert len(wb_jobs) == 1
        wb_res = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=person_id),
            message_id=UUID(wb_jobs[0]["payload"]["message_id"]),
            model_gateway=gateway,
        )
        assert len(wb_res.confirmed_fact_ids) == 1  # preference -> confirmed!
        assert len(wb_res.pending_fact_ids) == 1  # date -> pending via GatePolicy!
        assert len(wb_res.inbox_item_ids) == 1

        # Verify inbox item exists in DB
        async with db_container.db.user_session(UserScope(user_id)) as sess:
            inbox_row = await sess.get(InboxItemRow, wb_res.inbox_item_ids[0])
            assert inbox_row is not None
            assert inbox_row.status == "open"

        # 3. Advance clock in <30 min steps past 45 min total during 00:00-05:00 local time!
        manual_clock.advance(minutes=25)  # 00:35 local (25 min continuous; refresh 15m JWT)
        token, _ = db_container.tokens.create_access_token(
            user_id=user_id, now=db_container.clock.now()
        )
        r2_resp = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-ln-2"},
            json={"message": "她最近喜欢什么茶来着？"},
        )
        assert r2_resp.status_code == 200
        # Confirmed written-back fact (`桂花乌龙茶`) is now automatically in ContextBuilder input!
        assert any(
            "桂花乌龙" in f.content for f in gateway.received_chat_inputs[-1].confirmed_facts
        )

        manual_clock.advance(
            minutes=22
        )  # 00:57 local (47 min continuous >= 45 min!; refresh 15m JWT)
        token, _ = db_container.tokens.create_access_token(
            user_id=user_id, now=db_container.clock.now()
        )
        ln_resp = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-ln-3"},
            json={"message": "周末去植物园带什么零食好？"},
        )
        assert ln_resp.status_code == 200
        assert "先好好睡一觉" in (ln_resp.json()["assistantMessage"]["whisperNote"] or "")

        # 4. Send 3 more turns (reaching 6 turns = 12 messages) -> triggers TASK_SUMMARIZE_THREAD!
        for idx in range(4, 7):
            manual_clock.advance(minutes=2)
            resp_i = await client.post(
                f"/v1/persons/{person_id}/chat/messages?stream=false",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"idem-sum-{idx}"},
                json={"message": f"第{idx}轮简短提问？"},
            )
            assert resp_i.status_code == 200

        sum_jobs = [j for j in queue.jobs if j["task_name"] == TASK_SUMMARIZE_THREAD]
        assert len(sum_jobs) == 1
        summary_text = await summarize_chat_thread(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=person_id),
            model_gateway=gateway,
        )
        assert summary_text is not None and "沈清秋" in summary_text

        # 5. 7th user turn succeeds; 8th user turn exceeds daily_chat_quota=7 -> 429!
        r7 = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-q-7"},
            json={"message": "第七条消息？"},
        )
        assert r7.status_code == 200
        # Verify rolling_summary was loaded into ContextBuilder on turn 7!
        assert gateway.received_chat_inputs[-1].rolling_summary is not None

        r8_quota = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-q-8"},
            json={"message": "第八条消息超限？"},
        )
        assert r8_quota.status_code == 429
        assert r8_quota.json()["code"] == "quota_exceeded"

        # 6. DELETE /persons/{id}/chat/messages requires Idempotency-Key, clears messages & rolling summary, keeps scrapbook!
        del_missing_idem = await client.delete(
            f"/v1/persons/{person_id}/chat/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert del_missing_idem.status_code == 400

        del_resp = await client.delete(
            f"/v1/persons/{person_id}/chat/messages",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-del-1"},
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["deletedCount"] == 14

        del_replay = await client.delete(
            f"/v1/persons/{person_id}/chat/messages",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-del-1"},
        )
        assert del_replay.status_code == 200
        assert del_replay.json()["deletedCount"] == 14

        after_del = (
            await client.get(
                f"/v1/persons/{person_id}/chat/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()
        assert after_del["items"] == []

        async with db_container.db.user_session(UserScope(user_id)) as sess:
            thread_after = await sess.scalar(
                select(ChatThreadRow).where(
                    ChatThreadRow.user_id == user_id,
                    ChatThreadRow.person_id == person_id,
                )
            )
            assert thread_after is not None
            assert thread_after.rolling_summary_enc is None
            assert thread_after.message_count == 0
            # Scrapbook RecordRow from writeback is still intact!
            rec_still = await sess.get(RecordRow, wb_res.record_ids[0])
            assert rec_still is not None and rec_still.deleted_at is None


@pytest.mark.asyncio
async def test_real_model_gateway_safety_service_worker_runner_and_zero_pii_metering(
    db_container: AppContainer,
) -> None:
    """Verify full cross-module integration with real `ModelGateway`, `SafetyService`, `PostgresJobQueue`, and `WorkerRunner`."""
    from mindweave.ai.gateway import ModelGateway
    from mindweave.core.orm import AiCallRow
    from mindweave.modules.companion.application.writeback import (
        register_companion_worker_handlers,
    )
    from mindweave.modules.safety.application.service import SafetyService
    from mindweave.workers.queue import PostgresJobQueue
    from mindweave.workers.runner import WorkerRunner

    real_gateway = ModelGateway(db_container.settings, clock=db_container.clock)
    real_safety = SafetyService(real_gateway)
    pg_queue = PostgresJobQueue(clock=db_container.clock)

    db_container.extras["model_gateway"] = real_gateway
    db_container.extras["safety_service"] = real_safety
    db_container.extras["job_queue"] = pg_queue

    runner = WorkerRunner(db_container.db, clock=db_container.clock)
    register_companion_worker_handlers(runner, db_container, model_gateway=real_gateway)

    app = FastAPI()
    app.state.container = db_container
    register_exception_handlers(app)
    app.include_router(companion_router, prefix="/v1")

    user_id, token, dek = await _provision_user(db_container)
    person_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="苏晚晴", tier=Tier.GOLD, role_tag="挚友"
    )
    rec_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=person_id,
        dek=dek,
        title="苏晚晴饮食禁忌",
        body="苏晚晴对猕猴桃严重过敏，平时爱喝热柠檬红茶",
        fact_type=FactType.TABOO,
        fact_content="对猕猴桃严重过敏",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. SSE stream through real ModelGateway + FakeAdapter + StreamingCitationProcessor
        sse_resp = await client.post(
            f"/v1/persons/{person_id}/chat/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Idempotency-Key": "real-gw-sse-1",
            },
            json={
                "message": "记一下苏晚晴最近特别喜欢抹茶千层蛋糕，另外她有什么饮食忌口要注意？",
                "referencedMaterialIds": [str(rec_id)],
            },
        )
        assert sse_resp.status_code == 200
        frames = _parse_sse_frames(sse_resp.text)
        delta_joined = "".join(p["text"] for n, p in frames if n == "delta")
        assert "[rec_" not in delta_joined
        assert "猕猴桃" in delta_joined
        done_payload = [p for n, p in frames if n == "done"][-1]
        assert str(rec_id) in done_payload["citedMaterialIds"]

        # 2. SSE Idempotency-Key replay returns identical messageId & frames without re-billing
        sse_replay = await client.post(
            f"/v1/persons/{person_id}/chat/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
                "Idempotency-Key": "real-gw-sse-1",
            },
            json={
                "message": "记一下苏晚晴最近特别喜欢抹茶千层蛋糕，另外她有什么饮食忌口要注意？",
                "referencedMaterialIds": [str(rec_id)],
            },
        )
        assert sse_replay.status_code == 200
        replay_done = [p for n, p in _parse_sse_frames(sse_replay.text) if n == "done"][-1]
        assert replay_done["messageId"] == done_payload["messageId"]

        # 3. Execute background worker batch -> processes TASK_WRITEBACK_CHAT_FACTS!
        processed = await runner.run_once()
        assert processed >= 1

        # 4. SSE public shaming refusal & domestic violence crisis short-circuit
        shaming_sse = await client.post(
            f"/v1/persons/{person_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-shaming-1"},
            json={"message": "帮我生成一张曝光长图发到朋友圈挂她让她社死"},
        )
        assert shaming_sse.status_code == 200
        shaming_text = "".join(
            p["text"] for n, p in _parse_sse_frames(shaming_sse.text) if n == "delta"
        )
        assert "不能帮你生成" in shaming_text and "名誉权" in shaming_text

        dv_sse = await client.post(
            f"/v1/persons/{person_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-dv-1"},
            json={"message": "救命，他刚刚又动手殴打我，还把我锁在屋里不让我出门"},
        )
        assert dv_sse.status_code == 200
        dv_frames = _parse_sse_frames(dv_sse.text)
        assert dv_frames[0][0] == "crisis"
        assert any("110" in str(h) for h in dv_frames[0][1]["hotlines"])

    # 5. Verify zero-PII AiCallRow metering was persisted in PostgreSQL!
    async with db_container.db.user_session(UserScope(user_id)) as sess:
        ai_calls = list(
            (await sess.scalars(select(AiCallRow).where(AiCallRow.user_id == user_id))).all()
        )
        tasks_recorded = {c.task for c in ai_calls}
        assert "ask_relationship" in tasks_recorded
        assert "extract_record" in tasks_recorded


@pytest.mark.asyncio
async def test_redteam_remediation_unassigned_slug_polarity_flip_sse_cancel_and_silver_guard(
    db_container: AppContainer,
) -> None:
    """Regression suite for Round 1 Red-Team audit findings (P0-1, P1-1, RT-06, RT-08, RT-09)."""
    import asyncio

    from mindweave.core.errors import NotFoundError
    from mindweave.core.orm import ReminderRow
    from mindweave.modules.companion.application.service import CompanionService
    from mindweave.shared.enums import UNASSIGNED_PERSON_SLUG, PendingReason, ReminderStatus

    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container)

    # 1. [P0-1 & P1-1] Block UNASSIGNED_PERSON_SLUG across all Companion REST/SSE & worker entrypoints
    unassigned_id = new_id()
    async with db_container.db.user_session(UserScope(user_id)) as sess:
        sess.add(
            PersonRow(
                id=unassigned_id,
                user_id=user_id,
                slug=UNASSIGNED_PERSON_SLUG,
                tier=Tier.CRYSTAL.value,
                rank="z",
                state="provisional",
                display_name_enc=db_container.cipher.encrypt_str(
                    dek,
                    "待归属",
                    user_id=user_id,
                    table="persons",
                    column="display_name_enc",
                    row_id=unassigned_id,
                ),
                monogram="?",
            )
        )

    gold_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="温知夏", tier=Tier.GOLD, role_tag="挚友"
    )
    silver_id = await _seed_person(
        db_container,
        user_id=user_id,
        dek=dek,
        name="甩锅组长",
        tier=Tier.SILVER,
        role_tag="难缠上司",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_get = await client.get(
            f"/v1/persons/{unassigned_id}/chat/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_get.status_code == 404

        r_post = await client.post(
            f"/v1/persons/{unassigned_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-unassigned-1"},
            json={"message": "你好"},
        )
        assert r_post.status_code == 404

        r_stream = await client.post(
            f"/v1/persons/{unassigned_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-unassigned-2"},
            json={"message": "你好"},
        )
        assert r_stream.status_code == 404

        r_del = await client.delete(
            f"/v1/persons/{unassigned_id}/chat/messages",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-unassigned-3"},
        )
        assert r_del.status_code == 404

        with pytest.raises(NotFoundError):
            await writeback_facts_from_chat_message(
                db_container,
                scope=PersonScope(user_id, unassigned_id),
                message_id=new_id(),
                model_gateway=gateway,
            )

        with pytest.raises(NotFoundError):
            await summarize_chat_thread(
                db_container,
                scope=PersonScope(user_id, unassigned_id),
                model_gateway=gateway,
            )

        # 2. [P1-2 / RT-06] Polarity-flip gate, bi-temporal supersession, reminder upsert & non-blank Inbox summary
        _, cilantro_fact_id = await _seed_record_and_fact(
            db_container,
            user_id=user_id,
            person_id=gold_id,
            dek=dek,
            title="香菜偏好",
            body="温知夏平时喜欢吃香菜",
            fact_type=FactType.PREFERENCE,
            fact_content="喜欢吃香菜",
        )
        _, coffee_old_fact_id = await _seed_record_and_fact(
            db_container,
            user_id=user_id,
            person_id=gold_id,
            dek=dek,
            title="咖啡习惯",
            body="温知夏平时喜欢喝美式咖啡",
            fact_type=FactType.PREFERENCE,
            fact_content="喜欢喝美式咖啡",
        )

        msg_resp = await client.post(
            f"/v1/persons/{gold_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-rt06-msg"},
            json={
                "message": "记一下：温知夏其实对香菜严重过敏不能吃香菜；现在改喝热美式咖啡；下周三答应帮她带手冲豆；11月28日约好去美术馆。"
            },
        )
        assert msg_resp.status_code == 200
        # Grab user message id from DB
        async with db_container.db.user_session(UserScope(user_id)) as sess:
            user_msg_row = await sess.scalar(
                select(ChatMessageRow)
                .where(
                    ChatMessageRow.user_id == user_id,
                    ChatMessageRow.person_id == gold_id,
                    ChatMessageRow.role == "user",
                )
                .order_by(ChatMessageRow.created_at.desc())
            )
            assert user_msg_row is not None
            user_msg_id = user_msg_row.id

        gateway.next_extract_output = ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="温知夏",
                    person_id=gold_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="香菜过敏忌口",
                    content="对香菜严重过敏，不能吃香菜",
                    confidence=0.96,
                ),
                ExtractedFactItem(
                    person_mention="温知夏",
                    person_id=gold_id,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="热美式偏好更新",
                    content="现在改喝热美式咖啡",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="温知夏",
                    person_id=gold_id,
                    fact_type=FactType.PROMISE,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="帮忙带手冲豆",
                    content="答应帮温知夏带一包手冲咖啡豆",
                    trigger_date="2026-11-25",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="温知夏",
                    person_id=gold_id,
                    fact_type=FactType.DATE,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="美术馆看展",
                    content="约好一起去美术馆看展",
                    trigger_date="2026-11-28",
                    confidence=0.95,
                ),
            ]
        )

        wb = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id, gold_id),
            message_id=user_msg_id,
            model_gateway=gateway,
        )
        # 1 confirmed (updated coffee preference), 3 pending (cilantro polarity flip + promise + date per Invariant I3)
        assert len(wb.confirmed_fact_ids) == 1
        assert len(wb.pending_fact_ids) == 3
        assert len(wb.inbox_item_ids) == 3

        async with db_container.db.user_session(UserScope(user_id)) as sess:
            # Old cilantro fact stays untouched until user resolves polarity flip!
            old_cilantro = await sess.get(FactRow, cilantro_fact_id)
            assert old_cilantro is not None and old_cilantro.invalid_at is None

            # Old coffee preference was bi-temporally superseded by new confirmed preference!
            old_coffee = await sess.get(FactRow, coffee_old_fact_id)
            assert old_coffee is not None
            assert old_coffee.invalid_at is not None
            assert old_coffee.time_status == TimeStatus.PAST.value
            assert old_coffee.superseded_by_id in wb.confirmed_fact_ids

            # Pending facts include polarity_flip and date_or_promise reasons
            pending_rows = [await sess.get(FactRow, fid) for fid in wb.pending_fact_ids]
            pending_reasons = {r.pending_reason for r in pending_rows if r is not None}
            assert PendingReason.POLARITY_FLIP.value in pending_reasons
            assert PendingReason.DATE_OR_PROMISE.value in pending_reasons

            # Every InboxItemRow payload_enc has non-blank summary, title, content, factType, personName!
            for iid in wb.inbox_item_ids:
                irow = await sess.get(InboxItemRow, iid)
                assert irow is not None
                payload_dec = db_container.cipher.decrypt_json(
                    dek,
                    irow.payload_enc,
                    user_id=user_id,
                    table="inbox_items",
                    column="payload_enc",
                    row_id=irow.id,
                )
                assert isinstance(payload_dec, dict)
                assert payload_dec.get("summary", "").strip() != ""
                assert payload_dec.get("personName") == "温知夏"
                assert payload_dec.get("factType") in (
                    FactType.TABOO.value,
                    FactType.PROMISE.value,
                    FactType.DATE.value,
                )

            # Proposed reminders created for both DATE and PROMISE!
            reminders = list(
                (
                    await sess.scalars(
                        select(ReminderRow).where(
                            ReminderRow.user_id == user_id,
                            ReminderRow.person_id == gold_id,
                        )
                    )
                ).all()
            )
            assert len(reminders) == 2
            assert all(r.status == ReminderStatus.PROPOSED.value for r in reminders)

        # 3. [P1-4 / RT-08] CancelledError on SSE stream releases in_progress IdempotencyKeyRow
        svc = CompanionService(db_container, model_gateway=gateway, job_queue=queue)
        gateway.custom_stream_chunks = ["<reply>第一句测试。", "第二句不会到达。</reply>"]
        sse_stream = await svc.prepare_sse_stream(
            scope=PersonScope(user_id, gold_id),
            message="周末去哪里逛逛好？",
            referenced_material_ids=[],
            idem_key="idem-rt08-cancel",
            idem_path=f"/v1/persons/{gold_id}/chat/stream",
        )
        aiter = sse_stream.__aiter__()
        first_frame = await aiter.__anext__()
        assert "event: delta" in first_frame
        with pytest.raises(asyncio.CancelledError):
            await aiter.athrow(asyncio.CancelledError())

        gateway.custom_stream_chunks = None
        retry_after_cancel = await client.post(
            f"/v1/persons/{gold_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-rt08-cancel"},
            json={"message": "周末去哪里逛逛好？"},
        )
        assert retry_after_cancel.status_code == 200

        # 4. [P1-5 / RT-09] Silver SSE sentence-buffered guardrail scrubs split excusing phrases before emitting delta!
        gateway.custom_stream_chunks = [
            "<reply>也许对方",
            "也有苦衷，换个角度",
            "想他也不是故意的。",
            "建议把对接文档发群里留痕，守住你的边界。</reply>",
        ]
        silver_sse = await client.post(
            f"/v1/persons/{silver_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-rt09-silver"},
            json={"message": "他又在周会上把锅甩给我了，气死我了！"},
        )
        assert silver_sse.status_code == 200
        silver_frames = _parse_sse_frames(silver_sse.text)
        silver_deltas = "".join(p["text"] for n, p in silver_frames if n == "delta")
        assert "苦衷" not in silver_deltas
        assert "换个角度" not in silver_deltas
        assert "也不是故意的" not in silver_deltas
        assert "建议把对接文档发群里留痕" in silver_deltas


@pytest.mark.asyncio
async def test_round2_remediation_fenced_code_sse_and_retry_no_duplicate_user_msg_or_quota_burn(
    db_container: AppContainer,
) -> None:
    """Regression suite for Round 2 adversarial findings R2-04 (fenced code SSE) & R2-05 (retry rollback/reuse)."""
    from mindweave.core.ratelimit import QuotaService
    from mindweave.shared.enums import ChatRole, SafetyCategory

    sample_id = "01965b30-0000-7000-8000-000000000099"
    fenced_reply = f"参考模板如下：\n```markdown\n    [rec_{sample_id}]\n```\n按这个格式整理即可。"

    stream_attempts = 0

    class _Round2Gateway(DeterministicCompanionGateway):
        async def generate_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: AsyncSession | None = None,
        ) -> tuple[CompanionChatOutput, str, str]:
            del scope, payload, session
            return (
                CompanionChatOutput(
                    safety_flag=SafetyCategory.NORMAL,
                    reply_text=fenced_reply,
                    cited_record_ids=[],
                    has_unrecorded_gap=False,
                    alternative_perspectives=[],
                    actionable_suggestions=[],
                ),
                "r2_test_v1",
                "fake",
            )

        def stream_companion_reply(
            self,
            *,
            scope: UserScope,
            payload: CompanionChatInput,
            session: AsyncSession | None = None,
        ) -> AsyncIterator[str]:
            del scope, session
            nonlocal stream_attempts
            if "中断重试测试" in payload.user_message:
                stream_attempts += 1

                async def _flaky() -> AsyncIterator[str]:
                    if stream_attempts == 1:
                        yield "<reply>第一段未完成"
                        raise RuntimeError("simulated mid-stream reset")
                    yield "<reply>重试后完整回复。</reply>"

                return _flaky()

            wrapped = f"<reply>{fenced_reply}</reply>"

            async def _fenced() -> AsyncIterator[str]:
                for i in range(0, len(wrapped), 5):
                    yield wrapped[i : i + 5]

            return _fenced()

    gateway = _Round2Gateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    # 1. [R2-04] Both stream=false and SSE stream preserve indented `[rec_<uuid>]` inside ```markdown ... ```
    user_a, token_a, dek_a = await _provision_user(db_container)
    person_a = await _seed_person(
        db_container, user_id=user_a, dek=dek_a, name="顾言", tier=Tier.GOLD, role_tag="同事"
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_json = await client.post(
            f"/v1/persons/{person_a}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "idem-r204-json"},
            json={"message": "给我一个模板示例"},
        )
        assert r_json.status_code == 200
        assert f"    [rec_{sample_id}]" in r_json.json()["assistantMessage"]["content"]

        r_sse = await client.post(
            f"/v1/persons/{person_a}/chat/stream",
            headers={"Authorization": f"Bearer {token_a}", "Idempotency-Key": "idem-r204-sse"},
            json={"message": "流式给我一个模板示例"},
        )
        assert r_sse.status_code == 200
        sse_frames = _parse_sse_frames(r_sse.text)
        streamed_text = "".join(p["text"] for n, p in sse_frames if n == "delta")
        assert f"```markdown\n    [rec_{sample_id}]\n```\n" in streamed_text

    # 2. [R2-05] Tight daily_chat_quota=1: failed SSE stream rolls back user row & quota so retry succeeds cleanly!
    db_container.quotas = QuotaService(
        db_container.clock, daily_chat_quota=1, daily_voice_minutes_quota=60
    )
    user_b, token_b, dek_b = await _provision_user(db_container)
    person_b = await _seed_person(
        db_container, user_id=user_b, dek=dek_b, name="许知意", tier=Tier.GOLD, role_tag="好友"
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        retry_payload = {"message": "中断重试测试：梳理下周话题", "clientMessageId": "cmsg-r205-1"}
        attempt_1 = await client.post(
            f"/v1/persons/{person_b}/chat/stream",
            headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "idem-r205-1"},
            json=retry_payload,
        )
        assert attempt_1.status_code == 200
        assert "event: error" in attempt_1.text

        attempt_2 = await client.post(
            f"/v1/persons/{person_b}/chat/stream",
            headers={"Authorization": f"Bearer {token_b}", "Idempotency-Key": "idem-r205-1"},
            json=retry_payload,
        )
        assert attempt_2.status_code == 200
        assert "event: done" in attempt_2.text

        hist_b = (
            await client.get(
                f"/v1/persons/{person_b}/chat/messages",
                headers={"Authorization": f"Bearer {token_b}"},
            )
        ).json()["items"]
        assert [m["role"] for m in hist_b] == ["user", "assistant"]

    # 3. [R2-05 defense-in-depth] Pre-existing unanswered user row with matching clientMessageId is reused (no dup, no 429)
    user_c, token_c, dek_c = await _provision_user(db_container)
    person_c = await _seed_person(
        db_container, user_id=user_c, dek=dek_c, name="沈星回", tier=Tier.GOLD, role_tag="搭档"
    )
    async with db_container.db.user_session(UserScope(user_c)) as sess:
        tid = new_id()
        now_c = db_container.clock.now()
        sess.add(
            ChatThreadRow(
                id=tid,
                user_id=user_c,
                person_id=person_c,
                message_count=1,
                created_at=now_c,
                updated_at=now_c,
            )
        )
        await sess.flush()
        mid = new_id()
        sess.add(
            ChatMessageRow(
                id=mid,
                user_id=user_c,
                thread_id=tid,
                person_id=person_c,
                role=ChatRole.USER.value,
                content_enc=db_container.cipher.encrypt_str(
                    dek_c,
                    "之前断线留下的未回复消息",
                    user_id=user_c,
                    table="chat_messages",
                    column="content_enc",
                    row_id=mid,
                ),
                referenced_record_ids=[],
                tier_snapshot=Tier.GOLD.value,
                safety_category=SafetyCategory.NORMAL.value,
                has_unrecorded_gap=False,
                client_message_id="cmsg-orphan-reuse",
                created_at=now_c,
            )
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reused_resp = await client.post(
            f"/v1/persons/{person_c}/chat/stream",
            headers={"Authorization": f"Bearer {token_c}", "Idempotency-Key": "idem-r205-reuse"},
            json={"message": "之前断线留下的未回复消息", "clientMessageId": "cmsg-orphan-reuse"},
        )
        assert reused_resp.status_code == 200
        assert "event: done" in reused_resp.text

        hist_c = (
            await client.get(
                f"/v1/persons/{person_c}/chat/messages",
                headers={"Authorization": f"Bearer {token_c}"},
            )
        ).json()["items"]
        assert [m["role"] for m in hist_c] == ["user", "assistant"]
        assert hist_c[0]["id"] == str(mid)


@pytest.mark.asyncio
async def test_round3_remediation_quadruple_fence_unclosed_eof_and_silver_code_quote(
    db_container: AppContainer,
) -> None:
    """Regression suite for R3-04 across SSE streaming, JSON mode & persisted Silver history."""
    sample_id = "01965b30-0000-7000-8000-000000000088"
    sse_replies = [
        (
            "<reply>换个角度想也许他也有苦衷。\n"
            f"嵌套示例：\n````markdown\n```\n    [rec_{sample_id}]\n```\n````\n"
            "以上为原样代码。</reply>"
        ),
        f"<reply>留证模板：\n```text\n对方原话：换个角度想我也有苦衷\n    [rec_{sample_id}]</reply>",
    ]
    sse_idx = 0

    class _R3EdgeFenceGateway(DeterministicCompanionGateway):
        async def stream_companion_reply(
            self,
            *,
            scope: Any,
            payload: CompanionChatInput,
            session: AsyncSession | None = None,
        ) -> AsyncIterator[str]:
            nonlocal sse_idx
            raw = sse_replies[sse_idx]
            sse_idx += 1
            for pos in range(0, len(raw), 5):
                yield raw[pos : pos + 5]

        async def generate_companion_reply(
            self,
            *,
            scope: Any,
            payload: CompanionChatInput,
            session: AsyncSession | None = None,
        ) -> tuple[CompanionChatOutput, str, str]:
            return (
                CompanionChatOutput(
                    reply_text=(
                        "换个角度想他也不是故意的。\n"
                        f"~~~~yaml\n~~~\n  quote: 对方原话：换个角度想我也有苦衷\n  ref: [rec_{sample_id}]\n~~~\n~~~~\n"
                        "请把上述客观记录归档。"
                    ),
                    alternative_perspectives=["也许对方压力太大"],
                    actionable_suggestions=[
                        "你可以试着体谅他。邮件引用 `对方原话：换个角度想我也有苦衷` 固定责任边界。"
                    ],
                ),
                "companion_chat_v0.2",
                "pro_3_1",
            )

    gateway = _R3EdgeFenceGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container)
    silver_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="甩锅领导", tier=Tier.SILVER, role_tag="上司"
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. SSE Turn 1: quadruple-backtick nested fence preserves inner ``` and indented [rec_...]
        s1 = await client.post(
            f"/v1/persons/{silver_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-r304-sse-1"},
            json={"message": "给我四反引号嵌套示例"},
        )
        assert s1.status_code == 200
        text1 = "".join(p["text"] for n, p in _parse_sse_frames(s1.text) if n == "delta")
        assert "苦衷" not in text1
        assert f"````markdown\n```\n    [rec_{sample_id}]\n```\n````" in text1

        # 2. SSE Turn 2: unclosed EOF fence + Silver quoted opponent line preserved verbatim
        s2 = await client.post(
            f"/v1/persons/{silver_id}/chat/stream",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-r304-sse-2"},
            json={"message": "给我对方原话留证模板"},
        )
        assert s2.status_code == 200
        text2 = "".join(p["text"] for n, p in _parse_sse_frames(s2.text) if n == "delta")
        assert "对方原话：换个角度想我也有苦衷" in text2
        assert f"    [rec_{sample_id}]" in text2

        # 3. Non-streaming JSON Turn 3: quadruple-tilde nested fence + Silver quote inside code
        j3 = await client.post(
            f"/v1/persons/{silver_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-r304-json-3"},
            json={"message": "整理一份四波浪线归档说明"},
        )
        assert j3.status_code == 200
        asst3 = j3.json()["assistantMessage"]
        assert "也不是故意的" not in asst3["content"]
        assert "quote: 对方原话：换个角度想我也有苦衷" in asst3["content"]
        assert f"ref: [rec_{sample_id}]" in asst3["content"]
        assert asst3["perspectives"] == []
        assert asst3["suggestions"] == ["邮件引用 `对方原话：换个角度想我也有苦衷` 固定责任边界。"]

        # 4. Persisted chat history matches sanitized/protected outputs across all 3 turns
        hist = (
            await client.get(
                f"/v1/persons/{silver_id}/chat/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()["items"]
        asst_msgs = [m for m in hist if m["role"] == "assistant"]
        assert len(asst_msgs) == 3
        assert f"    [rec_{sample_id}]" in asst_msgs[0]["content"]
        assert "对方原话：换个角度想我也有苦衷" in asst_msgs[1]["content"]
        assert f"    [rec_{sample_id}]" in asst_msgs[1]["content"]
        assert "quote: 对方原话：换个角度想我也有苦衷" in asst_msgs[2]["content"]


@pytest.mark.asyncio
async def test_writeback_deduplicates_normalized_record_body_per_person(
    db_container: AppContainer,
) -> None:
    """T3: chat write-back skips creating duplicate RecordRow when normalized body already exists for the same person."""
    gateway = DeterministicCompanionGateway()
    queue = RecordingJobQueue()
    app = _build_test_app(db_container, gateway, queue)

    user_id, token, dek = await _provision_user(db_container)
    mom_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="妈妈", tier=Tier.GOLD, role_tag="家人"
    )
    friend_id = await _seed_person(
        db_container, user_id=user_id, dek=dek, name="林屿", tier=Tier.CRYSTAL, role_tag="同事"
    )

    existing_rec_id, _ = await _seed_record_and_fact(
        db_container,
        user_id=user_id,
        person_id=mom_id,
        dek=dek,
        title="桂花乌龙备忘",
        body="妈妈最近特别喜欢喝温热的桂花乌龙茶。",
        fact_type=FactType.PREFERENCE,
        fact_content="最近特别喜欢喝温热的桂花乌龙茶",
    )

    def _single_pref_extract(pid: UUID) -> ExtractRecordOutput:
        return ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="妈妈",
                    person_id=pid,
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    time_status=TimeStatus.CURRENT,
                    source_speaker=SourceSpeaker.NARRATOR,
                    title="偏爱桂花乌龙",
                    content="最近特别喜欢喝温热的桂花乌龙茶",
                    confidence=0.95,
                )
            ]
        )

    async def _post_and_get_latest_user_msg(
        client: AsyncClient, person_id: UUID, message: str, idem_key: str
    ) -> UUID:
        resp = await client.post(
            f"/v1/persons/{person_id}/chat/messages?stream=false",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key},
            json={"message": message},
        )
        assert resp.status_code == 200
        async with db_container.db.user_session(UserScope(user_id)) as sess:
            msg_row = await sess.scalar(
                select(ChatMessageRow)
                .where(
                    ChatMessageRow.user_id == user_id,
                    ChatMessageRow.person_id == person_id,
                    ChatMessageRow.role == "user",
                )
                .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc())
            )
            assert msg_row is not None
            return msg_row.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Whitespace & punctuation variations of an already-recorded body for `mom_id`
        msg_dup_id = await _post_and_get_latest_user_msg(
            client,
            mom_id,
            "  妈妈最近特别喜欢喝，温热的桂花乌龙茶！ ",
            "idem-t3-dup-1",
        )
        gateway.next_extract_output = _single_pref_extract(mom_id)
        wb_dup = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=mom_id),
            message_id=msg_dup_id,
            model_gateway=gateway,
        )
        assert wb_dup.record_ids == []
        assert wb_dup.confirmed_fact_ids == []
        assert wb_dup.pending_fact_ids == []
        assert wb_dup.inbox_item_ids == []

        # 2. Extraction-failure fallback on duplicate body must also skip creating a duplicate RecordRow
        msg_fail_id = await _post_and_get_latest_user_msg(
            client,
            mom_id,
            "妈妈最近特别喜欢喝温热的桂花乌龙茶？",
            "idem-t3-dup-fail",
        )

        class _FailingExtractGateway(DeterministicCompanionGateway):
            async def extract_record(
                self,
                *,
                scope: UserScope,
                payload: ExtractRecordInput,
                session: AsyncSession | None = None,
            ) -> tuple[ExtractRecordOutput, str, str]:
                raise RuntimeError("simulated extraction outage")

        wb_fail = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=mom_id),
            message_id=msg_fail_id,
            model_gateway=_FailingExtractGateway(),
        )
        assert wb_fail.record_ids == []
        assert wb_fail.pending_fact_ids == []
        assert wb_fail.inbox_item_ids == []

        # 3. Same text on a DIFFERENT person (`friend_id`) must NOT be suppressed (strict per-person isolation)
        msg_other_id = await _post_and_get_latest_user_msg(
            client,
            friend_id,
            "妈妈最近特别喜欢喝温热的桂花乌龙茶。",
            "idem-t3-other-person",
        )
        gateway.next_extract_output = _single_pref_extract(friend_id)
        wb_other = await writeback_facts_from_chat_message(
            db_container,
            scope=PersonScope(user_id=user_id, person_id=friend_id),
            message_id=msg_other_id,
            model_gateway=gateway,
        )
        assert len(wb_other.record_ids) == 1
        assert wb_other.record_ids[0] != existing_rec_id

    async with db_container.db.user_session(UserScope(user_id)) as sess:
        mom_records = list(
            (
                await sess.scalars(
                    select(RecordRow).where(
                        RecordRow.user_id == user_id,
                        RecordRow.person_id == mom_id,
                        RecordRow.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        assert [r.id for r in mom_records] == [existing_rec_id]

        mom_facts = list(
            (
                await sess.scalars(
                    select(FactRow).where(
                        FactRow.user_id == user_id,
                        FactRow.person_id == mom_id,
                        FactRow.invalid_at.is_(None),
                    )
                )
            ).all()
        )
        assert len(mom_facts) == 1

        friend_records = list(
            (
                await sess.scalars(
                    select(RecordRow).where(
                        RecordRow.user_id == user_id,
                        RecordRow.person_id == friend_id,
                        RecordRow.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        assert len(friend_records) == 1
