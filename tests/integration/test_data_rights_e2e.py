"""End-to-end PostgreSQL integration tests for Data Sovereignty (GB 45438 export + Crypto-Shredding) & Telemetry."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from mindweave.core.clock import ManualClock
from mindweave.core.crypto import KeyShreddedError, normalize_alias_for_index
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    RLS_PROTECTED_TABLES,
    AiCallRow,
    AuditLogRow,
    AuthIdentityRow,
    BackgroundJobRow,
    CaptureRow,
    ChatCitationRow,
    ChatMessageRow,
    ChatThreadRow,
    ConsentRow,
    DeviceRow,
    ExportJobRow,
    FactRow,
    IdempotencyKeyRow,
    InboxItemRow,
    PersonAliasRow,
    PersonRow,
    RecordRow,
    RefreshTokenRow,
    ReminderDeliveryRow,
    ReminderRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.core.security import hash_contact_lookup, hash_opaque_token
from mindweave.modules.data_rights.api.router import router as data_rights_router
from mindweave.modules.data_rights.application.service import (
    DataRightsService,
    build_export_archive,
)
from mindweave.modules.data_rights.domain import JOURNAL_AIGC_DISCLOSURE_FOOTER
from mindweave.modules.telemetry.api.router import router as telemetry_router
from mindweave.shared.data_rights_registry import data_rights_registry
from mindweave.shared.enums import UNASSIGNED_PERSON_SLUG
from mindweave.shared.scopes import UserScope


@pytest.fixture(autouse=True)
def _anchor_manual_clock_in_past(manual_clock: ManualClock) -> None:
    """Keep deterministic clock safely behind wall-clock so PyJWT `iat` checks pass."""
    manual_clock.set(datetime(2026, 11, 1, 4, 0, 0, tzinfo=UTC))


def _build_test_app(container: AppContainer) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(data_rights_router, prefix=container.settings.api_prefix)
    app.include_router(telemetry_router, prefix=container.settings.api_prefix)
    return app


async def _seed_rich_user(
    container: AppContainer,
    *,
    email: str,
    person_gold_name: str,
    person_silver_name: str,
    person_crystal_name: str,
    voice_bytes: bytes,
) -> tuple[UUID, str, str, str]:
    """Provision a full user across all exportable tables and return `(user_id, access_token, reauth_token, media_key)`."""
    now = container.clock.now()
    uid = new_id()
    scope = UserScope(uid)
    wrapped_dek, kek_id = container.cipher.generate_wrapped_dek(uid)
    dek = container.cipher.load_dek(user_id=uid, wrapped_dek=wrapped_dek, kek_id=kek_id)

    identity_id = new_id()
    refresh_id = new_id()
    async with container.db.system_session() as sys_sess:
        sys_sess.add(UserRow(id=uid, status="active", created_at=now, updated_at=now))
        await sys_sess.flush()
        sys_sess.add(
            AuthIdentityRow(
                id=identity_id,
                user_id=uid,
                provider="email",
                subject_hmac=hash_contact_lookup(email, secret=container.settings.jwt_secret),
                contact_enc=container.cipher.encrypt_str(
                    dek,
                    email,
                    user_id=uid,
                    table="auth_identities",
                    column="contact_enc",
                    row_id=identity_id,
                ),
                created_at=now,
                last_login_at=now,
            )
        )
        sys_sess.add(
            RefreshTokenRow(
                id=refresh_id,
                user_id=uid,
                family_id=new_id(),
                token_hash=hash_opaque_token(f"rt-{uid}"),
                expires_at=now + timedelta(days=30),
                created_at=now,
            )
        )
        sys_sess.add(
            BackgroundJobRow(
                id=new_id(),
                user_id=uid,
                task_name="companion.summarize_thread",
                payload_json={"thread_id": str(new_id())},
                status="queued",
                run_at=now + timedelta(minutes=10),
                created_at=now,
                updated_at=now,
            )
        )

    media_key = f"{uid}/voice/ocean_promise.webm"
    await container.storage.put_encrypted(
        user_id=uid,
        dek=dek,
        object_key=media_key,
        plaintext=voice_bytes,
        content_type="audio/webm",
    )

    p_gold = new_id()
    p_silver = new_id()
    p_crystal = new_id()
    alias_id = new_id()
    device_id = new_id()
    capture_id = new_id()
    record_id = new_id()
    fact_id = new_id()
    inbox_id = new_id()
    reminder_id = new_id()
    delivery_id = new_id()
    thread_id = new_id()
    msg_id = new_id()
    citation_id = new_id()

    async with container.db.user_session(scope) as sess:
        sess.add(
            UserKeyRow(
                user_id=uid,
                wrapped_dek=wrapped_dek,
                kek_id=kek_id,
                created_at=now,
            )
        )
        sess.add(
            UserPreferenceRow(
                user_id=uid,
                preferred_edition="cotton",
                cupid_layout="a",
                wallpaper_preset="warm_paper",
                app_lock_enabled=True,
                late_night_care_enabled=True,
                timezone="Asia/Shanghai",
                version=1,
                updated_at=now,
            )
        )
        sess.add(
            ConsentRow(
                id=new_id(),
                user_id=uid,
                consent_kind="privacy_policy",
                policy_version="v0.2",
                granted=True,
                granted_at=now,
            )
        )
        sess.add(
            DeviceRow(
                id=device_id,
                user_id=uid,
                platform="ios_apns",
                push_token_enc=container.cipher.encrypt_str(
                    dek,
                    "SENSITIVE_RAW_APNS_DEVICE_PUSH_TOKEN_DO_NOT_EXPORT",
                    user_id=uid,
                    table="devices",
                    column="push_token_enc",
                    row_id=device_id,
                ),
                token_hmac=hash_opaque_token(f"push-{uid}"),
                device_label_enc=container.cipher.encrypt_str(
                    dek,
                    "文杰的 iPhone 16 Pro",
                    user_id=uid,
                    table="devices",
                    column="device_label_enc",
                    row_id=device_id,
                ),
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        sess.add_all(
            [
                PersonRow(
                    id=p_gold,
                    user_id=uid,
                    tier="gold",
                    rank="i",
                    state="active",
                    display_name_enc=container.cipher.encrypt_str(
                        dek,
                        person_gold_name,
                        user_id=uid,
                        table="persons",
                        column="display_name_enc",
                        row_id=p_gold,
                    ),
                    monogram=person_gold_name[0],
                    role_tag_enc=container.cipher.encrypt_str(
                        dek,
                        "心动伴侣",
                        user_id=uid,
                        table="persons",
                        column="role_tag_enc",
                        row_id=p_gold,
                    ),
                    short_memo_enc=container.cipher.encrypt_str(
                        dek,
                        "海边看海鸥与燕麦拿铁",
                        user_id=uid,
                        table="persons",
                        column="short_memo_enc",
                        row_id=p_gold,
                    ),
                    traits_enc=container.cipher.encrypt_json(
                        dek,
                        ["忌芒果", "爱桂花乌龙"],
                        user_id=uid,
                        table="persons",
                        column="traits_enc",
                        row_id=p_gold,
                    ),
                    visual_tokens={"sealBg": "#F5E6D3"},
                    created_at=now,
                    updated_at=now,
                ),
                PersonRow(
                    id=p_silver,
                    user_id=uid,
                    tier="silver",
                    rank="m",
                    state="active",
                    display_name_enc=container.cipher.encrypt_str(
                        dek,
                        person_silver_name,
                        user_id=uid,
                        table="persons",
                        column="display_name_enc",
                        row_id=p_silver,
                    ),
                    monogram=person_silver_name[0],
                    created_at=now,
                    updated_at=now,
                ),
                PersonRow(
                    id=p_crystal,
                    user_id=uid,
                    tier="crystal",
                    rank="q",
                    state="active",
                    display_name_enc=container.cipher.encrypt_str(
                        dek,
                        person_crystal_name,
                        user_id=uid,
                        table="persons",
                        column="display_name_enc",
                        row_id=p_crystal,
                    ),
                    monogram=person_crystal_name[0],
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await sess.flush()

        sess.add(
            PersonAliasRow(
                id=alias_id,
                user_id=uid,
                person_id=p_gold,
                alias_hmac=container.cipher.compute_blind_index(
                    dek,
                    user_id=uid,
                    domain="alias",
                    normalized_value=normalize_alias_for_index("清秋"),
                ),
                alias_enc=container.cipher.encrypt_str(
                    dek,
                    "清秋",
                    user_id=uid,
                    table="person_aliases",
                    column="alias_enc",
                    row_id=alias_id,
                ),
                kind="nickname",
                is_primary=True,
                created_at=now,
            )
        )
        sess.add(
            CaptureRow(
                id=capture_id,
                user_id=uid,
                source="voice",
                route_mode="current",
                scoped_person_id=p_gold,
                status="completed",
                audio_object_key=media_key,
                keep_original_media=True,
                duration_ms=4200,
                transcript_enc=container.cipher.encrypt_str(
                    dek,
                    f"{person_gold_name}对芒果过敏，周五下午去镰仓看海鸥",
                    user_id=uid,
                    table="captures",
                    column="transcript_enc",
                    row_id=capture_id,
                ),
                idempotency_key=f"cap-{capture_id}",
                created_at=now,
                completed_at=now,
            )
        )
        await sess.flush()

        sess.add(
            RecordRow(
                id=record_id,
                user_id=uid,
                person_id=p_gold,
                capture_id=capture_id,
                kind="voice",
                title_enc=container.cipher.encrypt_str(
                    dek,
                    "周五午后 · 海边约定",
                    user_id=uid,
                    table="records",
                    column="title_enc",
                    row_id=record_id,
                ),
                body_enc=container.cipher.encrypt_str(
                    dek,
                    "备好拍立得相纸，千万避开芒果甜品",
                    user_id=uid,
                    table="records",
                    column="body_enc",
                    row_id=record_id,
                ),
                media_object_key=media_key,
                audio_duration_ms=4200,
                happened_on=date(2026, 11, 12),
                created_at=now,
                updated_at=now,
            )
        )
        await sess.flush()

        sess.add(
            FactRow(
                id=fact_id,
                user_id=uid,
                person_id=p_gold,
                record_id=record_id,
                capture_id=capture_id,
                type="taboo",
                polarity="negative",
                content_enc=container.cipher.encrypt_str(
                    dek,
                    "严重芒果过敏",
                    user_id=uid,
                    table="facts",
                    column="content_enc",
                    row_id=fact_id,
                ),
                trigger_date=date(2026, 12, 24),
                trigger_time=time(9, 0),
                status="confirmed",
                confidence=0.96,
                created_by="ai",
                valid_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await sess.flush()

        sess.add(
            InboxItemRow(
                id=inbox_id,
                user_id=uid,
                person_id=p_gold,
                capture_id=capture_id,
                record_id=record_id,
                kind="fact_confirm",
                status="resolved",
                fact_ids=[fact_id],
                candidate_person_ids=[p_gold],
                payload_enc=container.cipher.encrypt_json(
                    dek,
                    {"summary": "已确认忌口条目"},
                    user_id=uid,
                    table="inbox_items",
                    column="payload_enc",
                    row_id=inbox_id,
                ),
                resolution_action="confirm",
                resolved_at=now,
                created_at=now,
            )
        )
        sess.add(
            ReminderRow(
                id=reminder_id,
                user_id=uid,
                person_id=p_gold,
                fact_id=fact_id,
                title_enc=container.cipher.encrypt_str(
                    dek,
                    "平安夜镰仓纪念日",
                    user_id=uid,
                    table="reminders",
                    column="title_enc",
                    row_id=reminder_id,
                ),
                rule="yearly",
                target_month=12,
                target_day=24,
                lead_days=[7, 1, 0],
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        await sess.flush()

        sess.add(
            ReminderDeliveryRow(
                id=delivery_id,
                user_id=uid,
                reminder_id=reminder_id,
                scheduled_for=now,
                lead_day=7,
                channel="ios_apns",
                status="delivered",
                delivered_at=now,
            )
        )
        sess.add(
            ChatThreadRow(
                id=thread_id,
                user_id=uid,
                person_id=p_gold,
                rolling_summary_enc=container.cipher.encrypt_str(
                    dek,
                    "围绕周五海边散步准备礼物的对话",
                    user_id=uid,
                    table="chat_threads",
                    column="rolling_summary_enc",
                    row_id=thread_id,
                ),
                message_count=1,
                created_at=now,
                updated_at=now,
            )
        )
        await sess.flush()

        sess.add(
            ChatMessageRow(
                id=msg_id,
                user_id=uid,
                thread_id=thread_id,
                person_id=p_gold,
                role="assistant",
                content_enc=container.cipher.encrypt_str(
                    dek,
                    f"记得避开芒果甜品 [rec_{record_id}]，准备桂花乌龙会很贴心。",
                    user_id=uid,
                    table="chat_messages",
                    column="content_enc",
                    row_id=msg_id,
                ),
                whisper_enc=container.cipher.encrypt_str(
                    dek,
                    "相纸提前装进相机包",
                    user_id=uid,
                    table="chat_messages",
                    column="whisper_enc",
                    row_id=msg_id,
                ),
                perspectives_enc=container.cipher.encrypt_json(
                    dek,
                    ["她最近赶画稿较累，节奏慢一点更放松"],
                    user_id=uid,
                    table="chat_messages",
                    column="perspectives_enc",
                    row_id=msg_id,
                ),
                referenced_record_ids=[record_id],
                tier_snapshot="gold",
                created_at=now,
            )
        )
        await sess.flush()

        sess.add(
            ChatCitationRow(
                id=citation_id,
                user_id=uid,
                message_id=msg_id,
                record_id=record_id,
                citation_index=1,
                created_at=now,
            )
        )
        sess.add(
            AiCallRow(
                id=new_id(),
                user_id=uid,
                task="extract",
                provider="fake",
                model="fake-v1",
                prompt_id="extract_record",
                prompt_version="1.1.0",
                input_tokens=320,
                output_tokens=140,
                latency_ms=210,
                cost_micros_cny=1800,
                outcome="succeeded",
                created_at=now,
            )
        )

    access_token, _ = container.tokens.create_access_token(uid, now=now)
    reauth_token, _ = container.tokens.create_reauth_token(uid, now=now)
    return uid, access_token, reauth_token, media_key


@pytest.mark.asyncio
async def test_export_e2e_step_up_reauth_aad_decryption_gb45438_and_one_time_token(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    user_a, access_a, reauth_a, _ = await _seed_rich_user(
        db_container,
        email="alice@mindweave.local",
        person_gold_name="沈清秋",
        person_silver_name="张主管",
        person_crystal_name="陈教授",
        voice_bytes=b"WEBM-OPUS-ALICE-VOICE-MEMO-BYTES",
    )
    _, access_b, reauth_b, _ = await _seed_rich_user(
        db_container,
        email="bob@mindweave.local",
        person_gold_name="苏敏",
        person_silver_name="李经理",
        person_crystal_name="房东老顾",
        voice_bytes=b"WEBM-OPUS-BOB-SECRET-VOICE",
    )

    # Seed an internal UNASSIGNED_PERSON_SLUG placeholder + unassigned record/fact for User A (RT-01)
    async with db_container.db.user_session(UserScope(user_a)) as sess:
        dek_a = await db_container.load_user_dek(sess, UserScope(user_a))
        now_a = db_container.clock.now()
        unassigned_pid = new_id()
        unassigned_al_id = new_id()
        unassigned_rec_id = new_id()
        unassigned_fact_id = new_id()
        sess.add(
            PersonRow(
                id=unassigned_pid,
                user_id=user_a,
                slug=UNASSIGNED_PERSON_SLUG,
                tier="crystal",
                rank="zzzz",
                state="provisional",
                display_name_enc=db_container.cipher.encrypt_str(
                    dek_a,
                    "待归档",
                    user_id=user_a,
                    table="persons",
                    column="display_name_enc",
                    row_id=unassigned_pid,
                ),
                monogram="待",
                has_photo=False,
                visual_tokens={},
                version=1,
                created_at=now_a,
                updated_at=now_a,
            )
        )
        await sess.flush()
        sess.add(
            PersonAliasRow(
                id=unassigned_al_id,
                user_id=user_a,
                person_id=unassigned_pid,
                alias_hmac=db_container.cipher.compute_blind_index(
                    dek_a,
                    user_id=user_a,
                    domain="alias",
                    normalized_value=normalize_alias_for_index("待归档"),
                ),
                alias_enc=db_container.cipher.encrypt_str(
                    dek_a,
                    "待归档",
                    user_id=user_a,
                    table="person_aliases",
                    column="alias_enc",
                    row_id=unassigned_al_id,
                ),
                kind="name",
                is_primary=True,
                created_at=now_a,
            )
        )
        sess.add(
            RecordRow(
                id=unassigned_rec_id,
                user_id=user_a,
                person_id=unassigned_pid,
                capture_id=None,
                kind="note",
                title_enc=db_container.cipher.encrypt_str(
                    dek_a,
                    "深夜未归属随笔",
                    user_id=user_a,
                    table="records",
                    column="title_enc",
                    row_id=unassigned_rec_id,
                ),
                body_enc=db_container.cipher.encrypt_str(
                    dek_a,
                    "她随口说不喜欢香菜味",
                    user_id=user_a,
                    table="records",
                    column="body_enc",
                    row_id=unassigned_rec_id,
                ),
                review_state="held",
                pinned=False,
                version=1,
                created_at=now_a,
                updated_at=now_a,
            )
        )
        await sess.flush()
        sess.add(
            FactRow(
                id=unassigned_fact_id,
                user_id=user_a,
                person_id=unassigned_pid,
                record_id=unassigned_rec_id,
                capture_id=None,
                type="taboo",
                polarity="negative",
                time_status="current",
                source_speaker="narrator",
                content_enc=db_container.cipher.encrypt_str(
                    dek_a,
                    "待归属忌口：不喜欢香菜",
                    user_id=user_a,
                    table="facts",
                    column="content_enc",
                    row_id=unassigned_fact_id,
                ),
                is_lunar=False,
                lunar_is_leap_month=False,
                status="pending",
                pending_reason="ambiguous_person",
                confidence=0.6,
                created_by="ai",
                valid_at=now_a,
                version=1,
                created_at=now_a,
                updated_at=now_a,
            )
        )

    app = _build_test_app(db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Step-up re-authentication & Idempotency-Key strictly enforced
        no_reauth_resp = await client.post(
            "/v1/me/exports",
            headers={
                "Authorization": f"Bearer {access_a}",
                "Idempotency-Key": "exp-no-reauth",
            },
        )
        assert no_reauth_resp.status_code == 403
        assert no_reauth_resp.json()["code"] == "reauth_required"

        # Passing an access token or another user's reauth token into X-Reauth-Token is rejected
        wrong_reauth_resp = await client.post(
            "/v1/me/exports",
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_b,
                "Idempotency-Key": "exp-wrong-reauth",
            },
        )
        assert wrong_reauth_resp.status_code == 403
        assert wrong_reauth_resp.json()["code"] == "reauth_required"

        no_idem_resp = await client.post(
            "/v1/me/exports",
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_a,
            },
        )
        assert no_idem_resp.status_code == 400

        # 2. Create export + idempotent replay (with zero plaintext downloadToken in idempotency_keys!)
        create_resp = await client.post(
            "/v1/me/exports",
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_a,
                "Idempotency-Key": "exp-create-1",
            },
        )
        assert create_resp.status_code == 201
        export_body = create_resp.json()
        export_id = UUID(export_body["exportId"])
        initial_download_token = export_body["downloadToken"]
        assert export_body["status"] == "completed"
        assert isinstance(initial_download_token, str) and len(initial_download_token) >= 32

        # Verify idempotency_keys.response_body NEVER stores plaintext downloadToken or downloadPath (RT-04)
        async with db_container.db.user_session(UserScope(user_a)) as sess:
            idem_row = await sess.scalar(
                select(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.user_id == user_a,
                    IdempotencyKeyRow.idem_key == "exp-create-1",
                )
            )
            assert idem_row is not None
            assert idem_row.response_body is not None
            assert idem_row.response_body["downloadToken"] is None
            assert idem_row.response_body["downloadPath"] is None
            assert initial_download_token not in json.dumps(idem_row.response_body)

        replay_resp = await client.post(
            "/v1/me/exports",
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_a,
                "Idempotency-Key": "exp-create-1",
            },
        )
        assert replay_resp.status_code == 201
        assert replay_resp.json()["exportId"] == str(export_id)
        download_token = replay_resp.json()["downloadToken"]
        assert isinstance(download_token, str) and len(download_token) >= 32
        assert download_token != initial_download_token

        # Verify DB stores ONLY SHA-256 hash of active downloadToken, never plaintext, and encrypted ZIP on disk
        async with db_container.db.user_session(UserScope(user_a)) as sess:
            job_row = await sess.get(ExportJobRow, export_id)
            assert job_row is not None
            assert job_row.download_token_hash == hash_opaque_token(download_token)
            assert download_token.encode("utf-8") != job_row.download_token_hash
            assert job_row.archive_object_key is not None
            disk_file = db_container.settings.blob_storage_dir / job_row.archive_object_key
            raw_cipher_blob = disk_file.read_bytes()
            assert not raw_cipher_blob.startswith(b"PK\x03\x04")
            assert "沈清秋".encode() not in raw_cipher_blob

        # Status endpoint hides downloadToken & blocks cross-tenant access
        status_resp = await client.get(
            f"/v1/me/exports/{export_id}",
            headers={"Authorization": f"Bearer {access_a}"},
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["downloadToken"] is None

        cross_tenant_get = await client.get(
            f"/v1/me/exports/{export_id}",
            headers={"Authorization": f"Bearer {access_b}"},
        )
        assert cross_tenant_get.status_code == 404

        # Unauthenticated download WITHOUT Bearer JWT is immediately rejected with 401 (RT-04)
        unauth_dl_resp = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": download_token},
        )
        assert unauth_dl_resp.status_code == 401

        # Cross-tenant download with User B's JWT + User A's leaked token is isolated by RLS (404)
        cross_tenant_dl_resp = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": download_token},
            headers={"Authorization": f"Bearer {access_b}"},
        )
        assert cross_tenant_dl_resp.status_code == 404

        # Wrong token and superseded pre-replay token rejected with 403
        for rejected_token in ("forged-download-token", initial_download_token):
            bad_tok_resp = await client.get(
                f"/v1/me/exports/{export_id}/download",
                params={"token": rejected_token},
                headers={"Authorization": f"Bearer {access_a}"},
            )
            assert bad_tok_resp.status_code == 403

        # 3. Valid first download succeeds and returns complete decrypted ZIP
        dl_resp = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": download_token},
            headers={"Authorization": f"Bearer {access_a}"},
        )
        assert dl_resp.status_code == 200
        assert dl_resp.headers["content-type"] == "application/zip"
        zip_bytes = dl_resp.content
        assert hashlib.sha256(zip_bytes).hexdigest() == export_body["sha256Hex"]

        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            names = set(zf.namelist())
            assert "data.json" in names
            assert "journal.md" in names
            assert "media/ocean_promise.webm" in names
            assert zf.read("media/ocean_promise.webm") == b"WEBM-OPUS-ALICE-VOICE-MEMO-BYTES"

            data_doc = json.loads(zf.read("data.json").decode("utf-8"))
            assert data_doc["schemaVersion"] == "1.0"
            assert data_doc["_aigcMetadata"]["standard"] == "GB 45438-2025"
            assert data_doc["_aigcMetadata"]["contentGeneratedByAI"] is True
            assert data_doc["_aigcMetadata"]["provider"] == "MindWeave"
            assert len(data_doc["_aigcMetadata"]["contentSha256"]) == 64

            # Every export_section registered in DataRightsRegistry must exist in data.json!
            expected_sections = {
                reg.export_section
                for reg in data_rights_registry.all_registrations().values()
                if reg.export_section is not None
            }
            assert expected_sections.issubset(set(data_doc.keys()))

            # RT-01: Internal UNASSIGNED_PERSON_SLUG ("__unassigned__" / "待归档") excluded from persons & aliases,
            # while unassigned user records & facts are 100% preserved with personId=None!
            exported_slugs = {p["slug"] for p in data_doc["persons"]}
            exported_names = {p["displayName"] for p in data_doc["persons"]}
            assert UNASSIGNED_PERSON_SLUG not in exported_slugs
            assert "待归档" not in exported_names
            assert all(a["alias"] != "待归档" for a in data_doc["personAliases"])
            unassigned_mats = [m for m in data_doc["materials"] if m["title"] == "深夜未归属随笔"]
            assert len(unassigned_mats) == 1
            assert unassigned_mats[0]["personId"] is None
            unassigned_facts = [
                f for f in data_doc["facts"] if f["content"] == "待归属忌口：不喜欢香菜"
            ]
            assert len(unassigned_facts) == 1
            assert unassigned_facts[0]["personId"] is None

            # Device metadata exported WITHOUT raw push token
            assert len(data_doc["devices"]) == 1
            dev_entry = data_doc["devices"][0]
            assert dev_entry["deviceLabel"] == "文杰的 iPhone 16 Pro"
            assert "pushToken" not in dev_entry
            assert "push_token_enc" not in json.dumps(dev_entry)
            assert "SENSITIVE_RAW_APNS" not in json.dumps(data_doc)

            # Tenant isolation check: zero User B strings anywhere in User A's archive
            raw_data_str = zf.read("data.json").decode("utf-8")
            assert "沈清秋" in raw_data_str
            assert "严重芒果过敏" in raw_data_str
            assert "苏敏" not in raw_data_str
            assert "房东老顾" not in raw_data_str

            journal_md = zf.read("journal.md").decode("utf-8")
            assert "## 金箭 · 记爱" in journal_md
            assert "## 银箭 · 记恨" in journal_md
            assert "## 水晶箭 · 记繁琐" in journal_md
            assert "沈清秋" in journal_md
            assert "张主管" in journal_md
            assert "陈教授" in journal_md
            assert "### 待归档" not in journal_md
            assert UNASSIGNED_PERSON_SLUG not in journal_md
            assert "## 未归属随手记" in journal_md
            assert "她随口说不喜欢香菜味" in journal_md
            assert "[rec_" not in journal_md
            assert JOURNAL_AIGC_DISCLOSURE_FOOTER in journal_md

        # 4. Strict one-time download token: second download attempt immediately rejected!
        second_dl_resp = await client.get(
            f"/v1/me/exports/{export_id}/download",
            params={"token": download_token},
            headers={"Authorization": f"Bearer {access_a}"},
        )
        assert second_dl_resp.status_code == 403

        # 5. Queued async export + direct `build_export_archive(...)` + 24h expiry enforcement
        queued_resp = await client.post(
            "/v1/me/exports",
            json={"asyncMode": True},
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_a,
                "Idempotency-Key": "exp-queued-2",
            },
        )
        assert queued_resp.status_code == 201
        queued_export_id = UUID(queued_resp.json()["exportId"])
        assert queued_resp.json()["status"] == "pending"

        built_async = await build_export_archive(
            db_container,
            user_id=user_a,
            export_id=queued_export_id,
        )
        assert built_async.status == "completed"
        assert built_async.download_token is not None

        # Advance clock past 24h TTL and verify expired download token is rejected
        manual_clock.advance(hours=24, seconds=1)
        fresh_access_a, _ = db_container.tokens.create_access_token(user_a, now=manual_clock.now())
        expired_dl = await client.get(
            f"/v1/me/exports/{queued_export_id}/download",
            params={"token": built_async.download_token},
            headers={"Authorization": f"Bearer {fresh_access_a}"},
        )
        assert expired_dl.status_code == 403


@pytest.mark.asyncio
async def test_telemetry_api_strict_allowlist_batch_and_summary(
    db_container: AppContainer,
) -> None:
    _, access_a, _, _ = await _seed_rich_user(
        db_container,
        email="tel-a@mindweave.local",
        person_gold_name="林屿",
        person_silver_name="王总监",
        person_crystal_name="李工",
        voice_bytes=b"voice-a",
    )
    _, access_b, _, _ = await _seed_rich_user(
        db_container,
        email="tel-b@mindweave.local",
        person_gold_name="周舟",
        person_silver_name="赵组长",
        person_crystal_name="孙姐",
        voice_bytes=b"voice-b",
    )

    app = _build_test_app(db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing Idempotency-Key on POST /v1/telemetry/events is rejected with 400 (P1-7)
        no_idem_tel = await client.post(
            "/v1/telemetry/events",
            json={
                "eventName": "record_created",
                "metadata": {"source": "voice", "latency_ms": 290},
            },
            headers={"Authorization": f"Bearer {access_a}"},
        )
        assert no_idem_tel.status_code == 400

        # Valid single event + idempotent replay
        r1 = await client.post(
            "/v1/telemetry/events",
            json={
                "eventName": "record_created",
                "metadata": {"source": "voice", "latency_ms": 290},
            },
            headers={"Authorization": f"Bearer {access_a}", "Idempotency-Key": "tel-1"},
        )
        assert r1.status_code == 201
        assert r1.json()["acceptedCount"] == 1

        r1_replay = await client.post(
            "/v1/telemetry/events",
            json={
                "eventName": "record_created",
                "metadata": {"source": "voice", "latency_ms": 290},
            },
            headers={"Authorization": f"Bearer {access_a}", "Idempotency-Key": "tel-1"},
        )
        assert r1_replay.status_code == 201
        assert r1_replay.json()["eventIds"] == r1.json()["eventIds"]

        # Valid batch events
        r2 = await client.post(
            "/v1/telemetry/events",
            json={
                "events": [
                    {
                        "eventName": "ask_replied",
                        "metadata": {"cited_count": 2, "has_gap": False, "tier": "gold"},
                    },
                    {"eventName": "record_undo", "metadata": {"reason_optional": "mistake"}},
                    {
                        "eventName": "record_created",
                        "metadata": {"source": "text", "latency_ms": 110},
                    },
                ]
            },
            headers={"Authorization": f"Bearer {access_a}", "Idempotency-Key": "tel-2"},
        )
        assert r2.status_code == 201
        assert r2.json()["acceptedCount"] == 3

        # Reject unknown event_name, forbidden PII keys, >32 char string, and >50 batch with 422 ValidationFailedError
        for idx, invalid_payload in enumerate(
            [
                {"eventName": "raw_keystroke_dump", "metadata": {}},
                {
                    "eventName": "record_created",
                    "metadata": {"source": "voice", "person_name": "林屿"},
                },
                {"eventName": "ask_replied", "metadata": {"message": "帮我写一封信"}},
                {"eventName": "fact_confirmed", "metadata": {"confidence_bucket": "x" * 33}},
                {"eventName": "record_undo", "metadata": {"reason_optional": "写错人名林屿了"}},
                {
                    "events": [
                        {"eventName": "data_export", "metadata": {"status": "completed"}}
                        for _ in range(51)
                    ]
                },
            ]
        ):
            bad = await client.post(
                "/v1/telemetry/events",
                json=invalid_payload,
                headers={
                    "Authorization": f"Bearer {access_a}",
                    "Idempotency-Key": f"tel-bad-{idx}",
                },
            )
            assert bad.status_code == 422
            assert bad.json()["code"] == "validation_failed"

        # User A summary reflects only valid events; User B sees 0 events (RLS tenant isolation)
        sum_a = await client.get(
            "/v1/telemetry/summary",
            headers={"Authorization": f"Bearer {access_a}"},
        )
        assert sum_a.status_code == 200
        assert sum_a.json()["totalEvents"] == 4
        assert sum_a.json()["countsByEvent"] == {
            "ask_replied": 1,
            "record_created": 2,
            "record_undo": 1,
        }

        sum_b = await client.get(
            "/v1/telemetry/summary",
            headers={"Authorization": f"Bearer {access_b}"},
        )
        assert sum_b.status_code == 200
        assert sum_b.json()["totalEvents"] == 0


@pytest.mark.asyncio
async def test_account_deletion_crypto_shredding_cascade_and_token_revocation(
    db_container: AppContainer,
) -> None:
    # 1. Prove standalone crypto-shredding makes existing ciphertext immediately unreadable even before row deletion
    shred_only_uid, _, _, _ = await _seed_rich_user(
        db_container,
        email="shred-check@mindweave.local",
        person_gold_name="温宁",
        person_silver_name="钱主管",
        person_crystal_name="吴老师",
        voice_bytes=b"shred-voice",
    )
    svc = DataRightsService(db_container)
    await svc.crypto_shred_user_key(scope=UserScope(shred_only_uid))
    async with db_container.db.user_session(UserScope(shred_only_uid)) as sess:
        key_row = await sess.get(UserKeyRow, shred_only_uid)
        assert key_row is not None
        assert key_row.wrapped_dek is None
        assert key_row.destroyed_at is not None
        with pytest.raises(KeyShreddedError):
            await db_container.load_user_dek(sess, UserScope(shred_only_uid))

    # 2. Full DELETE /v1/me end-to-end flow for User A while keeping User B completely untouched
    user_a, access_a, reauth_a, media_key_a = await _seed_rich_user(
        db_container,
        email="delete-me@mindweave.local",
        person_gold_name="沈清秋",
        person_silver_name="张主管",
        person_crystal_name="陈教授",
        voice_bytes=b"ALICE-ENCRYPTED-MEDIA-TO-BE-PURGED",
    )
    user_b, access_b, _, media_key_b = await _seed_rich_user(
        db_container,
        email="keep-me@mindweave.local",
        person_gold_name="苏敏",
        person_silver_name="李经理",
        person_crystal_name="老顾",
        voice_bytes=b"BOB-ENCRYPTED-MEDIA-MUST-SURVIVE",
    )

    blob_path_a = db_container.settings.blob_storage_dir / media_key_a
    blob_path_b = db_container.settings.blob_storage_dir / media_key_b
    assert blob_path_a.exists()
    assert blob_path_b.exists()

    app = _build_test_app(db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # DELETE /v1/me requires both X-Reauth-Token and Idempotency-Key
        missing_reauth = await client.delete(
            "/v1/me",
            headers={"Authorization": f"Bearer {access_a}", "Idempotency-Key": "del-no-reauth"},
        )
        assert missing_reauth.status_code == 403
        assert missing_reauth.json()["code"] == "reauth_required"

        missing_idem = await client.delete(
            "/v1/me",
            headers={"Authorization": f"Bearer {access_a}", "X-Reauth-Token": reauth_a},
        )
        assert missing_idem.status_code == 400

        # Execute irreversible account deletion
        del_resp = await client.delete(
            "/v1/me",
            headers={
                "Authorization": f"Bearer {access_a}",
                "X-Reauth-Token": reauth_a,
                "Idempotency-Key": "del-confirm-1",
            },
        )
        assert del_resp.status_code == 200
        del_body = del_resp.json()
        assert del_body["deleted"] is True
        assert del_body["cryptoShredded"] is True
        assert del_body["deletedBlobsCount"] >= 1
        assert del_body["cancelledJobsCount"] == 1

        # Media blobs under user_a prefix physically deleted; user_b media untouched
        assert not blob_path_a.exists()
        assert not (db_container.settings.blob_storage_dir / str(user_a)).exists()
        assert blob_path_b.exists()

        # Verify physical cascade deletion across users + auth_identities + refresh_tokens + ALL 19 RLS tables
        async with db_container.db.system_session() as sys_sess:
            assert await sys_sess.get(UserRow, user_a) is None
            assert await sys_sess.get(UserRow, user_b) is not None

            for sys_tbl_model in (AuthIdentityRow, RefreshTokenRow):
                cnt = await sys_sess.scalar(
                    select(func.count())
                    .select_from(sys_tbl_model)
                    .where(sys_tbl_model.user_id == user_a)
                )
                assert cnt == 0

            from mindweave.core.db import Base

            for tbl_name in RLS_PROTECTED_TABLES:
                tbl = Base.metadata.tables[tbl_name]
                cnt_a = await sys_sess.scalar(
                    select(func.count()).select_from(tbl).where(tbl.c.user_id == user_a)
                )
                assert cnt_a == 0, f"Residual row found in {tbl_name} after DELETE /me"

            # Queued background jobs for user_a were cancelled
            queued_cnt = await sys_sess.scalar(
                select(func.count())
                .select_from(BackgroundJobRow)
                .where(
                    BackgroundJobRow.user_id == user_a,
                    BackgroundJobRow.status == "queued",
                )
            )
            assert queued_cnt == 0

            # Anonymized compliance tombstone exists in audit_log with zero PII and zero raw user_id
            tombstone = await sys_sess.scalar(
                select(AuditLogRow).where(
                    AuditLogRow.id == UUID(del_body["auditReceiptId"]),
                    AuditLogRow.action == "data_rights.account_deleted",
                )
            )
            assert tombstone is not None
            serialized_audit = json.dumps(
                {
                    "actor": tombstone.actor_user_hash,
                    "target_id": tombstone.target_id,
                    "metadata": tombstone.metadata_json,
                }
            )
            assert str(user_a) not in serialized_audit
            assert "delete-me@mindweave.local" not in serialized_audit
            assert "沈清秋" not in serialized_audit

        # Any subsequent request with the old accessToken MUST immediately fail with 401 UnauthorizedError!
        for method, path in [
            ("GET", "/v1/me/exports"),
            ("GET", "/v1/telemetry/summary"),
            ("DELETE", "/v1/me"),
        ]:
            post_del_resp = await client.request(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {access_a}",
                    "X-Reauth-Token": reauth_a,
                    "Idempotency-Key": "after-delete",
                },
            )
            assert post_del_resp.status_code == 401
            assert post_del_resp.json()["code"] == "unauthorized"

        # Meanwhile User B continues to work normally
        ok_b = await client.get(
            "/v1/me/exports",
            headers={"Authorization": f"Bearer {access_b}"},
        )
        assert ok_b.status_code == 200
