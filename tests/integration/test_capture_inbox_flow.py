"""Live PostgreSQL integration tests for Multimodal Capture, V10 `/voice/ingest`,
GatePolicy, Screenshot Privacy, 5s Undo, Materials/Facts CRUD, Pending Inbox (all 6 actions),
72h Venting Hold Expiry, and RLS Tenant Isolation."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.ai.schemas.contracts import (
    CompanionChatInput,
    CompanionChatOutput,
    CrisisCardPayload,
    ExtractedFactItem,
    ExtractedPersonCandidate,
    ExtractRecordInput,
    ExtractRecordOutput,
    SafetyClassifyOutput,
    SafetyDecision,
    ScreenshotBubble,
    ScreenshotParseOutput,
    TranscribeInput,
    TranscribeOutput,
)
from mindweave.core.clock import ManualClock
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    CaptureRow,
    InboxItemRow,
    PersonRow,
    RecordRow,
    ReminderRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.modules.capture import handle_process_capture_job
from mindweave.modules.capture.api import router as capture_router
from mindweave.modules.memory import expire_stale_venting_items
from mindweave.modules.memory.api import router as memory_router
from mindweave.modules.memory.infra.repository import create_person_with_primary_alias
from mindweave.shared.enums import (
    FactType,
    PersonState,
    Polarity,
    SafetyCategory,
    SourceSpeaker,
    Tier,
)
from mindweave.shared.scopes import UserScope

VALID_WEBM_BYTES = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81mindweave-voice-clip"


def _make_jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (64, 64), color=(210, 175, 140))
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 17 Pro"
    exif[0x0131] = "Location-Sensitive-Camera"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


@dataclass
class ProgrammableGateway:
    """Deterministic fake `ModelGatewayPort` for testing all capture & modality branches."""

    transcript_text: str = ""
    duration_ms: int = 4200
    screenshot_output: ScreenshotParseOutput | None = None
    extract_handler: Callable[[UserScope, ExtractRecordInput], ExtractRecordOutput] | None = None
    extract_calls: list[ExtractRecordInput] = field(default_factory=list)

    async def transcribe(
        self,
        *,
        scope: UserScope,
        payload: TranscribeInput,
        session: AsyncSession | None = None,
    ) -> TranscribeOutput:
        return TranscribeOutput(
            raw_transcript=self.transcript_text,
            corrected_transcript=self.transcript_text,
            duration_ms=self.duration_ms,
        )

    async def parse_screenshot(
        self,
        *,
        scope: UserScope,
        image_bytes: bytes,
        mime_type: str,
        target_person_name: str | None = None,
        session: AsyncSession | None = None,
    ) -> ScreenshotParseOutput:
        if self.screenshot_output is not None:
            return self.screenshot_output
        return ScreenshotParseOutput(
            is_group_chat=False,
            bubbles=[
                ScreenshotBubble(
                    speaker=SourceSpeaker.TARGET_PERSON,
                    speaker_label=target_person_name or "对方",
                    text="我不吃折耳根",
                )
            ],
            formatted_transcript=f"[LEFT_BUBBLE/{target_person_name or '对方'}]: 我不吃折耳根",
        )

    async def extract_record(
        self,
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        self.extract_calls.append(payload)
        if self.extract_handler is not None:
            out = self.extract_handler(scope, payload)
            return out, "extract_record_v1.0", "fake-fast-v1"
        return (
            ExtractRecordOutput(
                facts=[
                    ExtractedFactItem(
                        person_mention="",
                        fact_type=FactType.PREFERENCE,
                        polarity=Polarity.POSITIVE,
                        title="日常偏好",
                        content=payload.text[:80],
                        confidence=0.94,
                    )
                ]
            ),
            "extract_record_v1.0",
            "fake-fast-v1",
        )

    async def generate_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> tuple[CompanionChatOutput, str, str]:
        raise NotImplementedError

    async def stream_companion_reply(
        self,
        *,
        scope: UserScope,
        payload: CompanionChatInput,
        session: AsyncSession | None = None,
    ) -> AsyncIterator[str]:
        if False:
            yield ""

    async def classify_safety(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyClassifyOutput:
        return SafetyClassifyOutput(category=SafetyCategory.NORMAL)

    async def summarize_thread(
        self,
        *,
        scope: UserScope,
        person_name: str,
        previous_summary: str | None,
        turns_text: str,
        session: AsyncSession | None = None,
    ) -> str:
        return ""


@dataclass
class ProgrammableSafetyService:
    """Deterministic `SafetyServicePort` triggering crisis card on distress keywords."""

    async def evaluate(
        self,
        *,
        scope: UserScope,
        text: str,
        session: AsyncSession | None = None,
    ) -> SafetyDecision:
        if "不想活" in text or "撑不下去" in text:
            return SafetyDecision(
                category=SafetyCategory.CRISIS,
                block_normal_reply=True,
                trigger_source="l1",
                crisis_card=CrisisCardPayload(
                    template_id="crisis_self_harm_v1",
                    title="你不必独自承受这一切",
                    care_message="此刻请先照顾好自己的安全，随时可以联系专业援助热线。",
                    hotlines=[{"name": "全国心理援助热线", "phone": "12356"}],
                    disclaimer="心络并非医疗或危机干预机构。",
                ),
            )
        return SafetyDecision(category=SafetyCategory.NORMAL)


@dataclass
class InMemoryJobQueue:
    """Test `JobQueuePort` recording enqueued background jobs."""

    jobs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

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
        self.jobs.append((task_name, dict(payload)))
        return new_id()


async def _provision_user(
    container: AppContainer,
    *,
    timezone: str = "Asia/Shanghai",
) -> tuple[UserScope, str, bytes]:
    if isinstance(container.clock, ManualClock) and container.clock.now() > datetime.now(UTC):
        container.clock.set(datetime.now(UTC) - timedelta(hours=80))

    user_id = new_id()
    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=user_id, status="active"))

    scope = UserScope(user_id=user_id)
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(user_id)
    async with container.db.user_session(scope) as sess:
        sess.add(
            UserKeyRow(
                user_id=user_id,
                wrapped_dek=wrapped_dek,
                kek_id=kek_id,
            )
        )
        sess.add(
            UserPreferenceRow(
                user_id=user_id,
                timezone=timezone,
            )
        )
        await sess.flush()
        dek = await container.load_user_dek(sess, scope)

    token, _ = container.tokens.create_access_token(user_id, now=container.clock.now())
    return scope, token, dek


async def _seed_person(
    container: AppContainer,
    scope: UserScope,
    dek: bytes,
    *,
    display_name: str,
    tier: Tier = Tier.CRYSTAL,
    aliases: tuple[str, ...] = (),
) -> UUID:
    from mindweave.core.orm import PersonAliasRow
    from mindweave.modules.memory.infra.repository import normalize_alias_for_index

    now = container.clock.now()
    async with container.db.user_session(scope) as sess:
        person = await create_person_with_primary_alias(
            sess,
            scope=scope,
            dek=dek,
            cipher=container.cipher,
            display_name=display_name,
            tier=tier,
            state=PersonState.ACTIVE,
            now=now,
        )
        for extra_alias in aliases:
            aid = new_id()
            sess.add(
                PersonAliasRow(
                    id=aid,
                    user_id=scope.user_id,
                    person_id=person.id,
                    alias_enc=container.cipher.encrypt_str(
                        dek,
                        extra_alias,
                        user_id=scope.user_id,
                        table="person_aliases",
                        column="alias_enc",
                        row_id=aid,
                    ),
                    alias_hmac=container.cipher.compute_blind_index(
                        dek,
                        user_id=scope.user_id,
                        domain="alias",
                        normalized_value=normalize_alias_for_index(extra_alias),
                    ),
                    kind="nickname",
                    is_primary=False,
                    created_at=now,
                )
            )
        return person.id


def _build_test_app(
    container: AppContainer,
    gateway: ProgrammableGateway,
    safety: ProgrammableSafetyService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    container.extras["gateway"] = gateway
    container.extras["safety"] = safety or ProgrammableSafetyService()
    register_exception_handlers(app)
    app.include_router(capture_router, prefix="/v1")
    app.include_router(memory_router, prefix="/v1")
    return app


def _auth(token: str, idem: str | None = None, if_match: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


# ============================================================================
# 1. V10 `/v1/voice/ingest` Global Split + Provisional Person + Media Stream
# ============================================================================


@pytest.mark.asyncio
async def test_v10_voice_ingest_global_splits_new_person_reminder_and_media_stream(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(
        db_container, scope, dek, display_name="林溪", tier=Tier.GOLD, aliases=("溪溪",)
    )
    mom_id = await _seed_person(
        db_container, scope, dek, display_name="妈妈", tier=Tier.SILVER, aliases=("老妈",)
    )

    gateway = ProgrammableGateway(
        transcript_text="林溪不能吃香菜，妈妈下周日2026-11-22体检，王总海鲜过敏",
        duration_ms=8200,
    )

    def _extract(_scope: UserScope, _inp: ExtractRecordInput) -> ExtractRecordOutput:
        return ExtractRecordOutput(
            persons=[
                ExtractedPersonCandidate(
                    mention="林溪", matched_person_id=linxi_id, confidence=0.98
                ),
                ExtractedPersonCandidate(mention="妈妈", matched_person_id=mom_id, confidence=0.97),
                ExtractedPersonCandidate(mention="王总", is_new_person=True, confidence=0.91),
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="饮食忌口",
                    content="不能吃香菜",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="妈妈",
                    person_id=mom_id,
                    fact_type=FactType.DATE,
                    polarity=Polarity.NEUTRAL,
                    title="年度体检",
                    content="2026-11-22 医院体检",
                    trigger_date="2026-11-22",
                    confidence=0.94,
                ),
                ExtractedFactItem(
                    person_mention="王总",
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="宴请忌口",
                    content="海鲜过敏",
                    confidence=0.92,
                ),
            ],
        )

    gateway.extract_handler = _extract
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/voice/ingest",
            headers=_auth(token, idem="v10-ingest-1"),
            data={"routeMode": "global", "activePersonId": ""},
            files={"audio": ("clip.webm", VALID_WEBM_BYTES, "audio/webm")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["routeMode"] == "global"
        assert "<mark>林溪</mark>" in body["transcriptHtml"]
        assert "<mark>妈妈</mark>" in body["transcriptHtml"]
        assert "<mark>王总</mark>" in body["transcriptHtml"]
        assert body["pendingCount"] == 2
        assert body["undoDeadline"] is not None
        assert len(body["splits"]) == 3

        by_name = {s["personName"]: s for s in body["splits"]}
        assert by_name["林溪"]["tier"] == "gold"
        assert by_name["林溪"]["isNewPerson"] is False
        assert by_name["林溪"]["dur"] == "0:08"

        assert by_name["王总"]["tier"] == "crystal"
        assert by_name["王总"]["isNewPerson"] is True

        # Idempotent replay on /v1/voice/ingest returns identical VoiceIngestResponse
        replay = await client.post(
            "/v1/voice/ingest",
            headers=_auth(token, idem="v10-ingest-1"),
            data={"routeMode": "global", "activePersonId": ""},
            files={"audio": ("clip.webm", VALID_WEBM_BYTES, "audio/webm")},
        )
        assert replay.status_code == 200
        assert replay.json()["jobId"] == body["jobId"]

        # Stream decrypted audio bytes from material endpoint
        linxi_mat_id = by_name["林溪"]["materialId"]
        media_resp = await client.get(f"/v1/materials/{linxi_mat_id}/media", headers=_auth(token))
        assert media_resp.status_code == 200
        assert media_resp.headers["content-type"].startswith("audio/webm")
        assert media_resp.content == VALID_WEBM_BYTES

        # 林溪 taboo fact is immediately confirmed; 妈妈 date fact is pending
        linxi_facts = await client.get(
            f"/v1/persons/{linxi_id}/facts?status=confirmed", headers=_auth(token)
        )
        assert linxi_facts.status_code == 200
        assert [f["content"] for f in linxi_facts.json()["items"]] == ["不能吃香菜"]

    # Verify DB state: provisional person has_photo=False + proposed reminder created for 妈妈
    async with db_container.db.user_session(scope) as sess:
        wang_id = UUID(by_name["王总"]["personId"])
        wang_row = await sess.get(PersonRow, wang_id)
        assert wang_row is not None
        assert wang_row.state == "provisional"
        assert wang_row.tier == "crystal"
        assert wang_row.has_photo is False

        rem_row = await sess.scalar(select(ReminderRow).where(ReminderRow.person_id == mom_id))
        assert rem_row is not None
        assert rem_row.status == "proposed"


# ============================================================================
# 2. Scoped `routeMode="current"` Lock & Cross-Person Split
# ============================================================================


@pytest.mark.asyncio
async def test_scoped_current_mode_requires_person_and_isolates_cross_person_clause(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    mom_id = await _seed_person(db_container, scope, dek, display_name="妈妈", tier=Tier.SILVER)

    gateway = ProgrammableGateway()

    def _extract(_scope: UserScope, _inp: ExtractRecordInput) -> ExtractRecordOutput:
        return ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="她",
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="甜点偏好",
                    content="喜欢吃抹茶千层",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="妈妈",
                    person_id=mom_id,
                    fact_type=FactType.WISH,
                    polarity=Polarity.POSITIVE,
                    title="买营养品",
                    content="想要深海鱼油",
                    confidence=0.93,
                ),
            ]
        )

    gateway.extract_handler = _extract
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing activePersonId in current mode -> 400 Bad Request
        bad_resp = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="scoped-missing"),
            json={"source": "text", "text": "她喜欢抹茶", "routeMode": "current"},
        )
        assert bad_resp.status_code == 400

        ok_resp = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="scoped-ok"),
            json={
                "source": "text",
                "text": "她喜欢吃抹茶千层，另外妈妈想要深海鱼油",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
                "sync": True,
            },
        )
        assert ok_resp.status_code == 201, ok_resp.text
        data = ok_resp.json()
        # Only active person appears in current-page splits; cross-person went to mom's pending inbox
        assert [s["personId"] for s in data["splits"]] == [str(linxi_id)]
        assert data["pendingCount"] == 1

        linxi_facts = (
            await client.get(f"/v1/persons/{linxi_id}/facts", headers=_auth(token))
        ).json()["items"]
        assert [f["content"] for f in linxi_facts] == ["喜欢吃抹茶千层"]
        assert linxi_facts[0]["status"] == "confirmed"

        mom_inbox = (await client.get(f"/v1/inbox?personId={mom_id}", headers=_auth(token))).json()[
            "items"
        ]
        assert len(mom_inbox) == 1
        assert mom_inbox[0]["facts"][0]["content"] == "想要深海鱼油"
        assert mom_inbox[0]["facts"][0]["status"] == "pending"


# ============================================================================
# 3. Invariant I2 Zero-Data-Loss Pre-AI Commit & `saved_as_note` Fallback
# ============================================================================


@pytest.mark.asyncio
async def test_invariant_i2_commits_received_capture_before_gateway_and_falls_back_on_error(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)

    observed_pre_ai_statuses: list[str] = []
    gateway = ProgrammableGateway()

    def _exploding_extract(call_scope: UserScope, _inp: ExtractRecordInput) -> ExtractRecordOutput:
        # We can't await inside sync callback, so we raise after recording input;
        # we also verify pre-commit via async wrapper below!
        raise RuntimeError("Upstream Gemini 503 outage")

    gateway.extract_handler = _exploding_extract

    # Wrap extract_record on gateway instance to inspect committed PostgreSQL state mid-flight!
    orig_extract = gateway.extract_record

    async def _inspecting_extract(
        *,
        scope: UserScope,
        payload: ExtractRecordInput,
        session: AsyncSession | None = None,
    ) -> tuple[ExtractRecordOutput, str, str]:
        async with db_container.db.user_session(scope) as independent_sess:
            rows = (await independent_sess.scalars(select(CaptureRow))).all()
            observed_pre_ai_statuses.extend(r.status for r in rows)
        return await orig_extract(scope=scope, payload=payload, session=session)

    gateway.extract_record = _inspecting_extract  # type: ignore[method-assign]
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="i2-fallback-1"),
            json={
                "source": "text",
                "text": "重要手记：林溪老家外婆下个月过八十大寿",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
                "sync": True,
            },
        )
        assert resp.status_code == 201, resp.text
        # Prove CaptureRow(status='received') was ALREADY committed in PostgreSQL before AI ran!
        assert observed_pre_ai_statuses == ["received"]

        payload = resp.json()
        assert payload["status"] == "saved_as_note"
        assert payload["pendingCount"] == 1
        assert len(payload["splits"]) == 1
        assert "八十大寿" in payload["splits"][0]["body"]

        # Verify fallback RecordRow + FactRow(type='note', pending_reason='extraction_failed') + InboxItemRow
        inbox = (await client.get("/v1/inbox", headers=_auth(token))).json()["items"]
        assert len(inbox) == 1
        assert inbox[0]["facts"][0]["type"] == "note"
        assert inbox[0]["facts"][0]["status"] == "pending"
        assert inbox[0]["facts"][0]["pendingReason"] == "extraction_failed"


# ============================================================================
# 4. Screenshot Right-Bubble Filter, EXIF Stripping & Ephemeral Media Shield
# ============================================================================


@pytest.mark.asyncio
async def test_screenshot_right_bubble_filter_ephemeral_deletion_and_exif_strip(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)

    gateway = ProgrammableGateway(
        screenshot_output=ScreenshotParseOutput(
            is_group_chat=False,
            bubbles=[
                ScreenshotBubble(
                    speaker=SourceSpeaker.TARGET_PERSON,
                    speaker_label="林溪",
                    text="我不吃折耳根",
                ),
                ScreenshotBubble(
                    speaker=SourceSpeaker.USER_SELF,
                    speaker_label="我",
                    text="我严重香菜过敏，下周五带你去吃怀石料理",
                ),
            ],
        )
    )

    def _shot_extract(_scope: UserScope, _inp: ExtractRecordInput) -> ExtractRecordOutput:
        return ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    source_speaker=SourceSpeaker.TARGET_PERSON,
                    title="忌口",
                    content="不吃折耳根",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    source_speaker=SourceSpeaker.USER_SELF,
                    title="误提忌口",
                    content="严重香菜过敏",
                    confidence=0.95,
                ),
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.PROMISE,
                    polarity=Polarity.POSITIVE,
                    source_speaker=SourceSpeaker.USER_SELF,
                    title="周末约定",
                    content="下周五带去吃怀石料理",
                    trigger_date="2026-11-20",
                    confidence=0.93,
                ),
            ]
        )

    gateway.extract_handler = _shot_extract
    app = _build_test_app(db_container, gateway)
    jpeg_with_exif = _make_jpeg_with_exif()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Case A: Default ephemeral screenshot (`keepOriginalMedia` omitted -> False)
        resp_ephemeral = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="shot-ephemeral"),
            data={"routeMode": "current", "activePersonId": str(linxi_id), "sync": "true"},
            files={"image": ("chat.jpg", jpeg_with_exif, "image/jpeg")},
        )
        assert resp_ephemeral.status_code == 201, resp_ephemeral.text
        eph_data = resp_ephemeral.json()
        eph_mat_id = eph_data["splits"][0]["materialId"]

        # Ephemeral screenshot MUST NOT remain stored on material
        eph_media = await client.get(f"/v1/materials/{eph_mat_id}/media", headers=_auth(token))
        assert eph_media.status_code == 404

        # Verify USER_SELF taboo ("严重香菜过敏") was stripped, while TARGET_PERSON taboo & USER_SELF promise kept!
        all_facts = (
            await client.get(f"/v1/persons/{linxi_id}/facts", headers=_auth(token))
        ).json()["items"]
        contents = {f["content"]: f["status"] for f in all_facts}
        assert "严重香菜过敏" not in contents
        assert contents["不吃折耳根"] == "confirmed"
        assert contents["下周五带去吃怀石料理"] == "pending"

        # Case B: Explicit `keepOriginalMedia=true` encrypts EXIF-stripped image
        resp_keep = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="shot-keep"),
            data={
                "routeMode": "current",
                "activePersonId": str(linxi_id),
                "keepOriginalMedia": "true",
                "sync": "true",
            },
            files={"image": ("chat_keep.jpg", jpeg_with_exif, "image/jpeg")},
        )
        assert resp_keep.status_code == 201
        keep_mat_id = resp_keep.json()["splits"][0]["materialId"]
        keep_media = await client.get(f"/v1/materials/{keep_mat_id}/media", headers=_auth(token))
        assert keep_media.status_code == 200
        with Image.open(io.BytesIO(keep_media.content)) as stored_img:
            assert len(stored_img.getexif()) == 0


# ============================================================================
# 5. Safety Crisis Card + Async Queue SSE `/events` + 5s Undo & Idempotency
# ============================================================================


@pytest.mark.asyncio
async def test_safety_crisis_card_async_sse_and_5s_undo_lifecycle(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)

    gateway = ProgrammableGateway()

    def _extract(_scope: UserScope, inp: ExtractRecordInput) -> ExtractRecordOutput:
        return ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.PROMISE,
                    polarity=Polarity.POSITIVE,
                    title="陪聊约定",
                    content=f"周末一起散步（{inp.text[:12]}）",
                    trigger_date="2026-11-21",
                    confidence=0.92,
                )
            ]
        )

    gateway.extract_handler = _extract
    queue = InMemoryJobQueue()
    db_container.extras["job_queue"] = queue
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 5a. Crisis input returns crisisCard while safely storing capture
        crisis_resp = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="crisis-cap"),
            json={
                "source": "text",
                "text": "最近真的撑不下去了，还好林溪陪着我",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert crisis_resp.status_code == 201
        c_json = crisis_resp.json()
        assert c_json["safetyCategory"] == "crisis"
        assert c_json["crisisCard"] is not None
        assert c_json["crisisCard"]["hotlines"][0]["phone"] == "12356"

        # 5b. Async queued capture (default when job_queue is present and sync not set) -> 202
        queued_resp = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="async-cap-1"),
            json={
                "source": "text",
                "text": "答应林溪周末一起散步",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert queued_resp.status_code == 202
        cap_id = queued_resp.json()["captureId"]
        assert queued_resp.json()["status"] == "received"
        assert len(queue.jobs) == 1
        assert queue.jobs[0][0] == "capture.process"

        # Reusing same Idempotency-Key with DIFFERENT payload -> 409 idempotency_conflict
        conflict_idem = await client.post(
            "/v1/captures",
            headers=_auth(token, idem="async-cap-1"),
            json={
                "source": "text",
                "text": "完全不同的另一段话",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert conflict_idem.status_code == 409
        assert conflict_idem.json()["code"] == "idempotency_conflict"

        # Run worker handler, then stream SSE `/v1/captures/{id}/events`
        await handle_process_capture_job(db_container, queue.jobs[0][1])

        sse_resp = await client.get(f"/v1/captures/{cap_id}/events", headers=_auth(token))
        assert sse_resp.status_code == 200
        sse_text = sse_resp.text
        assert "event: transcript" in sse_text
        assert "event: splits" in sse_text
        assert "event: needs_review" in sse_text
        assert "event: done" in sse_text

        # 5c. Undo within 5-second window (advance 3s <= 5s)
        manual_clock.advance(seconds=3)
        undo_ok = await client.post(
            f"/v1/captures/{cap_id}:undo",
            headers=_auth(token, idem="undo-cap-1"),
        )
        assert undo_ok.status_code == 200, undo_ok.text
        u_json = undo_ok.json()
        assert u_json["status"] == "reverted"
        assert u_json["revertedRecordCount"] == 1
        assert u_json["rejectedFactCount"] == 1
        assert u_json["cancelledReminderCount"] == 1

        # Idempotent replay of undo
        undo_replay = await client.post(
            f"/v1/captures/{cap_id}:undo",
            headers=_auth(token, idem="undo-cap-1"),
        )
        assert undo_replay.status_code == 200
        assert undo_replay.json()["status"] == "reverted"

        # Verify DB reverted state
        after_get = await client.get(f"/v1/captures/{cap_id}", headers=_auth(token))
        assert after_get.json()["status"] == "reverted"

        # 5d. Undo AFTER 5-second window expires -> 409 undo_window_expired
        manual_clock.advance(seconds=6)
        expired_undo = await client.post(
            f"/v1/captures/{c_json['captureId']}:undo",
            headers=_auth(token, idem="undo-expired"),
        )
        assert expired_undo.status_code == 409
        assert expired_undo.json()["code"] == "undo_window_expired"


# ============================================================================
# 6. Materials & Facts CRUD, Optimistic Locking (412), & Bi-Temporal History
# ============================================================================


@pytest.mark.asyncio
async def test_materials_and_facts_crud_optimistic_locking_and_bitemporal_supersession(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    app = _build_test_app(db_container, ProgrammableGateway())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create 2 manual materials and test cursor pagination + optimistic locking
        m1 = await client.post(
            f"/v1/persons/{linxi_id}/materials",
            headers=_auth(token, idem="mat-1"),
            json={"kind": "note", "title": "初见手札", "body": "在街角咖啡馆聊了整晚电影"},
        )
        assert m1.status_code == 201
        mat1_id = m1.json()["id"]

        m2 = await client.post(
            f"/v1/persons/{linxi_id}/materials",
            headers=_auth(token, idem="mat-2"),
            json={"kind": "note", "title": "秋日散步", "body": "梧桐落叶很好看"},
        )
        assert m2.status_code == 201

        page1 = (
            await client.get(f"/v1/persons/{linxi_id}/materials?limit=1", headers=_auth(token))
        ).json()
        assert len(page1["items"]) == 1
        assert page1["hasMore"] is True
        page2 = (
            await client.get(
                f"/v1/persons/{linxi_id}/materials?limit=1&cursor={page1['nextCursor']}",
                headers=_auth(token),
            )
        ).json()
        assert len(page2["items"]) == 1

        # Missing Idempotency-Key on PATCH / DELETE is rejected (P1-7)
        no_idem_patch = await client.patch(
            f"/v1/materials/{mat1_id}",
            headers=_auth(token, if_match='"1"'),
            json={"title": "缺少幂等键"},
        )
        assert no_idem_patch.status_code == 400

        # PATCH material with valid If-Match: "1" -> version 2; stale If-Match: "1" -> 412
        patched_m = await client.patch(
            f"/v1/materials/{mat1_id}",
            headers=_auth(token, idem="patch-m-1", if_match='"1"'),
            json={"title": "初见手札·修订"},
        )
        assert patched_m.status_code == 200
        assert patched_m.json()["version"] == 2

        stale_m = await client.patch(
            f"/v1/materials/{mat1_id}",
            headers=_auth(token, idem="patch-m-stale", if_match='"1"'),
            json={"title": "旧版本冲突"},
        )
        assert stale_m.status_code == 412

        # Manual confirmed fact 1 -> then contradicting manual fact 2 triggers bi-temporal supersession!
        f1 = await client.post(
            f"/v1/persons/{linxi_id}/facts",
            headers=_auth(token, idem="fact-1"),
            json={
                "type": "preference",
                "polarity": "positive",
                "content": "喜欢吃抹茶冰淇淋",
            },
        )
        assert f1.status_code == 201
        fact1_id = f1.json()["id"]
        assert f1.json()["recordId"] is not None

        f2 = await client.post(
            f"/v1/persons/{linxi_id}/facts",
            headers=_auth(token, idem="fact-2"),
            json={
                "type": "taboo",
                "polarity": "negative",
                "content": "对抹茶过敏，不能吃抹茶",
            },
        )
        assert f2.status_code == 201
        fact2_id = f2.json()["id"]

        # Active facts list excludes invalidated f1 by default
        active_facts = (
            await client.get(f"/v1/persons/{linxi_id}/facts", headers=_auth(token))
        ).json()["items"]
        assert [f["id"] for f in active_facts] == [fact2_id]

        # With includeInvalidated=true, f1 is preserved in history with invalidAt, timeStatus='past', supersededById=fact2_id
        history_facts = (
            await client.get(
                f"/v1/persons/{linxi_id}/facts?includeInvalidated=true",
                headers=_auth(token),
            )
        ).json()["items"]
        by_fid = {f["id"]: f for f in history_facts}
        assert by_fid[fact1_id]["timeStatus"] == "past"
        assert by_fid[fact1_id]["invalidAt"] is not None
        assert by_fid[fact1_id]["supersededById"] == fact2_id

        # Fact PATCH If-Match optimistic lock + DELETE (with Idempotency-Key & 204 replay)
        assert (
            await client.delete(f"/v1/facts/{fact2_id}", headers=_auth(token))
        ).status_code == 400

        stale_f = await client.patch(
            f"/v1/facts/{fact2_id}",
            headers=_auth(token, idem="patch-f-stale", if_match='"99"'),
            json={"content": "版本不匹配"},
        )
        assert stale_f.status_code == 412

        del_f = await client.delete(f"/v1/facts/{fact2_id}", headers=_auth(token, idem="del-f-1"))
        assert del_f.status_code == 204
        del_f_replay = await client.delete(
            f"/v1/facts/{fact2_id}", headers=_auth(token, idem="del-f-1")
        )
        assert del_f_replay.status_code == 204

        del_m = await client.delete(
            f"/v1/materials/{mat1_id}", headers=_auth(token, idem="del-m-1")
        )
        assert del_m.status_code == 204
        del_m_replay = await client.delete(
            f"/v1/materials/{mat1_id}", headers=_auth(token, idem="del-m-1")
        )
        assert del_m_replay.status_code == 204
        get_del_m = await client.get(f"/v1/materials/{mat1_id}", headers=_auth(token))
        assert get_del_m.status_code == 404


# ============================================================================
# 7. All 6 Inbox Resolution Actions + Gold Venting Hold + 72h Auto-Demote
# ============================================================================


@pytest.mark.asyncio
async def test_inbox_all_six_resolution_actions_and_72h_venting_expiry(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)
    chen1_id = await _seed_person(
        db_container, scope, dek, display_name="陈明", tier=Tier.CRYSTAL, aliases=("老陈",)
    )
    chen2_id = await _seed_person(
        db_container, scope, dek, display_name="陈叔", tier=Tier.SILVER, aliases=("老陈",)
    )

    gateway = ProgrammableGateway()
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # ------------------------------------------------------------------
        # Action 1: `confirm` on lunar date fact -> activates ReminderRow!
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.DATE,
                    title="农历生日",
                    content="林溪农历八月十五生日",
                    is_lunar=True,
                    lunar_month=8,
                    lunar_day=15,
                    confidence=0.95,
                )
            ]
        )
        r_date = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-date"),
            json={
                "source": "text",
                "text": "林溪农历八月十五生日",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        date_inbox_id = r_date.json()["inboxItemIds"][0]
        res_confirm = await client.post(
            f"/v1/inbox/{date_inbox_id}:resolve",
            headers=_auth(token, idem="res-confirm"),
            json={"action": "confirm"},
        )
        assert res_confirm.status_code == 200
        async with db_container.db.user_session(scope) as sess:
            rem = await sess.scalar(select(ReminderRow).where(ReminderRow.person_id == linxi_id))
            assert rem is not None
            assert rem.status == "active"

        # ------------------------------------------------------------------
        # Action 2: `keep_as_note` -> RecordRow.review_state='note_only', FactRow='rejected'
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.PREFERENCE,
                    title="模糊偏好",
                    content="好像偶尔会看悬疑片",
                    confidence=0.55,
                )
            ]
        )
        r_low = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-low"),
            json={
                "source": "text",
                "text": "她好像偶尔会看悬疑片",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        low_inbox_id = r_low.json()["inboxItemIds"][0]
        low_mat_id = r_low.json()["splits"][0]["materialId"]
        res_note = await client.post(
            f"/v1/inbox/{low_inbox_id}:resolve",
            headers=_auth(token, idem="res-keep-note"),
            json={"action": "keep_as_note"},
        )
        assert res_note.status_code == 200
        mat_after = (await client.get(f"/v1/materials/{low_mat_id}", headers=_auth(token))).json()
        assert mat_after["reviewState"] == "note_only"

        # ------------------------------------------------------------------
        # Action 3: `discard` ("气消了删掉") on Gold `venting_hold`
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            is_venting_or_irony=True,
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.EVENT,
                    polarity=Polarity.NEGATIVE,
                    title="吵架气话",
                    content="气死我了以后再也不理她了",
                    confidence=0.92,
                )
            ],
        )
        r_vent = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-vent-1"),
            json={
                "source": "text",
                "text": "气死我了以后再也不理她了",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        vent_inbox_id = r_vent.json()["inboxItemIds"][0]
        vent_mat_id = r_vent.json()["splits"][0]["materialId"]

        # Held venting record MUST NOT appear in default person materials timeline!
        normal_mats = (
            await client.get(f"/v1/persons/{linxi_id}/materials", headers=_auth(token))
        ).json()["items"]
        assert all(m["id"] != vent_mat_id for m in normal_mats)

        # Visible when includeHeld=true
        held_mats = (
            await client.get(
                f"/v1/persons/{linxi_id}/materials?includeHeld=true", headers=_auth(token)
            )
        ).json()["items"]
        assert any(m["id"] == vent_mat_id for m in held_mats)

        res_discard = await client.post(
            f"/v1/inbox/{vent_inbox_id}:resolve",
            headers=_auth(token, idem="res-discard"),
            json={"action": "discard"},
        )
        assert res_discard.status_code == 200
        assert (
            await client.get(f"/v1/materials/{vent_mat_id}", headers=_auth(token))
        ).status_code == 404

        # ------------------------------------------------------------------
        # Action 4: `assign_person` on ambiguous "老陈"
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[
                ExtractedPersonCandidate(
                    mention="老陈",
                    candidate_person_ids=[chen1_id, chen2_id],
                    confidence=0.70,
                )
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="老陈",
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="饮茶偏好",
                    content="只喝陈年普洱",
                    confidence=0.93,
                )
            ],
        )
        r_amb = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-amb"),
            json={"source": "text", "text": "老陈只喝陈年普洱", "routeMode": "global"},
        )
        amb_inbox_id = r_amb.json()["inboxItemIds"][0]
        inbox_list = (await client.get("/v1/inbox", headers=_auth(token))).json()["items"]
        amb_dto = next(x for x in inbox_list if x["id"] == amb_inbox_id)
        assert {c["personId"] for c in amb_dto["candidatePersons"]} == {
            str(chen1_id),
            str(chen2_id),
        }

        res_assign = await client.post(
            f"/v1/inbox/{amb_inbox_id}:resolve",
            headers=_auth(token, idem="res-assign"),
            json={"action": "assign_person", "targetPersonId": str(chen1_id)},
        )
        assert res_assign.status_code == 200
        chen1_facts = (
            await client.get(f"/v1/persons/{chen1_id}/facts", headers=_auth(token))
        ).json()["items"]
        assert [f["content"] for f in chen1_facts] == ["只喝陈年普洱"]
        assert chen1_facts[0]["status"] == "confirmed"
        assert chen1_facts[0]["recordId"] is not None

        # ------------------------------------------------------------------
        # Action 5: `create_person` promoting provisional person to active
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[
                ExtractedPersonCandidate(mention="苏医生", is_new_person=True, confidence=0.92)
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="苏医生",
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="饮品习惯",
                    content="喜欢无糖乌龙茶",
                    confidence=0.93,
                )
            ],
        )
        r_newp = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-newp"),
            json={"source": "text", "text": "苏医生喜欢无糖乌龙茶", "routeMode": "global"},
        )
        newp_inbox_id = r_newp.json()["inboxItemIds"][0]
        su_id = UUID(r_newp.json()["splits"][0]["personId"])

        res_create_p = await client.post(
            f"/v1/inbox/{newp_inbox_id}:resolve",
            headers=_auth(token, idem="res-createp"),
            json={"action": "create_person", "tier": "silver"},
        )
        assert res_create_p.status_code == 200
        async with db_container.db.user_session(scope) as sess:
            su_row = await sess.get(PersonRow, su_id)
            assert su_row is not None
            assert su_row.state == "active"
            assert su_row.tier == "silver"

        # ------------------------------------------------------------------
        # Action 6: `merge_person` merging provisional nickname into existing 林溪
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[
                ExtractedPersonCandidate(mention="小溪宝", is_new_person=True, confidence=0.90)
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="小溪宝",
                    fact_type=FactType.WISH,
                    polarity=Polarity.POSITIVE,
                    title="旅行心愿",
                    content="冬天想去看极光",
                    confidence=0.94,
                )
            ],
        )
        r_merge = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-merge"),
            json={"source": "text", "text": "小溪宝冬天想去看极光", "routeMode": "global"},
        )
        merge_inbox_id = r_merge.json()["inboxItemIds"][0]
        prov_dup_id = UUID(r_merge.json()["splits"][0]["personId"])

        res_merge = await client.post(
            f"/v1/inbox/{merge_inbox_id}:resolve",
            headers=_auth(token, idem="res-merge"),
            json={"action": "merge_person", "targetPersonId": str(linxi_id)},
        )
        assert res_merge.status_code == 200
        linxi_wishes = (
            await client.get(
                f"/v1/persons/{linxi_id}/facts?type=wish&status=confirmed",
                headers=_auth(token),
            )
        ).json()["items"]
        assert [w["content"] for w in linxi_wishes] == ["冬天想去看极光"]
        async with db_container.db.user_session(scope) as sess:
            dup_row = await sess.get(PersonRow, prov_dup_id)
            assert dup_row is not None
            assert dup_row.deleted_at is not None

        # ------------------------------------------------------------------
        # 72-Hour Worker Helper: `expire_stale_venting_items`
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            is_venting_or_irony=True,
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.EVENT,
                    polarity=Polarity.NEGATIVE,
                    title="深夜抱怨",
                    content="今天真的有点委屈",
                    confidence=0.91,
                )
            ],
        )
        r_vent2 = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="inbox-vent-expire"),
            json={
                "source": "text",
                "text": "今天真的有点委屈",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        vent2_inbox_id = UUID(r_vent2.json()["inboxItemIds"][0])
        vent2_mat_id = UUID(r_vent2.json()["splits"][0]["materialId"])

        manual_clock.advance(hours=73)
        async with db_container.db.user_session(scope) as sess:
            demoted = await expire_stale_venting_items(sess, scope, now=manual_clock.now())
            assert demoted == 1
            item_row = await sess.get(InboxItemRow, vent2_inbox_id)
            rec_row = await sess.get(RecordRow, vent2_mat_id)
            assert item_row is not None and item_row.status == "expired"
            assert rec_row is not None and rec_row.review_state == "note_only"


# ============================================================================
# 8. Cross-Tenant Row-Level Security Isolation Across Capture & Memory APIs
# ============================================================================


@pytest.mark.asyncio
async def test_capture_and_memory_endpoints_enforce_strict_tenant_isolation(
    db_container: AppContainer,
) -> None:
    scope_a, token_a, dek_a = await _provision_user(db_container)
    _scope_b, token_b, _dek_b = await _provision_user(db_container)
    person_a = await _seed_person(db_container, scope_a, dek_a, display_name="林溪", tier=Tier.GOLD)

    gateway = ProgrammableGateway(
        extract_handler=lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=person_a,
                    fact_type=FactType.DATE,
                    title="纪念日",
                    content="纪念日 12月01日",
                    trigger_date="2026-12-01",
                    confidence=0.95,
                )
            ]
        )
    )
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cap_a = (
            await client.post(
                "/v1/captures?sync=true",
                headers=_auth(token_a, idem="rls-cap-a"),
                json={
                    "source": "text",
                    "text": "林溪纪念日 12月01日",
                    "routeMode": "current",
                    "activePersonId": str(person_a),
                },
            )
        ).json()
        cap_id = cap_a["captureId"]
        mat_id = cap_a["splits"][0]["materialId"]
        inbox_id = cap_a["inboxItemIds"][0]

        # User B gets 404 / empty across every single resource owned by User A
        assert (
            await client.get(f"/v1/captures/{cap_id}", headers=_auth(token_b))
        ).status_code == 404
        assert (
            await client.post(f"/v1/captures/{cap_id}:undo", headers=_auth(token_b, idem="b-undo"))
        ).status_code == 404
        assert (
            await client.get(f"/v1/materials/{mat_id}", headers=_auth(token_b))
        ).status_code == 404
        assert (
            await client.get(f"/v1/persons/{person_a}/materials", headers=_auth(token_b))
        ).status_code == 404
        assert (
            await client.post(
                f"/v1/inbox/{inbox_id}:resolve",
                headers=_auth(token_b, idem="b-res"),
                json={"action": "confirm"},
            )
        ).status_code == 404
        b_inbox = (await client.get("/v1/inbox", headers=_auth(token_b))).json()["items"]
        assert b_inbox == []


# ============================================================================
# 9. Red-Team Remediations: RT-02 Held Disambiguation & Target Validation,
#    RT-07 Lunar Promise `once_date` Activation, and P2-1 Signed Media URLs
# ============================================================================


@pytest.mark.asyncio
async def test_redteam_remediations_rt02_rt07_and_p2_1_signed_media(
    db_container: AppContainer,
) -> None:
    from mindweave.modules.capture.infra.repository import get_or_create_unassigned_placeholder
    from mindweave.modules.reminders.api.router import router as reminders_router

    scope, token, dek = await _provision_user(db_container)
    scope_other, _token_other, dek_other = await _provision_user(db_container)
    foreign_person_id = await _seed_person(
        db_container, scope_other, dek_other, display_name="外租户人", tier=Tier.SILVER
    )

    chen1_id = await _seed_person(
        db_container, scope, dek, display_name="陈明", tier=Tier.CRYSTAL, aliases=("老陈",)
    )
    chen2_id = await _seed_person(
        db_container, scope, dek, display_name="陈叔", tier=Tier.SILVER, aliases=("老陈",)
    )
    deleted_person_id = await _seed_person(
        db_container, scope, dek, display_name="已删联系人", tier=Tier.CRYSTAL
    )
    linxi_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)

    async with db_container.db.user_session(scope) as sess:
        del_row = await sess.get(PersonRow, deleted_person_id)
        assert del_row is not None
        del_row.deleted_at = db_container.clock.now()
        unassigned_row = await get_or_create_unassigned_placeholder(
            sess, scope=scope, dek=dek, cipher=db_container.cipher, now=db_container.clock.now()
        )
        unassigned_id = unassigned_row.id

    gateway = ProgrammableGateway(transcript_text="老陈只喝白毫银针", duration_ms=5100)
    app = _build_test_app(db_container, gateway)
    app.include_router(reminders_router, prefix="/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # ------------------------------------------------------------------
        # 1. RT-02: Manual materials/facts on `__unassigned__` or deleted person -> 404
        # ------------------------------------------------------------------
        for invalid_pid in (unassigned_id, deleted_person_id):
            assert (
                await client.post(
                    f"/v1/persons/{invalid_pid}/materials",
                    headers=_auth(token, idem=f"bad-mat-{invalid_pid}"),
                    json={"kind": "note", "title": "不该写入标题", "body": "不该写入"},
                )
            ).status_code == 404
            assert (
                await client.post(
                    f"/v1/persons/{invalid_pid}/facts",
                    headers=_auth(token, idem=f"bad-fact-{invalid_pid}"),
                    json={"type": "preference", "content": "不该写入"},
                )
            ).status_code == 404

        # ------------------------------------------------------------------
        # 2. RT-02: Ambiguous voice capture holds RecordRow (`review_state='held'`)
        #    so candidate_person_ids[0]'s active materials are NEVER contaminated!
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[
                ExtractedPersonCandidate(
                    mention="老陈",
                    candidate_person_ids=[chen1_id, chen2_id],
                    confidence=0.68,
                )
            ],
            facts=[
                ExtractedFactItem(
                    person_mention="老陈",
                    fact_type=FactType.PREFERENCE,
                    polarity=Polarity.POSITIVE,
                    title="饮茶偏好",
                    content="只喝白毫银针",
                    confidence=0.95,
                )
            ],
        )
        amb_resp = await client.post(
            "/v1/voice/ingest",
            headers=_auth(token, idem="rt02-voice-amb"),
            data={"routeMode": "global", "activePersonId": ""},
            files={"audio": ("amb.webm", VALID_WEBM_BYTES, "audio/webm")},
        )
        assert amb_resp.status_code == 200
        amb_body = amb_resp.json()
        assert amb_body["splits"] == []
        assert amb_body["pendingCount"] == 1

        # Neither chen1 nor chen2 has any active normal material before disambiguation!
        chen1_mats_before = (
            await client.get(f"/v1/persons/{chen1_id}/materials", headers=_auth(token))
        ).json()["items"]
        chen2_mats_before = (
            await client.get(f"/v1/persons/{chen2_id}/materials", headers=_auth(token))
        ).json()["items"]
        assert chen1_mats_before == []
        assert chen2_mats_before == []

        inbox_items = (await client.get("/v1/inbox", headers=_auth(token))).json()["items"]
        amb_inbox = inbox_items[0]
        amb_inbox_id = amb_inbox["id"]

        # Verify held RecordRow exists in DB and is linked to InboxItemRow
        async with db_container.db.user_session(scope) as sess:
            inbox_row = await sess.get(InboxItemRow, UUID(amb_inbox_id))
            assert inbox_row is not None and inbox_row.record_id is not None
            held_rec = await sess.get(RecordRow, inbox_row.record_id)
            assert held_rec is not None
            assert held_rec.review_state == "held"
            held_rec_id = held_rec.id

        # Supplying foreign, soft-deleted, or `__unassigned__` targetPersonId MUST fail with 404!
        for bad_target in (foreign_person_id, deleted_person_id, unassigned_id):
            bad_res = await client.post(
                f"/v1/inbox/{amb_inbox_id}:resolve",
                headers=_auth(token, idem=f"rt02-bad-{bad_target}"),
                json={"action": "assign_person", "targetPersonId": str(bad_target)},
            )
            assert bad_res.status_code == 404

        # Resolving to chen2_id promotes the held RecordRow to `normal` on chen2_id!
        ok_res = await client.post(
            f"/v1/inbox/{amb_inbox_id}:resolve",
            headers=_auth(token, idem="rt02-good-chen2"),
            json={"action": "assign_person", "targetPersonId": str(chen2_id)},
        )
        assert ok_res.status_code == 200

        chen1_mats_after = (
            await client.get(f"/v1/persons/{chen1_id}/materials", headers=_auth(token))
        ).json()["items"]
        chen2_mats_after = (
            await client.get(f"/v1/persons/{chen2_id}/materials", headers=_auth(token))
        ).json()["items"]
        assert chen1_mats_after == []
        assert [m["id"] for m in chen2_mats_after] == [str(held_rec_id)]
        assert chen2_mats_after[0]["reviewState"] == "normal"

        # ------------------------------------------------------------------
        # 3. P2-1: Material `mediaUrl` is HMAC-signed & streamable without Bearer header
        # ------------------------------------------------------------------
        signed_media_url = chen2_mats_after[0]["mediaUrl"]
        assert signed_media_url is not None
        assert "uid=" in signed_media_url
        assert "exp=" in signed_media_url
        assert "sig=" in signed_media_url

        # Browser `<audio src="...">` GET without Authorization header succeeds via signed query
        browser_media_resp = await client.get(signed_media_url)
        assert browser_media_resp.status_code == 200
        assert browser_media_resp.content == VALID_WEBM_BYTES

        # Unsigned media path without Authorization header MUST fail with 401
        unsigned_resp = await client.get(f"/v1/materials/{held_rec_id}/media")
        assert unsigned_resp.status_code == 401

        # ------------------------------------------------------------------
        # 4. RT-07: Lunar `promise` capture populates Gregorian `once_date` so
        #    activating the proposed reminder via `PATCH /v1/reminders/{id}` returns 200!
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            facts=[
                ExtractedFactItem(
                    person_mention="林溪",
                    person_id=linxi_id,
                    fact_type=FactType.PROMISE,
                    polarity=Polarity.POSITIVE,
                    title="中秋海边看月亮",
                    content="答应林溪农历八月十五一起去海边看月亮",
                    is_lunar=True,
                    lunar_month=8,
                    lunar_day=15,
                    trigger_date=None,
                    confidence=0.96,
                )
            ]
        )
        lunar_promise_cap = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="rt07-lunar-promise"),
            json={
                "source": "text",
                "text": "答应林溪农历八月十五一起去海边看月亮",
                "routeMode": "current",
                "activePersonId": str(linxi_id),
            },
        )
        assert lunar_promise_cap.status_code == 201

        async with db_container.db.user_session(scope) as sess:
            rem_row = await sess.scalar(
                select(ReminderRow).where(ReminderRow.person_id == linxi_id)
            )
            assert rem_row is not None
            assert rem_row.rule == "once"
            assert rem_row.status == "proposed"
            assert rem_row.once_date is not None
            assert rem_row.next_fire_at is not None
            rem_id = rem_row.id

        act_resp = await client.patch(
            f"/v1/reminders/{rem_id}",
            headers=_auth(token, idem="rt07-activate-rem", if_match='"1"'),
            json={"status": "active"},
        )
        assert act_resp.status_code == 200, act_resp.text
        assert act_resp.json()["status"] == "active"


# ============================================================================
# 10. Round 2 Remediations: R2-03 Deleted/Archived Fact & Reminder Guards,
#     R2-06 Lunar & Archived Capture Validation, R2-07 Unassigned Masking +
#     Idempotency Media Re-signing, and R2-08 Autoflush Provisional Cleanup
# ============================================================================


@pytest.mark.asyncio
async def test_round2_remediations_r2_03_r2_06_r2_07_and_r2_08(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    active_pid = await _seed_person(db_container, scope, dek, display_name="唐梨", tier=Tier.GOLD)
    archived_pid = await _seed_person(
        db_container, scope, dek, display_name="归档联系人", tier=Tier.SILVER
    )
    to_delete_pid = await _seed_person(
        db_container, scope, dek, display_name="即将删除", tier=Tier.CRYSTAL
    )

    async with db_container.db.user_session(scope) as sess:
        arch_row = await sess.get(PersonRow, archived_pid)
        assert arch_row is not None
        arch_row.state = PersonState.ARCHIVED.value
        arch_row.archived_at = manual_clock.now()

    gateway = ProgrammableGateway()
    app = _build_test_app(db_container, gateway)
    jpeg_bytes = _make_jpeg_with_exif()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # ------------------------------------------------------------------
        # 1. R2-06: Incomplete lunar fact (POST & PATCH) + archived scoped capture -> 422
        # ------------------------------------------------------------------
        assert (
            await client.post(
                f"/v1/persons/{active_pid}/facts",
                headers=_auth(token, idem="r2-bad-lunar-1"),
                json={"type": "date", "content": "唐梨农历生日", "isLunar": True},
            )
        ).status_code == 422
        assert (
            await client.post(
                f"/v1/persons/{active_pid}/facts",
                headers=_auth(token, idem="r2-bad-lunar-2"),
                json={
                    "type": "date",
                    "content": "非农历却标闰月",
                    "isLunar": False,
                    "lunarIsLeapMonth": True,
                },
            )
        ).status_code == 422

        valid_fact_res = await client.post(
            f"/v1/persons/{active_pid}/facts",
            headers=_auth(token, idem="r2-base-fact"),
            json={"type": "preference", "polarity": "positive", "content": "喜欢乌龙茶"},
        )
        assert valid_fact_res.status_code == 201
        valid_fid = valid_fact_res.json()["id"]

        assert (
            await client.patch(
                f"/v1/facts/{valid_fid}",
                headers=_auth(token, idem="r2-patch-bad-lunar-1", if_match='"1"'),
                json={"type": "date", "isLunar": True},
            )
        ).status_code == 422
        assert (
            await client.patch(
                f"/v1/facts/{valid_fid}",
                headers=_auth(token, idem="r2-patch-bad-lunar-2", if_match='"1"'),
                json={"lunarIsLeapMonth": True},
            )
        ).status_code == 422

        arch_cap_res = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="r2-arch-cap"),
            json={
                "source": "text",
                "routeMode": "current",
                "activePersonId": str(archived_pid),
                "text": "她喜欢草莓蛋糕",
            },
        )
        assert arch_cap_res.status_code == 422

        # ------------------------------------------------------------------
        # 2. R2-03: Archived person fact keeps reminder PAUSED; soft-deleted
        #    person blocks PATCH/DELETE on facts & materials with 404
        # ------------------------------------------------------------------
        arch_date_fact = await client.post(
            f"/v1/persons/{archived_pid}/facts",
            headers=_auth(token, idem="r2-arch-fact"),
            json={
                "type": "date",
                "content": "归档纪念日 12月20日",
                "triggerDate": "2027-12-20",
            },
        )
        assert arch_date_fact.status_code == 201
        async with db_container.db.user_session(scope) as sess:
            arch_rem = await sess.scalar(
                select(ReminderRow).where(ReminderRow.person_id == archived_pid)
            )
            assert arch_rem is not None
            assert arch_rem.status == "paused"
            assert arch_rem.next_fire_at is None

        del_fact_res = await client.post(
            f"/v1/persons/{to_delete_pid}/facts",
            headers=_auth(token, idem="r2-del-p-fact"),
            json={"type": "preference", "content": "喜欢桂花糕"},
        )
        assert del_fact_res.status_code == 201
        del_fid = del_fact_res.json()["id"]
        del_mid = del_fact_res.json()["recordId"]

        async with db_container.db.user_session(scope) as sess:
            del_p_row = await sess.get(PersonRow, to_delete_pid)
            assert del_p_row is not None
            del_p_row.deleted_at = manual_clock.now()

        assert (
            await client.patch(
                f"/v1/facts/{del_fid}",
                headers=_auth(token, idem="r2-patch-del-f", if_match='"1"'),
                json={"content": "不该成功"},
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/v1/facts/{del_fid}",
                headers=_auth(token, idem="r2-delete-del-f"),
            )
        ).status_code == 404
        assert (
            await client.get(f"/v1/materials/{del_mid}", headers=_auth(token))
        ).status_code == 404
        assert (
            await client.patch(
                f"/v1/materials/{del_mid}",
                headers=_auth(token, idem="r2-patch-del-m", if_match='"1"'),
                json={"title": "不该成功"},
            )
        ).status_code == 404

        # ------------------------------------------------------------------
        # 3. R2-07: Unassigned material masks `personId: None`, and IdempotencyHit
        #    replay after 16m (> 15m media TTL) freshly re-signs `mediaUrl`!
        # ------------------------------------------------------------------
        def _boom(_s: UserScope, _i: ExtractRecordInput) -> ExtractRecordOutput:
            raise TimeoutError("simulated upstream timeout")

        gateway.extract_handler = _boom
        unassigned_cap = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="r2-unassigned-cap"),
            json={"source": "text", "routeMode": "global", "text": "买一束向日葵"},
        )
        assert unassigned_cap.status_code == 201
        open_inbox = (await client.get("/v1/inbox?status=open", headers=_auth(token))).json()[
            "items"
        ]
        unassigned_rec_id = open_inbox[0]["recordId"]
        mat_unassigned = await client.get(
            f"/v1/materials/{unassigned_rec_id}", headers=_auth(token)
        )
        assert mat_unassigned.status_code == 200
        assert mat_unassigned.json()["personId"] is None

        photo_mat = await client.post(
            f"/v1/persons/{active_pid}/materials",
            headers=_auth(token, idem="r2-photo-idem"),
            data={"title": "海边夕阳", "body": "胶片质感很好"},
            files={"photo": ("sunset.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert photo_mat.status_code == 201
        photo_mid = photo_mat.json()["id"]

        patch_photo_1 = await client.patch(
            f"/v1/materials/{photo_mid}",
            headers=_auth(token, idem="r2-photo-patch-idem", if_match='"1"'),
            json={"title": "海边夕阳·精修"},
        )
        assert patch_photo_1.status_code == 200

        manual_clock.advance(seconds=960)  # 16 minutes > 15m media & access token TTL
        fresh_token, _ = db_container.tokens.create_access_token(
            scope.user_id, now=manual_clock.now()
        )
        photo_mat_replay = await client.post(
            f"/v1/persons/{active_pid}/materials",
            headers=_auth(fresh_token, idem="r2-photo-idem"),
            data={"title": "海边夕阳", "body": "胶片质感很好"},
            files={"photo": ("sunset.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert photo_mat_replay.status_code == 201
        assert (await client.get(photo_mat_replay.json()["mediaUrl"])).status_code == 200

        patch_photo_replay = await client.patch(
            f"/v1/materials/{photo_mid}",
            headers=_auth(fresh_token, idem="r2-photo-patch-idem", if_match='"1"'),
            json={"title": "海边夕阳·精修"},
        )
        assert patch_photo_replay.status_code == 200
        assert (await client.get(patch_photo_replay.json()["mediaUrl"])).status_code == 200

        # ------------------------------------------------------------------
        # 4. R2-08: `:undo` and `inbox:resolve(discard)` flush before counting
        #    active records/facts so provisional persons are soft-deleted!
        # ------------------------------------------------------------------
        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[ExtractedPersonCandidate(mention="周予安", is_new_person=True)],
            facts=[
                ExtractedFactItem(
                    person_mention="周予安",
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="水果过敏",
                    content="对猕猴桃过敏",
                    confidence=0.95,
                )
            ],
        )
        cap_zhou = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(fresh_token, idem="r2-08-zhou"),
            json={"source": "text", "routeMode": "global", "text": "周予安对猕猴桃过敏"},
        )
        assert cap_zhou.status_code == 201
        zhou_pid = UUID(cap_zhou.json()["splits"][0]["personId"])
        undo_zhou = await client.post(
            f"/v1/captures/{cap_zhou.json()['captureId']}:undo",
            headers=_auth(fresh_token, idem="r2-08-zhou-undo"),
        )
        assert undo_zhou.status_code == 200

        gateway.extract_handler = lambda _s, _i: ExtractRecordOutput(
            persons=[ExtractedPersonCandidate(mention="林知夏", is_new_person=True)],
            facts=[
                ExtractedFactItem(
                    person_mention="林知夏",
                    fact_type=FactType.TABOO,
                    polarity=Polarity.NEGATIVE,
                    title="香菜忌口",
                    content="不吃香菜",
                    confidence=0.94,
                )
            ],
        )
        cap_lin = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(fresh_token, idem="r2-08-lin"),
            json={"source": "text", "routeMode": "global", "text": "林知夏不吃香菜"},
        )
        assert cap_lin.status_code == 201
        lin_pid = UUID(cap_lin.json()["splits"][0]["personId"])
        lin_inbox_id = cap_lin.json()["inboxItemIds"][0]
        disc_lin = await client.post(
            f"/v1/inbox/{lin_inbox_id}:resolve",
            headers=_auth(fresh_token, idem="r2-08-lin-discard"),
            json={"action": "discard"},
        )
        assert disc_lin.status_code == 200

        async with db_container.db.user_session(scope) as sess:
            zhou_row = await sess.get(PersonRow, zhou_pid)
            lin_row = await sess.get(PersonRow, lin_pid)
            assert zhou_row is not None and zhou_row.deleted_at is not None
            assert zhou_row.state == PersonState.ARCHIVED.value
            assert lin_row is not None and lin_row.deleted_at is not None
            assert lin_row.state == PersonState.ARCHIVED.value


# ============================================================================
# 11. Round 3 Remediations: R3-05 Lunar->Solar Patch Clears Lunar Fields &
#     R3-03 Multipart Form Null-Byte Defense
# ============================================================================


@pytest.mark.asyncio
async def test_round3_remediations_r3_05_lunar_toggle_and_r3_03_multipart_null_bytes(
    db_container: AppContainer,
) -> None:
    scope, token, dek = await _provision_user(db_container)
    person_id = await _seed_person(db_container, scope, dek, display_name="林溪", tier=Tier.GOLD)

    img = Image.new("RGB", (24, 24), color=(180, 120, 90))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    gateway = ProgrammableGateway(transcript_text="正常语音", duration_ms=1500)
    app = _build_test_app(db_container, gateway)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. R3-05: Creating solar fact with lunarMonth/lunarDay -> 422
        bad_create = await client.post(
            f"/v1/persons/{person_id}/facts",
            headers=_auth(token, idem="r3-bad-solar-create"),
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "冲突生日",
                "isLunar": False,
                "lunarMonth": 8,
                "lunarDay": 15,
            },
        )
        assert bad_create.status_code == 422

        # 2. R3-05: Lunar fact toggled to solar clears lunarMonth, lunarDay, lunarIsLeapMonth
        lunar_resp = await client.post(
            f"/v1/persons/{person_id}/facts",
            headers=_auth(token, idem="r3-lunar-create"),
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "林溪生日",
                "isLunar": True,
                "lunarMonth": 8,
                "lunarDay": 15,
                "lunarIsLeapMonth": True,
            },
        )
        assert lunar_resp.status_code == 201
        fid = lunar_resp.json()["id"]

        # Supplying lunarMonth alongside isLunar=False -> 422
        bad_patch = await client.patch(
            f"/v1/facts/{fid}",
            headers=_auth(token, idem="r3-bad-solar-patch", if_match='"1"'),
            json={"isLunar": False, "lunarMonth": 8},
        )
        assert bad_patch.status_code == 422

        solar_patch = await client.patch(
            f"/v1/facts/{fid}",
            headers=_auth(token, idem="r3-ok-solar-patch", if_match='"1"'),
            json={"isLunar": False, "triggerDate": "2026-10-01"},
        )
        assert solar_patch.status_code == 200
        patched_json = solar_patch.json()
        assert patched_json["isLunar"] is False
        assert patched_json["lunarMonth"] is None
        assert patched_json["lunarDay"] is None
        assert patched_json["lunarIsLeapMonth"] is False
        assert patched_json["triggerDate"] == "2026-10-01"

        async with db_container.db.user_session(scope) as sess:
            rem_row = await sess.scalar(
                select(ReminderRow).where(
                    ReminderRow.user_id == scope.user_id,
                    ReminderRow.fact_id == UUID(fid),
                )
            )
            assert rem_row is not None
            assert rem_row.is_lunar is False
            assert rem_row.is_leap_month is False
            assert rem_row.target_month == 10
            assert rem_row.target_day == 1

        # 3. R3-03: Multipart form null bytes in capture & memory routers -> 422 (never 500)
        cap_null = await client.post(
            "/v1/captures?sync=true",
            headers=_auth(token, idem="r3-null-cap"),
            data={"source": "text", "routeMode": "global", "text": "恶意\x00空字节"},
            files={"audio": ("clip.webm", VALID_WEBM_BYTES, "audio/webm")},
        )
        assert cap_null.status_code == 422

        voice_null_text = await client.post(
            "/v1/voice/ingest",
            headers=_auth(token, idem="r3-null-voice-text"),
            data={"routeMode": "global", "text": "语音\x00注入"},
            files={"audio": ("clip.webm", VALID_WEBM_BYTES, "audio/webm")},
        )
        assert voice_null_text.status_code == 422

        voice_null_urlencoded = await client.post(
            "/v1/voice/ingest",
            headers={
                **_auth(token, idem="r3-null-voice-form"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content=b"routeMode=global&text=urlencoded%00nullbyte",
        )
        assert voice_null_urlencoded.status_code == 422

        mat_null = await client.post(
            f"/v1/persons/{person_id}/materials",
            headers=_auth(token, idem="r3-null-mat"),
            data={"title": "标题\x00空字节", "body": "正常正文", "stampCode": "st\x001"},
            files={"photo": ("sunset.jpg", jpeg_bytes, "image/jpeg")},
        )
        assert mat_null.status_code == 422
