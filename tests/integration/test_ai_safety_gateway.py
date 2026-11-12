"""Live PostgreSQL integration tests for AI ModelGateway, SafetyService, Zero-PII Metering, and RLS."""

from __future__ import annotations

from sqlalchemy import select, text

from mindweave.ai.adapters.fake import FakeAdapter, FakeFaultConfig
from mindweave.ai.gateway import ModelGateway
from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    ContextFactItem,
    ExtractRecordInput,
    KnownPersonRef,
    TranscribeInput,
)
from mindweave.core.dependencies import AppContainer
from mindweave.core.ids import new_id
from mindweave.core.orm import AiCallRow, UserRow
from mindweave.modules.safety.application.service import SafetyService
from mindweave.shared.enums import FactType, SafetyCategory, Tier
from mindweave.shared.scopes import UserScope


async def _seed_user(container: AppContainer) -> UserScope:
    user_id = new_id()
    async with container.db.system_session() as session:
        session.add(UserRow(id=user_id, status="active"))
    return UserScope(user_id=user_id)


async def test_ai_calls_persisted_with_zero_pii_and_strict_rls_isolation(
    db_container: AppContainer,
) -> None:
    scope_a = await _seed_user(db_container)
    scope_b = await _seed_user(db_container)

    fake = FakeAdapter(
        fault_config=FakeFaultConfig(
            malformed_json_once_tasks={"transcribe"},
        )
    )
    gw = ModelGateway(db_container.settings, clock=db_container.clock, adapters={"fake": fake})
    safety = SafetyService(gw, timeout_seconds=db_container.settings.safety_timeout_seconds)

    sensitive_secret_marker = "绝密隐私口令_桂花巷88号_沈清秋过敏档案"
    person_a_id = new_id()
    rec_a_id = new_id()

    # Tenant A executes SafetyService + extract_record + generate_companion_reply + repaired transcribe
    async with db_container.db.user_session(scope_a) as session_a:
        safety_dec = await safety.evaluate(
            scope=scope_a,
            text=f"记录一下：{sensitive_secret_marker}",
            session=session_a,
        )
        assert safety_dec.category is SafetyCategory.NORMAL

        ext_out, prompt_ver, _ = await gw.extract_record(
            scope=scope_a,
            payload=ExtractRecordInput(
                text=f"沈清秋住在{sensitive_secret_marker}，对芒果严重过敏，身份证 11010519950618127X。",
                current_date="2026-11-11",
                scoped_person_id=person_a_id,
                scoped_person_tier=Tier.GOLD,
                known_persons=[
                    KnownPersonRef(person_id=person_a_id, display_name="沈清秋", tier=Tier.GOLD)
                ],
            ),
            session=session_a,
        )
        assert prompt_ver == "extract_record@1.1.0"
        assert len(ext_out.facts) >= 1

        chat_out, chat_ver, _ = await gw.generate_companion_reply(
            scope=scope_a,
            payload=CompanionChatInput(
                person_id=person_a_id,
                person_name="沈清秋",
                tier=Tier.GOLD,
                confirmed_facts=[
                    ContextFactItem(
                        record_id=rec_a_id,
                        fact_id=new_id(),
                        fact_type=FactType.TABOO,
                        content="对芒果严重过敏",
                    )
                ],
                user_message="周末想请沈清秋吃甜品要注意什么？",
            ),
            session=session_a,
        )
        assert chat_ver == "ask_relationship@1.1.0"
        assert chat_out.cited_record_ids == [rec_a_id]

        # Streaming reply also records zero-PII metering row
        stream_text = "".join(
            [
                chunk
                async for chunk in gw.stream_companion_reply(
                    scope=scope_a,
                    payload=CompanionChatInput(
                        person_id=person_a_id,
                        person_name="沈清秋",
                        tier=Tier.GOLD,
                        confirmed_facts=[
                            ContextFactItem(
                                record_id=rec_a_id,
                                fact_id=new_id(),
                                fact_type=FactType.TABOO,
                                content="对芒果严重过敏",
                            )
                        ],
                        user_message="帮我确认忌口",
                    ),
                    session=session_a,
                )
            ]
        )
        assert f"[rec_{rec_a_id}]" in stream_text

        # Transcribe triggers 1-shot JSON repair -> persists `validation_failed` + `repaired`
        trans_out = await gw.transcribe(
            scope=scope_a,
            payload=TranscribeInput(
                audio_bytes=f"TEXT:{sensitive_secret_marker}".encode(),
                mime_type="audio/webm",
                known_person_names=["沈清秋"],
            ),
            session=session_a,
        )
        assert "绝密隐私口令" in trans_out.corrected_transcript

    # Tenant B executes a single safety classification
    async with db_container.db.user_session(scope_b) as session_b:
        b_dec = await safety.evaluate(
            scope=scope_b,
            text="上班改方案累得想原地去世哈哈！言归正传记一下明早开会。",
            session=session_b,
        )
        assert b_dec.category is SafetyCategory.NORMAL

    # 1. Verify Tenant A sees ONLY Tenant A's `ai_calls` rows, with valid metadata & ZERO PII!
    async with db_container.db.user_session(scope_a) as check_a:
        rows_a = list(
            (
                await check_a.execute(select(AiCallRow).order_by(AiCallRow.created_at.asc()))
            ).scalars()
        )
        assert len(rows_a) == 6  # safety + extract + ask + stream + transcribe(failed + repaired)
        outcomes_a = {r.outcome for r in rows_a}
        assert {"ok", "stream_ok", "validation_failed", "repaired_ok"}.issubset(outcomes_a)

        for row in rows_a:
            assert row.user_id == scope_a.user_id
            assert row.prompt_id is not None
            assert row.prompt_version is not None
            assert row.prompt_hash is not None and len(row.prompt_hash) == 64
            assert row.input_tokens > 0
            assert row.latency_ms >= 0

            # Invariant I10: ZERO raw user text, person names, or PII anywhere in persisted row!
            serialized_cols = " | ".join(
                str(getattr(row, col.name)) for col in AiCallRow.__table__.columns
            )
            assert sensitive_secret_marker not in serialized_cols
            assert "沈清秋" not in serialized_cols
            assert "11010519950618127" not in serialized_cols
            assert "芒果" not in serialized_cols

    # 2. Verify Tenant B sees ONLY Tenant B's 1 row (RLS blocks reading Tenant A's rows!)
    async with db_container.db.user_session(scope_b) as check_b:
        rows_b = list((await check_b.execute(select(AiCallRow))).scalars())
        assert len(rows_b) == 1
        assert rows_b[0].user_id == scope_b.user_id
        assert rows_b[0].task == "safety_classify"

    # 3. Verify unauthenticated app session WITHOUT `app.user_id` sees 0 rows (FORCE RLS fail-closed!)
    async with db_container.db.unauthenticated_app_session() as bare_sess:
        res = await bare_sess.execute(text("SELECT count(*) FROM ai_calls"))
        assert res.scalar_one() == 0


async def test_safety_l2_timeout_persists_timeout_metering_and_preserves_l1_decision(
    db_container: AppContainer,
) -> None:
    scope = await _seed_user(db_container)
    fake = FakeAdapter(fault_config=FakeFaultConfig(timeout_tasks={"safety_classify"}))
    gw = ModelGateway(db_container.settings, clock=db_container.clock, adapters={"fake": fake})
    safety = SafetyService(gw, timeout_seconds=0.5)

    async with db_container.db.user_session(scope) as session:
        dv_dec = await safety.evaluate(
            scope=scope,
            text="老公喝醉了正在家暴打我，门被反锁出不去救命！",
            session=session,
        )
        assert dv_dec.category is SafetyCategory.VIOLENCE_VICTIM
        assert dv_dec.trigger_source == "l1"
        assert dv_dec.block_normal_reply is True
        assert dv_dec.crisis_card is not None
        assert dv_dec.crisis_card.template_id == "domestic_violence_v1"

    async with db_container.db.user_session(scope) as verify_sess:
        rows = list((await verify_sess.execute(select(AiCallRow))).scalars())
        assert len(rows) == 1
        assert rows[0].task == "safety_classify"
        assert rows[0].outcome == "timeout"
