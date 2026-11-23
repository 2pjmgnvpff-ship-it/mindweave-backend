"""Red-team security & privacy regression tests for Remediation Lane 2.

Covers all 6 security/privacy invariants:
1. `POST /v1/voice/transcribe` strict `PersonScope` isolation (zero cross-person bleed),
   token-bucket rate limiting, daily `AiCallRow(task='transcribe')` quota enforcement,
   bounded upload reads (413), and canonical OpenAPI exposure.
2. Venting quarantine captures NEVER spawn active/proposed reminders; `DELETE /v1/materials/{id}`
   atomically soft-rejects linked `FactRow`s and cancels linked `ReminderRow`s.
3. Silver-tier Zero-Photo invariant: rejects uploads (422), cloaks photo metadata/URLs across
   `/persons`, `/bootstrap`, and `/materials` (including after `GOLD -> SILVER` transition),
   and blocks direct byte streaming (`get_photo` / `get_material_media` -> 404).
4. `POST /v1/people/{id}/reminders` strict `PersonScope` fact validation (rejects `person_id IS NULL`,
   rejected, and invalidated facts with 404), same-tenant & cross-tenant PK conflict (409),
   and same-day post-09:00 graceful fallback (`now + 5m`).
5. `PATCH /v1/facts/{id}` transitioning out of `PENDING` auto-resolves open `FACT_CONFIRM`
   inbox items once no linked facts remain `PENDING` and syncs `CaptureRow.status`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from scripts.seed_demo import build_demo_wav_bytes
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.adapters.fake import FakeAdapter
from mindweave.ai.gateway import ModelGateway
from mindweave.ai.ports import ModelGatewayPort
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
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    AiCallRow,
    CaptureRow,
    FactRow,
    InboxItemRow,
    RecordRow,
    ReminderRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.core.ratelimit import QuotaService
from mindweave.main import create_app
from mindweave.shared.enums import (
    FactStatus,
    FactType,
    InboxItemKind,
    InboxItemStatus,
    Polarity,
    RecordKind,
    RecordReviewState,
    ReminderStatus,
    SourceSpeaker,
    TimeStatus,
)
from mindweave.shared.scopes import UserScope


def _make_clean_jpeg() -> bytes:
    img = Image.new("RGB", (32, 32), color=(180, 120, 90))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_wav_with_text(text: str) -> bytes:
    payload = f"TEXT:{text}".encode()
    riff_size = (4 + len(payload)).to_bytes(4, "little")
    return b"RIFF" + riff_size + b"WAVE" + payload


async def _provision_user(
    container: AppContainer,
    *,
    tz_name: str = "Asia/Shanghai",
) -> tuple[UUID, UserScope, str, bytes]:
    user_id = new_id()
    scope = UserScope(user_id=user_id)
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=user_id, status="active"))
    async with container.db.user_session(scope) as sess:
        sess.add(UserKeyRow(user_id=user_id, wrapped_dek=wrapped_dek, kek_id=kek_id))
        sess.add(UserPreferenceRow(user_id=user_id, timezone=tz_name))
        await sess.flush()
        dek = await container.load_user_dek(sess, scope)
    token, _ = container.tokens.create_access_token(user_id=user_id, now=container.clock.now())
    return user_id, scope, token, dek


def _auth(token: str, *, idem: str | None = None, if_match: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


class _SpyTranscribeGateway(ModelGatewayPort):
    """Records every `TranscribeInput` while delegating to a real `ModelGateway`."""

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.transcribe_inputs: list[TranscribeInput] = []

    async def transcribe(
        self,
        *,
        scope: UserScope,
        payload: TranscribeInput,
        session: AsyncSession | None = None,
    ) -> TranscribeOutput:
        self.transcribe_inputs.append(payload)
        return await self._inner.transcribe(scope=scope, payload=payload, session=session)

    async def parse_screenshot(
        self,
        *,
        scope: UserScope,
        image_bytes: bytes,
        mime_type: str,
        target_person_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> ScreenshotParseOutput:
        return await self._inner.parse_screenshot(
            scope=scope,
            image_bytes=image_bytes,
            mime_type=mime_type,
            target_person_name=target_person_name,
            session=session,
        )

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        return await self._inner.extract_record(scope=scope, payload=payload, session=session)

    async def generate_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> tuple[CompanionChatOutput, str, str]:
        return await self._inner.generate_companion_reply(
            scope=scope, payload=payload, session=session
        )

    def stream_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        return self._inner.stream_companion_reply(scope=scope, payload=payload, session=session)

    async def classify_safety(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyClassifyOutput:
        return await self._inner.classify_safety(scope=scope, text=text, session=session)

    async def summarize_thread(
        self,
        *,
        scope: UserScope,
        person_name: str,
        previous_summary: str | None,
        turns_text: str,
        session: AsyncSession | None = None,
    ) -> str:
        return await self._inner.summarize_thread(
            scope=scope,
            person_name=person_name,
            previous_summary=previous_summary,
            turns_text=turns_text,
            session=session,
        )


class _ProgrammableExtractGateway(_SpyTranscribeGateway):
    """Allows overriding `extract_record` while keeping real metering/safety behavior."""

    def __init__(self, inner: ModelGateway, extract_output: ExtractRecordOutput) -> None:
        super().__init__(inner)
        self._extract_output = extract_output

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        return self._extract_output, "extract_record@test", "fake/test"


# ============================================================================
# 1. Voice Transcribe PersonScope Isolation, Rate Limit, AiCallRow Quota, 413 & OpenAPI
# ============================================================================


@pytest.mark.asyncio
async def test_transcribe_only_strict_person_scope_prevents_cross_person_bleed(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    inner_gw = ModelGateway(
        db_container.settings,
        clock=db_container.clock,
        adapters={"fake": FakeAdapter()},
    )
    spy_gw = _SpyTranscribeGateway(inner_gw)
    db_container.extras["gateway"] = spy_gw
    app = create_app(container=db_container)

    user_id, scope, token, dek = await _provision_user(db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_a = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="sec-p-a"),
            json={"nameCn": "妈妈", "tier": "gold", "aliases": ["老妈"]},
        )
        assert r_a.status_code == 201
        person_a = UUID(r_a.json()["id"])

        r_b = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="sec-p-b"),
            json={"nameCn": "陈经理", "tier": "silver", "aliases": ["老陈秘密代号"]},
        )
        assert r_b.status_code == 201
        person_b = UUID(r_b.json()["id"])

        # Seed distinct confirmed facts on Person A and Person B
        async with db_container.db.user_session(scope) as sess:
            for pid, content in [
                (person_a, "右膝盖需要骨科复查"),
                (person_b, "绝密竞标底价代号X99"),
            ]:
                fid = new_id()
                sess.add(
                    FactRow(
                        id=fid,
                        user_id=user_id,
                        person_id=pid,
                        type=FactType.EVENT.value,
                        polarity=Polarity.NEUTRAL.value,
                        time_status=TimeStatus.CURRENT.value,
                        source_speaker=SourceSpeaker.NARRATOR.value,
                        content_enc=db_container.cipher.encrypt_str(
                            dek,
                            content,
                            user_id=user_id,
                            table="facts",
                            column="content_enc",
                            row_id=fid,
                        ),
                        status=FactStatus.CONFIRMED.value,
                        created_by="user",
                        version=1,
                    )
                )

        # Call /v1/voice/transcribe scoped to Person A -> Person B MUST NOT appear in TranscribeInput!
        wav_bytes = _make_wav_with_text("老妈说下周去复查")
        resp = await client.post(
            "/v1/voice/transcribe",
            headers=_auth(token),
            files={"audio": ("scoped.wav", wav_bytes, "audio/wav")},
            data={"activePersonId": str(person_a)},
        )
        assert resp.status_code == 200, resp.text
        assert len(spy_gw.transcribe_inputs) == 1
        scoped_in = spy_gw.transcribe_inputs[0]
        assert scoped_in.active_person_name == "妈妈"
        assert "妈妈" in scoped_in.known_person_names
        assert "老妈" in scoped_in.known_person_names
        assert "陈经理" not in scoped_in.known_person_names
        assert "老陈秘密代号" not in scoped_in.known_person_names
        assert scoped_in.recent_context_snippets == ["右膝盖需要骨科复查"]
        assert all("X99" not in s and "陈经理" not in s for s in scoped_in.recent_context_snippets)

        # Verify unscoped mode (activePersonId omitted) still passes global context
        resp_global = await client.post(
            "/v1/voice/transcribe",
            headers=_auth(token),
            files={"audio": ("global.wav", wav_bytes, "audio/wav")},
        )
        assert resp_global.status_code == 200
        global_in = spy_gw.transcribe_inputs[1]
        assert global_in.active_person_name is None
        assert "妈妈" in global_in.known_person_names
        assert "陈经理" in global_in.known_person_names


@pytest.mark.asyncio
async def test_transcribe_enforces_rate_limit_and_daily_ai_call_quota(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    # Align clock with PostgreSQL server_default UTC day so persisted AiCallRow timestamps match
    manual_clock.set(datetime.now(UTC))
    app = create_app(container=db_container)

    user_a, scope_a, token_a, _ = await _provision_user(db_container)
    _, _, token_b, _ = await _provision_user(db_container)
    wav_bytes = _make_wav_with_text("今天天气不错")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Token-bucket rate limit: capacity=20 -> 20 succeed, 21st returns 429 rate_limited
        db_container.rate_limiter.reset()
        for idx in range(20):
            r = await client.post(
                "/v1/voice/transcribe",
                headers=_auth(token_a),
                files={"audio": (f"q{idx}.wav", wav_bytes, "audio/wav")},
            )
            assert r.status_code == 200, f"call {idx} failed: {r.text}"

        r_21 = await client.post(
            "/v1/voice/transcribe",
            headers=_auth(token_a),
            files={"audio": ("q21.wav", wav_bytes, "audio/wav")},
        )
        assert r_21.status_code == 429
        assert r_21.json()["code"] == "rate_limited"

        # User B has an independent bucket and still succeeds
        r_b = await client.post(
            "/v1/voice/transcribe",
            headers=_auth(token_b),
            files={"audio": ("qb.wav", wav_bytes, "audio/wav")},
        )
        assert r_b.status_code == 200

        # 2. Daily AiCallRow quota: zero CaptureRows exist, yet AiCallRow(task='transcribe') enforces cap!
        async with db_container.db.user_session(scope_a) as sess:
            cap_count = int(await sess.scalar(select(func.count(CaptureRow.id))) or 0)
            ai_count = int(
                await sess.scalar(
                    select(func.count(AiCallRow.id)).where(
                        AiCallRow.user_id == user_a,
                        AiCallRow.task == "transcribe",
                    )
                )
                or 0
            )
            assert cap_count == 0
            assert ai_count >= 20

        db_container.rate_limiter.reset()
        db_container.quotas = QuotaService(
            db_container.clock,
            daily_voice_minutes_quota=60,
            daily_transcribe_calls_quota=20,
        )
        r_quota = await client.post(
            "/v1/voice/transcribe",
            headers=_auth(token_a),
            files={"audio": ("quota.wav", wav_bytes, "audio/wav")},
        )
        assert r_quota.status_code == 429
        assert r_quota.json()["code"] == "quota_exceeded"
        assert r_quota.json()["limitCalls"] == 20


@pytest.mark.asyncio
async def test_bounded_upload_reads_413_and_canonical_openapi_routes(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)

    # Verify canonical routes are exposed in OpenAPI schema while alias stays hidden
    openapi_paths = app.openapi()["paths"]
    assert "/v1/voice/transcribe" in openapi_paths
    assert "post" in openapi_paths["/v1/voice/transcribe"]
    assert "/v1/people/{person_id}/reminders" in openapi_paths
    assert "post" in openapi_paths["/v1/people/{person_id}/reminders"]
    assert "/v1/persons/{person_id}/reminders" not in openapi_paths

    _, _, token, _ = await _provision_user(db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_gold = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="bound-gold"),
            json={"nameCn": "妈妈", "tier": "gold"},
        )
        gold_id = r_gold.json()["id"]

        orig_audio = db_container.settings.max_audio_bytes
        orig_image = db_container.settings.max_image_bytes
        db_container.settings.max_audio_bytes = 64
        db_container.settings.max_image_bytes = 64
        try:
            oversized_wav = build_demo_wav_bytes()
            oversized_jpg = _make_clean_jpeg()
            assert len(oversized_wav) > 64
            assert len(oversized_jpg) > 64

            r_cap_audio = await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="oversize-cap-audio"),
                files={"audio": ("big.wav", oversized_wav, "audio/wav")},
            )
            assert r_cap_audio.status_code == 413

            r_cap_img = await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token, idem="oversize-cap-img"),
                files={"image": ("big.jpg", oversized_jpg, "image/jpeg")},
            )
            assert r_cap_img.status_code == 413

            r_ingest_audio = await client.post(
                "/v1/voice/ingest",
                headers=_auth(token, idem="oversize-ingest"),
                files={"audio": ("big.wav", oversized_wav, "audio/wav")},
            )
            assert r_ingest_audio.status_code == 413

            r_trans_audio = await client.post(
                "/v1/voice/transcribe",
                headers=_auth(token),
                files={"audio": ("big.wav", oversized_wav, "audio/wav")},
            )
            assert r_trans_audio.status_code == 413

            r_mat_img = await client.post(
                f"/v1/persons/{gold_id}/materials",
                headers=_auth(token, idem="oversize-mat"),
                files={"photo": ("big.jpg", oversized_jpg, "image/jpeg")},
                data={"title": "超大照片", "body": "测试超限拦截"},
            )
            assert r_mat_img.status_code == 413
        finally:
            db_container.settings.max_audio_bytes = orig_audio
            db_container.settings.max_image_bytes = orig_image


# ============================================================================
# 2. Venting Quarantine Reminder Guard & Material Soft-Delete Cascade
# ============================================================================


@pytest.mark.asyncio
async def test_venting_quarantine_never_spawns_reminders(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    user_id, scope, token, _ = await _provision_user(db_container)

    # First provision Gold person using default app
    base_app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=base_app), base_url="http://test") as client:
        r_gold = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="vent-gold"),
            json={"nameCn": "林溪", "tier": "gold"},
        )
        assert r_gold.status_code == 201
        linxi_id = UUID(r_gold.json()["id"])

    # Wire programmable gateway returning venting + DATE + PROMISE facts
    inner_gw = ModelGateway(db_container.settings, clock=db_container.clock)
    vent_output = ExtractRecordOutput(
        is_venting_or_irony=True,
        facts=[
            ExtractedFactItem(
                person_mention="林溪",
                person_id=linxi_id,
                fact_type=FactType.DATE,
                polarity=Polarity.NEGATIVE,
                title="气话日期",
                content="明天绝对不再理林溪",
                trigger_date="2026-11-24",
                confidence=0.95,
            ),
            ExtractedFactItem(
                person_mention="林溪",
                person_id=linxi_id,
                fact_type=FactType.PROMISE,
                polarity=Polarity.NEGATIVE,
                title="气话约定",
                content="下周五把东西全退给林溪",
                trigger_date="2026-12-04",
                confidence=0.92,
            ),
        ],
    )
    db_container.extras["gateway"] = _ProgrammableExtractGateway(inner_gw, vent_output)
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cap = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="vent-date-promise"),
            json={
                "source": "text",
                "text": "气死了！明天绝对不再理林溪，下周五把东西全退给她！",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert cap.status_code == 201, cap.text

    async with db_container.db.user_session(scope) as sess:
        facts = list((await sess.scalars(select(FactRow).where(FactRow.user_id == user_id))).all())
        assert len(facts) == 2
        assert all(f.status == FactStatus.PENDING.value for f in facts)
        assert all(f.pending_reason == "venting" for f in facts)

        inbox = list(
            (await sess.scalars(select(InboxItemRow).where(InboxItemRow.user_id == user_id))).all()
        )
        assert len(inbox) == 1
        assert inbox[0].kind == InboxItemKind.VENTING_HOLD.value

        reminders = list(
            (await sess.scalars(select(ReminderRow).where(ReminderRow.user_id == user_id))).all()
        )
        assert reminders == [], f"Venting quarantine MUST NOT spawn reminders, got: {reminders}"


@pytest.mark.asyncio
async def test_delete_material_cascades_soft_reject_facts_and_cancels_reminders(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    user_id, scope, token, _ = await _provision_user(db_container)

    base_app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=base_app), base_url="http://test") as client:
        r_gold = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="del-gold"),
            json={"nameCn": "妈妈", "tier": "gold"},
        )
        mom_id = UUID(r_gold.json()["id"])

    inner_gw = ModelGateway(db_container.settings, clock=db_container.clock)
    date_output = ExtractRecordOutput(
        facts=[
            ExtractedFactItem(
                person_mention="妈妈",
                person_id=mom_id,
                fact_type=FactType.PROMISE,
                polarity=Polarity.POSITIVE,
                title="骨科复查",
                content="11月28日陪妈妈去复查右膝盖",
                trigger_date="2026-11-28",
                confidence=0.96,
            )
        ]
    )
    db_container.extras["gateway"] = _ProgrammableExtractGateway(inner_gw, date_output)
    app = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cap = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="del-cap-1"),
            json={
                "source": "text",
                "text": "11月28日陪妈妈去复查右膝盖",
                "routeMode": "current",
                "activePersonId": str(mom_id),
            },
        )
        assert cap.status_code == 201, cap.text
        rec_id = UUID(cap.json()["splits"][0]["materialId"])
        fact_id = UUID(cap.json()["splits"][0]["factIds"][0])

        # Verify reminder was created as active before material deletion
        async with db_container.db.user_session(scope) as sess:
            rem_before = await sess.scalar(
                select(ReminderRow).where(
                    ReminderRow.user_id == user_id, ReminderRow.fact_id == fact_id
                )
            )
            assert rem_before is not None
            assert rem_before.status == ReminderStatus.ACTIVE.value

        # Delete the material -> MUST cascade soft-reject FactRow and cancel ReminderRow
        del_resp = await client.delete(
            f"/v1/materials/{rec_id}",
            headers=_auth(token, idem="del-mat-cascade"),
        )
        assert del_resp.status_code == 204

        async with db_container.db.user_session(scope) as sess:
            rec_after = await sess.get(RecordRow, rec_id)
            assert rec_after is not None and rec_after.deleted_at is not None

            fact_after = await sess.get(FactRow, fact_id)
            assert fact_after is not None
            assert fact_after.status == FactStatus.REJECTED.value
            assert fact_after.invalid_at is not None

            rem_after = await sess.scalar(
                select(ReminderRow).where(
                    ReminderRow.user_id == user_id, ReminderRow.fact_id == fact_id
                )
            )
            assert rem_after is not None
            assert rem_after.status == ReminderStatus.CANCELLED.value

        active_rems = await client.get("/v1/reminders?status=active", headers=_auth(token))
        assert active_rems.status_code == 200
        assert active_rems.json()["items"] == []


# ============================================================================
# 3. Silver-Tier Zero-Photo Invariant (Reject Uploads, Cloak Metadata, Block Streaming)
# ============================================================================


@pytest.mark.asyncio
async def test_silver_tier_zero_photo_reject_cloak_and_block_streaming(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    app = create_app(container=db_container)
    user_id, scope, token, dek = await _provision_user(db_container)
    jpeg_bytes = _make_clean_jpeg()
    jpeg_b64 = base64.b64encode(jpeg_bytes).decode("ascii")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create Silver person & verify ALL photo uploads are rejected with 422
        r_silver = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="silver-p-1"),
            json={"nameCn": "陈经理", "tier": "silver"},
        )
        assert r_silver.status_code == 201
        silver_id = r_silver.json()["id"]

        for slot in ("avatar", "main", "sub"):
            up_silver = await client.post(
                f"/v1/persons/{silver_id}/photos/{slot}",
                headers=_auth(token, idem=f"silver-up-{slot}"),
                files={"file": (f"{slot}.jpg", jpeg_bytes, "image/jpeg")},
            )
            assert up_silver.status_code == 422, up_silver.text
            assert up_silver.json()["field"] == "tier"

        # Reject Silver photo material via multipart, kind="photo" JSON, and photoBase64 JSON
        mat_mp = await client.post(
            f"/v1/persons/{silver_id}/materials",
            headers=_auth(token, idem="silver-mat-mp"),
            files={"photo": ("p.jpg", jpeg_bytes, "image/jpeg")},
            data={"title": "银箭照片", "body": "不应允许上传"},
        )
        assert mat_mp.status_code == 422
        assert mat_mp.json()["field"] == "kind"

        mat_kind = await client.post(
            f"/v1/persons/{silver_id}/materials",
            headers=_auth(token, idem="silver-mat-kind"),
            json={"kind": "photo", "title": "银箭照片类型", "body": "无文件也拒"},
        )
        assert mat_kind.status_code == 422
        assert mat_kind.json()["field"] == "kind"

        mat_b64 = await client.post(
            f"/v1/persons/{silver_id}/materials",
            headers=_auth(token, idem="silver-mat-b64"),
            json={
                "kind": "note",
                "title": "夹带base64",
                "body": "伪装note",
                "photoBase64": jpeg_b64,
            },
        )
        assert mat_b64.status_code == 422
        assert mat_b64.json()["field"] == "kind"

        # Plain text note on Silver person STILL succeeds
        mat_note = await client.post(
            f"/v1/persons/{silver_id}/materials",
            headers=_auth(token, idem="silver-mat-note"),
            json={"kind": "note", "title": "周会书面留痕", "body": "已邮件确认需求边界"},
        )
        assert mat_note.status_code == 201

        # 2. Gold person uploads avatar + main + sub + photo material, then transitions GOLD -> SILVER
        r_gold = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="gold-to-silver"),
            json={"nameCn": "旧友", "tier": "gold"},
        )
        assert r_gold.status_code == 201
        gold_id = r_gold.json()["id"]

        signed_photo_urls: dict[str, str] = {}
        for slot in ("avatar", "main", "sub"):
            up = await client.post(
                f"/v1/persons/{gold_id}/photos/{slot}",
                headers=_auth(token, idem=f"gold-up-{slot}"),
                files={"file": (f"{slot}.jpg", jpeg_bytes, "image/jpeg")},
            )
            assert up.status_code == 200
            url_key = {"avatar": "avatarUrl", "main": "mainPhotoUrl", "sub": "subPhotoUrl"}[slot]
            signed_photo_urls[slot] = up.json()[url_key]
            assert signed_photo_urls[slot]

        gold_photo_mat = await client.post(
            f"/v1/persons/{gold_id}/materials",
            headers=_auth(token, idem="gold-mat-photo"),
            files={"photo": ("mem.jpg", jpeg_bytes, "image/jpeg")},
            data={"title": "合照", "body": "海边合影"},
        )
        assert gold_photo_mat.status_code == 201
        photo_mat_id = gold_photo_mat.json()["id"]
        signed_mat_url = gold_photo_mat.json()["mediaUrl"]
        assert gold_photo_mat.json()["hasMedia"] is True
        assert signed_mat_url is not None

        # Seed a voice material on the same person to ensure non-photo audio stays intact
        voice_key = f"{user_id}/records/voice_{new_id()}.wav"
        await db_container.storage.put_encrypted(
            user_id=user_id,
            dek=dek,
            object_key=voice_key,
            plaintext=build_demo_wav_bytes(),
            content_type="audio/wav",
        )
        voice_mat_id = new_id()
        async with db_container.db.user_session(scope) as sess:
            sess.add(
                RecordRow(
                    id=voice_mat_id,
                    user_id=user_id,
                    person_id=UUID(gold_id),
                    kind=RecordKind.VOICE.value,
                    title_enc=db_container.cipher.encrypt_str(
                        dek,
                        "语音备忘",
                        user_id=user_id,
                        table="records",
                        column="title_enc",
                        row_id=voice_mat_id,
                    ),
                    body_enc=db_container.cipher.encrypt_str(
                        dek,
                        "语音备忘正文",
                        user_id=user_id,
                        table="records",
                        column="body_enc",
                        row_id=voice_mat_id,
                    ),
                    snippet_enc=db_container.cipher.encrypt_str(
                        dek,
                        "语音备忘正文",
                        user_id=user_id,
                        table="records",
                        column="snippet_enc",
                        row_id=voice_mat_id,
                    ),
                    media_object_key=voice_key,
                    audio_duration_ms=4000,
                    happened_on=date(2026, 11, 23),
                    review_state=RecordReviewState.NORMAL.value,
                    version=1,
                )
            )

        # While Gold, direct byte streaming works
        assert (await client.get(signed_photo_urls["avatar"])).status_code == 200
        assert (await client.get(signed_mat_url)).status_code == 200

        # Transition GOLD -> SILVER
        tr = await client.patch(
            f"/v1/persons/{gold_id}/tier",
            headers=_auth(token, idem="tr-gold-to-silver"),
            json={"tier": "silver"},
        )
        assert tr.status_code == 200
        assert tr.json()["tier"] == "silver"

        # Verify ALL metadata/URLs are cloaked on /persons/{id}, /persons, /bootstrap, /materials
        p_get = (await client.get(f"/v1/persons/{gold_id}", headers=_auth(token))).json()
        assert p_get["hasPhoto"] is False
        assert p_get["avatarUrl"] is None
        assert p_get["mainPhotoUrl"] is None
        assert p_get["subPhotoUrl"] is None

        p_list = (await client.get("/v1/persons", headers=_auth(token))).json()["items"]
        listed = next(it for it in p_list if it["id"] == gold_id)
        assert listed["hasPhoto"] is False
        assert listed["avatarUrl"] is None
        assert listed["mainPhotoUrl"] is None
        assert listed["subPhotoUrl"] is None

        boot = (await client.get("/v1/bootstrap", headers=_auth(token))).json()
        bp = boot["people"][gold_id]
        assert bp["hasPhoto"] is False
        assert bp["avatar"] is None
        assert bp["mainPhoto"] is None
        assert bp["subPhoto"] is None
        assert bp["avatarUrl"] is None
        assert bp["mainPhotoUrl"] is None
        assert bp["subPhotoUrl"] is None
        bp_photo_mat = next(m for m in bp["materials"] if m["id"] == photo_mat_id)
        assert bp_photo_mat["mediaUrl"] is None
        assert bp_photo_mat["thumb"] is None
        bp_voice_mat = next(m for m in bp["materials"] if m["id"] == str(voice_mat_id))
        assert bp_voice_mat["mediaUrl"] is not None

        mats_list = (
            await client.get(f"/v1/persons/{gold_id}/materials", headers=_auth(token))
        ).json()["items"]
        listed_photo_mat = next(m for m in mats_list if m["id"] == photo_mat_id)
        assert listed_photo_mat["hasMedia"] is False
        assert listed_photo_mat["mediaUrl"] is None

        single_photo_mat = (
            await client.get(f"/v1/materials/{photo_mat_id}", headers=_auth(token))
        ).json()
        assert single_photo_mat["hasMedia"] is False
        assert single_photo_mat["mediaUrl"] is None

        # Verify direct byte streaming is blocked (404) for BOTH Bearer JWT and old signed URLs!
        for slot in ("avatar", "main", "sub"):
            r_jwt = await client.get(f"/v1/persons/{gold_id}/photos/{slot}", headers=_auth(token))
            assert r_jwt.status_code == 404
            r_signed = await client.get(signed_photo_urls[slot])
            assert r_signed.status_code == 404

        assert (
            await client.get(f"/v1/materials/{photo_mat_id}/media", headers=_auth(token))
        ).status_code == 404
        assert (await client.get(signed_mat_url)).status_code == 404
        # Non-photo voice material media remains streamable
        assert (
            await client.get(f"/v1/materials/{voice_mat_id}/media", headers=_auth(token))
        ).status_code == 200

        # Transition back SILVER -> GOLD restores non-destructively preserved photos!
        tr_back = await client.patch(
            f"/v1/persons/{gold_id}/tier",
            headers=_auth(token, idem="tr-silver-to-gold"),
            json={"tier": "gold"},
        )
        assert tr_back.status_code == 200
        p_restored = (await client.get(f"/v1/persons/{gold_id}", headers=_auth(token))).json()
        assert p_restored["hasPhoto"] is True
        assert p_restored["avatarUrl"] is not None
        assert (
            await client.get(f"/v1/persons/{gold_id}/photos/avatar", headers=_auth(token))
        ).status_code == 200
        assert (
            await client.get(f"/v1/materials/{photo_mat_id}/media", headers=_auth(token))
        ).status_code == 200


# ============================================================================
# 4. POST /v1/people/{id}/reminders: PersonScope Fact Check, PK 409, Same-Day Post-09:00
# ============================================================================


@pytest.mark.asyncio
async def test_person_reminders_fact_scope_pk_conflict_and_same_day_post_0900(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    # 14:30 Asia/Shanghai (06:30 UTC) — well past default 09:00 fire time!
    now_utc = datetime(2026, 11, 23, 6, 30, 0, tzinfo=UTC)
    manual_clock.set(now_utc)
    app = create_app(container=db_container)

    user_a, scope_a, token_a, dek_a = await _provision_user(db_container)
    _, _, token_b, _ = await _provision_user(db_container)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_mom = await client.post(
            "/v1/persons",
            headers=_auth(token_a, idem="rem-mom"),
            json={"nameCn": "妈妈", "tier": "gold"},
        )
        mom_id = UUID(r_mom.json()["id"])

        r_b = await client.post(
            "/v1/persons",
            headers=_auth(token_b, idem="rem-b-p"),
            json={"nameCn": "林屿", "tier": "gold"},
        )
        b_person_id = UUID(r_b.json()["id"])

        # Seed 3 ineligible facts for User A: person_id=None, status=rejected, invalid_at!=None
        null_pid_fact = new_id()
        rejected_fact = new_id()
        invalidated_fact = new_id()
        async with db_container.db.user_session(scope_a) as sess:
            for fid, pid, st, inv in [
                (null_pid_fact, None, FactStatus.CONFIRMED.value, None),
                (rejected_fact, mom_id, FactStatus.REJECTED.value, now_utc),
                (invalidated_fact, mom_id, FactStatus.CONFIRMED.value, now_utc),
            ]:
                sess.add(
                    FactRow(
                        id=fid,
                        user_id=user_a,
                        person_id=pid,
                        type=FactType.DATE.value,
                        polarity=Polarity.NEUTRAL.value,
                        time_status=TimeStatus.CURRENT.value,
                        source_speaker=SourceSpeaker.NARRATOR.value,
                        content_enc=db_container.cipher.encrypt_str(
                            dek_a,
                            "测试事实",
                            user_id=user_a,
                            table="facts",
                            column="content_enc",
                            row_id=fid,
                        ),
                        status=st,
                        invalid_at=inv,
                        created_by="user",
                        version=1,
                    )
                )

        # All 3 ineligible facts MUST be rejected with 404
        for label, bad_fid in [
            ("null-person", null_pid_fact),
            ("rejected", rejected_fact),
            ("invalidated", invalidated_fact),
        ]:
            resp = await client.post(
                f"/v1/people/{mom_id}/reminders",
                headers=_auth(token_a, idem=f"bad-fact-{label}"),
                json={
                    "title": "非法事实绑定",
                    "triggerDate": "2026-11-28",
                    "factId": str(bad_fid),
                },
            )
            assert resp.status_code == 404, f"{label} should return 404, got {resp.status_code}"

        # Same-day after 09:00 (2026-11-23 at 14:30 Asia/Shanghai) -> 201 with now + 5m fallback!
        same_day = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=_auth(token_a, idem="same-day-post-0900"),
            json={"title": "今晚给妈妈打电话", "onceDate": "2026-11-23"},
        )
        assert same_day.status_code == 201, same_day.text
        sd_body = same_day.json()
        assert sd_body["status"] == "active"
        assert sd_body["nextLeadDay"] == 0
        expected_fire = now_utc + timedelta(minutes=5)
        actual_fire = datetime.fromisoformat(sd_body["nextFireAt"].replace("Z", "+00:00"))
        assert actual_fire == expected_fire

        # Pause & resume the same-day post-09:00 reminder also succeeds cleanly
        sd_id = sd_body["id"]
        p_resp = await client.patch(
            f"/v1/reminders/{sd_id}",
            headers=_auth(token_a, idem="sd-pause", if_match='"1"'),
            json={"status": "paused"},
        )
        assert p_resp.status_code == 200
        res_resp = await client.patch(
            f"/v1/reminders/{sd_id}",
            headers=_auth(token_a, idem="sd-resume", if_match='"2"'),
            json={"status": "active"},
        )
        assert res_resp.status_code == 200
        assert res_resp.json()["nextLeadDay"] == 0

        # Strictly past date (yesterday) STILL returns 422
        past = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=_auth(token_a, idem="past-date"),
            json={"title": "昨天的提醒", "onceDate": "2026-11-22"},
        )
        assert past.status_code == 422

        # PK 409 conflict: same-tenant AND cross-tenant PK collision both return 409 (NOT 500)!
        fixed_rem_id = str(new_id())
        first_create = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=_auth(token_a, idem="pk-first"),
            json={"id": fixed_rem_id, "title": "首个提醒", "onceDate": "2026-11-28"},
        )
        assert first_create.status_code == 201

        dup_same_user = await client.post(
            f"/v1/people/{mom_id}/reminders",
            headers=_auth(token_a, idem="pk-dup-same"),
            json={"id": fixed_rem_id, "title": "重复PK同租户", "onceDate": "2026-11-28"},
        )
        assert dup_same_user.status_code == 409
        assert dup_same_user.json()["code"] == "conflict"

        dup_cross_user = await client.post(
            f"/v1/people/{b_person_id}/reminders",
            headers=_auth(token_b, idem="pk-dup-cross"),
            json={"id": fixed_rem_id, "title": "重复PK跨租户", "onceDate": "2026-11-28"},
        )
        assert dup_cross_user.status_code == 409
        assert dup_cross_user.json()["code"] == "conflict"


# ============================================================================
# 5. Auto-Resolve FACT_CONFIRM Inbox on PATCH /v1/facts/{id}
# ============================================================================


@pytest.mark.asyncio
async def test_patch_fact_auto_resolves_fact_confirm_inbox_and_syncs_capture(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    manual_clock.set(datetime(2026, 11, 23, 2, 0, 0, tzinfo=UTC))
    user_id, scope, token, _ = await _provision_user(db_container)

    base_app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=base_app), base_url="http://test") as client:
        r_gold = await client.post(
            "/v1/persons",
            headers=_auth(token, idem="pf-gold"),
            json={"nameCn": "林溪", "tier": "gold"},
        )
        linxi_id = UUID(r_gold.json()["id"])

    # Scenario A: single low-confidence fact -> PATCH status=confirmed resolves FACT_CONFIRM & completes capture
    inner_gw = ModelGateway(db_container.settings, clock=db_container.clock)
    single_low_conf = ExtractRecordOutput(
        facts=[
            ExtractedFactItem(
                person_mention="林溪",
                person_id=linxi_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.POSITIVE,
                title="饮品偏好",
                content="可能喜欢桂花乌龙",
                confidence=0.55,
            )
        ]
    )
    db_container.extras["gateway"] = _ProgrammableExtractGateway(inner_gw, single_low_conf)
    app_a = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app_a), base_url="http://test") as client:
        cap_a = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="pf-cap-a"),
            json={
                "source": "text",
                "text": "林溪可能喜欢桂花乌龙",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert cap_a.status_code == 201
        cap_a_id = UUID(cap_a.json()["captureId"])
        fact_a_id = UUID(cap_a.json()["splits"][0]["factIds"][0])
        inbox_a_id = UUID(cap_a.json()["inboxItemIds"][0])
        assert cap_a.json()["status"] == "needs_review"

        patch_a = await client.patch(
            f"/v1/facts/{fact_a_id}",
            headers=_auth(token, idem="pf-confirm-a", if_match='"1"'),
            json={"status": "confirmed"},
        )
        assert patch_a.status_code == 200
        assert patch_a.json()["status"] == "confirmed"

        async with db_container.db.user_session(scope) as sess:
            ib_a = await sess.get(InboxItemRow, inbox_a_id)
            assert ib_a is not None
            assert ib_a.status == InboxItemStatus.RESOLVED.value
            assert ib_a.resolution_action == "confirm"
            assert ib_a.resolved_at is not None

            cap_row_a = await sess.get(CaptureRow, cap_a_id)
            assert cap_row_a is not None
            assert cap_row_a.status == "completed"

    # Scenario B: two low-confidence facts on one FACT_CONFIRM item -> stays open after 1st, resolves after 2nd
    two_low_conf = ExtractRecordOutput(
        facts=[
            ExtractedFactItem(
                person_mention="林溪",
                person_id=linxi_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.POSITIVE,
                title="电影偏好",
                content="可能喜欢纪录片",
                confidence=0.52,
            ),
            ExtractedFactItem(
                person_mention="林溪",
                person_id=linxi_id,
                fact_type=FactType.PREFERENCE,
                polarity=Polarity.POSITIVE,
                title="音乐偏好",
                content="可能喜欢爵士乐",
                confidence=0.53,
            ),
        ]
    )
    db_container.extras["gateway"] = _ProgrammableExtractGateway(inner_gw, two_low_conf)
    app_b = create_app(container=db_container)

    async with AsyncClient(transport=ASGITransport(app=app_b), base_url="http://test") as client:
        cap_b = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="pf-cap-b"),
            json={
                "source": "text",
                "text": "林溪可能喜欢纪录片和爵士乐",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert cap_b.status_code == 201
        cap_b_id = UUID(cap_b.json()["captureId"])
        fact_b1, fact_b2 = (UUID(fid) for fid in cap_b.json()["splits"][0]["factIds"])
        inbox_b_id = UUID(cap_b.json()["inboxItemIds"][0])

        # Reject 1st fact -> inbox stays OPEN because 2nd fact is still PENDING
        p_b1 = await client.patch(
            f"/v1/facts/{fact_b1}",
            headers=_auth(token, idem="pf-rej-b1", if_match='"1"'),
            json={"status": "rejected"},
        )
        assert p_b1.status_code == 200

        async with db_container.db.user_session(scope) as sess:
            ib_mid = await sess.get(InboxItemRow, inbox_b_id)
            assert ib_mid is not None
            assert ib_mid.status == InboxItemStatus.OPEN.value

        # Reject 2nd fact -> inbox auto-resolves with action="discard" & capture becomes completed
        p_b2 = await client.patch(
            f"/v1/facts/{fact_b2}",
            headers=_auth(token, idem="pf-rej-b2", if_match='"1"'),
            json={"status": "rejected"},
        )
        assert p_b2.status_code == 200

        async with db_container.db.user_session(scope) as sess:
            ib_end = await sess.get(InboxItemRow, inbox_b_id)
            assert ib_end is not None
            assert ib_end.status == InboxItemStatus.RESOLVED.value
            assert ib_end.resolution_action == "discard"
            assert ib_end.resolved_at is not None

            cap_row_b = await sess.get(CaptureRow, cap_b_id)
            assert cap_row_b is not None
            assert cap_row_b.status == "completed"

        # Sanity: no open inbox items remain for this user
        open_inbox = (await client.get("/v1/inbox?status=open", headers=_auth(token))).json()
        assert open_inbox["items"] == []
        _ = user_id  # referenced for clarity
