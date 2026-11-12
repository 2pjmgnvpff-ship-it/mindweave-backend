"""Live PostgreSQL & FastAPI integration tests for Identity, Step-Up Reauth, Consents & Preferences."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI
from PIL import Image
from sqlalchemy import func, select

from mindweave.core.clock import ManualClock
from mindweave.core.crypto import CryptoError
from mindweave.core.dependencies import AppContainer, require_step_up_reauth
from mindweave.core.errors import register_exception_handlers
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    AuditLogRow,
    AuthIdentityRow,
    PersonRow,
    RefreshTokenRow,
    UserKeyRow,
    UserPreferenceRow,
    UserRow,
)
from mindweave.core.security import hash_contact_lookup
from mindweave.modules.identity.api.router import router as identity_router
from mindweave.modules.identity.domain.models import OtpPurpose
from mindweave.modules.preferences.api.router import router as preferences_router
from mindweave.shared.scopes import UserScope


class _CaptureOtpSender:
    """In-memory test OTP sender to test `expose_dev_otp_in_response=False` safely."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str, OtpPurpose]] = []

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
    ) -> None:
        del expires_in_seconds
        self.deliveries.append((email, otp_code, purpose))


def _build_test_app(container: AppContainer) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    register_exception_handlers(app)
    app.include_router(identity_router, prefix="/v1")
    app.include_router(preferences_router, prefix="/v1")

    @app.post("/v1/_test/sensitive-op")
    async def _sensitive_op(scope: UserScope = Depends(require_step_up_reauth)) -> dict[str, str]:
        return {"userId": str(scope.user_id)}

    return app


def _make_exif_jpeg(color: tuple[int, int, int] = (205, 175, 145)) -> bytes:
    img = Image.new("RGB", (48, 48), color=color)
    exif = Image.Exif()
    exif[0x010F] = "iPhonePrivateMake"
    exif[0x0110] = "iPhone17Pro-Sensitive"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _make_png(color: tuple[int, int, int, int] = (90, 130, 170, 255)) -> bytes:
    img = Image.new("RGBA", (32, 32), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def api_client(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> AsyncIterator[httpx.AsyncClient]:
    from datetime import UTC, datetime

    # Anchor manual_clock prior to wall-clock UTC so PyJWT's built-in `iat` check in core.security passes
    manual_clock.set(datetime(2026, 11, 11, 10, 0, 0, tzinfo=UTC))
    app = _build_test_app(db_container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_apple_auth_first_time_provisioning_empty_workspace_and_aad_encryption(
    api_client: httpx.AsyncClient,
    db_container: AppContainer,
) -> None:
    # 1. First-time Apple sign-in provisions UserRow, UserKeyRow (via RLS), UserPreferenceRow, ZERO PersonRows!
    resp1 = await api_client.post(
        "/v1/auth/apple",
        json={
            "identityToken": "dev_apple:apple-sub-001",
            "authorizationCode": "auth-code-xyz",
            "displayEmail": "Shen.QingQiu@Example.com",
        },
    )
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["isNewUser"] is True
    assert body1["expiresIn"] == db_container.settings.access_token_ttl_seconds
    user_id = UUID(body1["userId"])

    # Verify DB state & AAD-bound envelope encryption
    scope = UserScope(user_id)
    async with db_container.db.user_session(scope) as user_sess:
        key_row = await user_sess.get(UserKeyRow, user_id)
        assert key_row is not None
        assert key_row.wrapped_dek is not None

        pref_row = await user_sess.get(UserPreferenceRow, user_id)
        assert pref_row is not None
        assert pref_row.preferred_edition == "cotton"
        assert pref_row.cupid_layout == "a"
        assert pref_row.version == 1

        # Strictly ZERO fictional/demo people created on initial provisioning!
        people_count = await user_sess.scalar(select(func.count(PersonRow.id)))
        assert people_count == 0

        dek = await db_container.load_user_dek(user_sess, scope)

    async with db_container.db.system_session() as sys_sess:
        user_row = await sys_sess.get(UserRow, user_id)
        assert user_row is not None and user_row.status == "active"

        ident_row = await sys_sess.scalar(
            select(AuthIdentityRow).where(AuthIdentityRow.user_id == user_id)
        )
        assert ident_row is not None
        assert ident_row.provider == "apple"
        assert ident_row.subject_hmac == hash_contact_lookup(
            "apple-sub-001", secret=db_container.settings.jwt_secret
        )
        assert ident_row.contact_enc is not None
        assert b"shen.qingqiu@example.com" not in ident_row.contact_enc

        decrypted_contact = db_container.cipher.decrypt_str(
            dek,
            ident_row.contact_enc,
            user_id=user_id,
            table="auth_identities",
            column="contact_enc",
            row_id=ident_row.id,
        )
        assert decrypted_contact == "shen.qingqiu@example.com"

        # Wrong AAD row_id MUST fail GCM authentication
        with pytest.raises(CryptoError):
            db_container.cipher.decrypt_str(
                dek,
                ident_row.contact_enc,
                user_id=user_id,
                table="auth_identities",
                column="contact_enc",
                row_id=new_id(),
            )

        audit_actions = list((await sys_sess.scalars(select(AuditLogRow.action))).all())
        assert "auth.login" in audit_actions

    # 2. Second login with same Apple identity returns existing user (`isNewUser: false`)
    resp2 = await api_client.post(
        "/v1/auth/apple",
        json={"identityToken": "dev_apple:apple-sub-001"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["userId"] == str(user_id)
    assert resp2.json()["isNewUser"] is False


@pytest.mark.asyncio
async def test_email_otp_login_lockout_expiry_ratelimit_and_dev_otp_hiding(
    api_client: httpx.AsyncClient,
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    # 1. Brute-force lockout persists attempts across rollbacks!
    req_lock = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "lockout@example.com", "purpose": "login"},
    )
    assert req_lock.status_code == 200
    lock_challenge_id = req_lock.json()["challengeId"]
    right_code = req_lock.json()["devOtpCode"]
    wrong_code = "000000" if right_code != "000000" else "999999"

    for attempt_idx in range(db_container.settings.otp_max_attempts):
        bad_resp = await api_client.post(
            "/v1/auth/email/otp:verify",
            json={
                "challengeId": lock_challenge_id,
                "email": "lockout@example.com",
                "code": wrong_code,
            },
        )
        assert bad_resp.status_code == 401
        if attempt_idx == db_container.settings.otp_max_attempts - 1:
            assert bad_resp.json()["code"] == "otp_max_attempts_exceeded"

    # Even the CORRECT code is now rejected because challenge is locked out!
    after_lockout = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": lock_challenge_id,
            "email": "lockout@example.com",
            "code": right_code,
        },
    )
    assert after_lockout.status_code == 401
    assert after_lockout.json()["code"] == "otp_max_attempts_exceeded"

    # 2. Expiry rejection
    req_exp = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "expire@example.com", "purpose": "login"},
    )
    exp_challenge_id = req_exp.json()["challengeId"]
    exp_code = req_exp.json()["devOtpCode"]
    manual_clock.advance(seconds=db_container.settings.otp_ttl_seconds + 1)
    exp_verify = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": exp_challenge_id,
            "email": "expire@example.com",
            "code": exp_code,
        },
    )
    assert exp_verify.status_code == 401
    assert exp_verify.json()["code"] == "otp_expired"

    # 3. Hide devOtpCode when expose_dev_otp_in_response is False + happy-path login & replay block
    outbox = _CaptureOtpSender()
    db_container.extras["otp_sender"] = outbox
    db_container.settings.expose_dev_otp_in_response = False

    req_prod = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "  Lin.ZhiXia@Example.com ", "purpose": "login"},
    )
    assert req_prod.status_code == 200
    assert "devOtpCode" not in req_prod.json()
    assert len(outbox.deliveries) == 1
    sent_email, sent_code, sent_purpose = outbox.deliveries[0]
    assert sent_email == "lin.zhixia@example.com"
    assert sent_purpose is OtpPurpose.LOGIN

    verify_ok = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": req_prod.json()["challengeId"],
            "email": "lin.zhixia@example.com",
            "code": sent_code,
        },
    )
    assert verify_ok.status_code == 200
    assert verify_ok.json()["isNewUser"] is True

    # Reusing a consumed challenge fails immediately
    verify_replay = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": req_prod.json()["challengeId"],
            "email": "lin.zhixia@example.com",
            "code": sent_code,
        },
    )
    assert verify_replay.status_code == 401
    assert verify_replay.json()["code"] == "otp_already_consumed"

    # 4. Per-email HMAC rate limiting (case-insensitive normalized email)
    for _ in range(5):
        r = await api_client.post(
            "/v1/auth/email/otp:request",
            json={"email": "burst@example.com", "purpose": "login"},
        )
        assert r.status_code == 200

    rate_limited = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "  BURST@Example.com ", "purpose": "login"},
    )
    assert rate_limited.status_code == 429
    assert rate_limited.json()["code"] == "rate_limited"

    # 5. Re-issuing OTP invalidates older unconsumed OTPs (RT-11) & advisory-locked concurrency
    db_container.settings.expose_dev_otp_in_response = True
    r1 = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "reissue@example.com", "purpose": "login"},
    )
    r2 = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "reissue@example.com", "purpose": "login"},
    )
    assert r1.status_code == 200 and r2.status_code == 200

    # OTP #1 MUST now fail with 401 Unauthorized because OTP #2 invalidated it
    v1 = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": r1.json()["challengeId"],
            "email": "reissue@example.com",
            "code": r1.json()["devOtpCode"],
        },
    )
    assert v1.status_code == 401
    assert v1.json()["code"] == "otp_already_consumed"

    # OTP #2 succeeds!
    v2 = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": r2.json()["challengeId"],
            "email": "reissue@example.com",
            "code": r2.json()["devOtpCode"],
        },
    )
    assert v2.status_code == 200

    # Concurrent burst of 8 OTP requests for the same email is serialized by pg_advisory_xact_lock:
    # exactly 5 succeed (200) and 3 are rate-limited (429)!
    burst_resps = await asyncio.gather(
        *[
            api_client.post(
                "/v1/auth/email/otp:request",
                json={"email": "concurrent-lock@example.com", "purpose": "login"},
            )
            for _ in range(8)
        ]
    )
    status_codes = [resp.status_code for resp in burst_resps]
    assert status_codes.count(200) == 5
    assert status_codes.count(429) == 3


@pytest.mark.asyncio
async def test_step_up_reauth_email_and_apple_end_to_end(
    api_client: httpx.AsyncClient,
    db_container: AppContainer,
) -> None:
    # Provision User A via Email OTP
    otp_req_a = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "user.a@example.com", "purpose": "login"},
    )
    login_a = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": otp_req_a.json()["challengeId"],
            "email": "user.a@example.com",
            "code": otp_req_a.json()["devOtpCode"],
        },
    )
    token_a = login_a.json()["accessToken"]
    user_a_id = login_a.json()["userId"]

    # Provision User B via Apple Sign-In
    login_b = await api_client.post(
        "/v1/auth/apple",
        json={"identityToken": "dev_apple:apple-user-b"},
    )
    token_b = login_b.json()["accessToken"]
    user_b_id = login_b.json()["userId"]

    # Sensitive endpoint without X-Reauth-Token returns 403 reauth_required
    no_reauth = await api_client.post(
        "/v1/_test/sensitive-op",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert no_reauth.status_code == 403
    assert no_reauth.json()["code"] == "reauth_required"

    # Request email reauth challenge for User A
    reauth_chal = await api_client.post(
        "/v1/auth/email/otp:request",
        json={"email": "user.a@example.com", "purpose": "reauth"},
    )
    chal_id = reauth_chal.json()["challengeId"]
    chal_code = reauth_chal.json()["devOtpCode"]

    # Unauthenticated verify on a reauth challenge -> 401
    unauth_reauth = await api_client.post(
        "/v1/auth/email/otp:verify",
        json={"challengeId": chal_id, "email": "user.a@example.com", "code": chal_code},
    )
    assert unauth_reauth.status_code == 401

    # User B attempting to verify User A's email for reauth -> 403 Forbidden!
    cross_user_reauth = await api_client.post(
        "/v1/auth/email/otp:verify",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"challengeId": chal_id, "email": "user.a@example.com", "code": chal_code},
    )
    assert cross_user_reauth.status_code == 403

    # User A verifying own email -> issues step-up reauthToken!
    ok_email_reauth = await api_client.post(
        "/v1/auth/email/otp:verify",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"challengeId": chal_id, "email": "user.a@example.com", "code": chal_code},
    )
    assert ok_email_reauth.status_code == 200
    reauth_token_a = ok_email_reauth.json()["reauthToken"]
    assert ok_email_reauth.json()["expiresIn"] == db_container.settings.reauth_token_ttl_seconds

    # Sensitive op succeeds with X-Reauth-Token for User A (and fails if stolen by User B!)
    sens_ok_a = await api_client.post(
        "/v1/_test/sensitive-op",
        headers={
            "Authorization": f"Bearer {token_a}",
            "X-Reauth-Token": reauth_token_a,
        },
    )
    assert sens_ok_a.status_code == 200
    assert sens_ok_a.json()["userId"] == user_a_id

    sens_stolen_by_b = await api_client.post(
        "/v1/_test/sensitive-op",
        headers={
            "Authorization": f"Bearer {token_b}",
            "X-Reauth-Token": reauth_token_a,
        },
    )
    assert sens_stolen_by_b.status_code == 403

    # Apple step-up reauth: wrong Apple sub -> 403; bound Apple sub -> 200 reauthToken!
    bad_apple_reauth = await api_client.post(
        "/v1/auth/reauth/apple",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"identityToken": "dev_apple:wrong-apple-sub"},
    )
    assert bad_apple_reauth.status_code == 403

    ok_apple_reauth = await api_client.post(
        "/v1/auth/reauth/apple",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"identityToken": "dev_apple:apple-user-b"},
    )
    assert ok_apple_reauth.status_code == 200
    reauth_token_b = ok_apple_reauth.json()["reauthToken"]

    sens_ok_b = await api_client.post(
        "/v1/_test/sensitive-op",
        headers={
            "Authorization": f"Bearer {token_b}",
            "X-Reauth-Token": reauth_token_b,
        },
    )
    assert sens_ok_b.status_code == 200
    assert sens_ok_b.json()["userId"] == user_b_id

    async with db_container.db.system_session() as sys_sess:
        reauth_audits = list(
            (
                await sys_sess.scalars(
                    select(AuditLogRow).where(AuditLogRow.action == "auth.reauth")
                )
            ).all()
        )
        assert len(reauth_audits) == 2


@pytest.mark.asyncio
async def test_refresh_token_family_rotation_replay_detection_and_logout(
    api_client: httpx.AsyncClient,
    db_container: AppContainer,
) -> None:
    # Device 1 login -> family 1
    login1 = await api_client.post(
        "/v1/auth/apple",
        json={"identityToken": "dev_apple:rotation-user"},
    )
    access1 = login1.json()["accessToken"]
    rt1 = login1.json()["refreshToken"]
    user_id = UUID(login1.json()["userId"])

    # Device 2 login -> family 2
    login2 = await api_client.post(
        "/v1/auth/apple",
        json={"identityToken": "dev_apple:rotation-user"},
    )
    rt_other_device = login2.json()["refreshToken"]

    # Rotate rt1 -> rt2 -> rt3
    rot1 = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt1})
    assert rot1.status_code == 200
    rt2 = rot1.json()["refreshToken"]
    assert rt2 != rt1

    rot2 = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt2})
    assert rot2.status_code == 200
    rt3 = rot2.json()["refreshToken"]
    assert rt3 not in (rt1, rt2)

    # TOKEN REPLAY ATTACK: Attacker replays already-rotated rt1!
    replay_resp = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt1})
    assert replay_resp.status_code == 401
    assert replay_resp.json()["code"] == "refresh_token_replayed"

    # Verify family revocation was COMMITTED before raising UnauthorizedError:
    # Legitimate rt3 in the compromised family MUST now also be revoked!
    rt3_after_replay = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt3})
    assert rt3_after_replay.status_code == 401

    # Audit log recorded "auth.refresh_replay_revoked"
    async with db_container.db.system_session() as sys_sess:
        replay_audits = list(
            (
                await sys_sess.scalars(
                    select(AuditLogRow).where(AuditLogRow.action == "auth.refresh_replay_revoked")
                )
            ).all()
        )
        assert len(replay_audits) == 2
        assert replay_audits[0].metadata_json["revokedFamilyCount"] == 1

        family_rows = list(
            (
                await sys_sess.scalars(
                    select(RefreshTokenRow).where(RefreshTokenRow.user_id == user_id)
                )
            ).all()
        )
        # 3 tokens in family 1 (all revoked) + 1 token in family 2 (still active!)
        revoked = [r for r in family_rows if r.revoked_at is not None]
        active = [r for r in family_rows if r.revoked_at is None]
        assert len(revoked) == 3
        assert len(active) == 1

    # Device 2's family is unaffected until logout
    rot_other = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt_other_device})
    assert rot_other.status_code == 200
    rt_other_2 = rot_other.json()["refreshToken"]

    # Logout without refreshToken revokes all remaining refresh tokens for user
    logout_resp = await api_client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {access1}"},
    )
    assert logout_resp.status_code == 200
    assert logout_resp.json() == {"ok": True}

    after_logout = await api_client.post("/v1/auth/refresh", json={"refreshToken": rt_other_2})
    assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_pipl_consents_idempotency_revocation_and_rls_isolation(
    api_client: httpx.AsyncClient,
) -> None:
    u1 = (
        await api_client.post("/v1/auth/apple", json={"identityToken": "dev_apple:consent-u1"})
    ).json()
    u2 = (
        await api_client.post("/v1/auth/apple", json={"identityToken": "dev_apple:consent-u2"})
    ).json()
    h1 = {"Authorization": f"Bearer {u1['accessToken']}"}
    h2 = {"Authorization": f"Bearer {u2['accessToken']}"}

    # Missing Idempotency-Key -> 400
    missing_idem = await api_client.post(
        "/v1/me/consents",
        headers=h1,
        json={"consentKind": "privacy_policy", "policyVersion": "2026.11", "granted": True},
    )
    assert missing_idem.status_code == 400

    # Grant all 3 PIPL consent kinds
    for idx, kind in enumerate(("privacy_policy", "sensitive_personal_info", "ai_processing")):
        res = await api_client.post(
            "/v1/me/consents",
            headers={**h1, "Idempotency-Key": f"idem-consent-{idx}"},
            json={"consentKind": kind, "policyVersion": "2026.11", "granted": True},
        )
        assert res.status_code == 200
        assert res.json()["consentKind"] == kind
        assert res.json()["granted"] is True
        assert res.json()["revokedAt"] is None

    # Idempotent replay returns identical record
    replay = await api_client.post(
        "/v1/me/consents",
        headers={**h1, "Idempotency-Key": "idem-consent-2"},
        json={"consentKind": "ai_processing", "policyVersion": "2026.11", "granted": True},
    )
    assert replay.status_code == 200
    assert replay.json()["consentKind"] == "ai_processing"

    # Reusing Idempotency-Key with different payload -> 409 idempotency_conflict
    conflict = await api_client.post(
        "/v1/me/consents",
        headers={**h1, "Idempotency-Key": "idem-consent-2"},
        json={"consentKind": "ai_processing", "policyVersion": "2026.11", "granted": False},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"

    # Revoke ai_processing consent
    revoked = await api_client.post(
        "/v1/me/consents",
        headers={**h1, "Idempotency-Key": "idem-consent-revoke"},
        json={"consentKind": "ai_processing", "policyVersion": "2026.11", "granted": False},
    )
    assert revoked.status_code == 200
    assert revoked.json()["granted"] is False
    assert revoked.json()["revokedAt"] is not None

    list_u1 = await api_client.get("/v1/me/consents", headers=h1)
    assert list_u1.status_code == 200
    assert len(list_u1.json()["items"]) == 3

    # Tenant isolation: User 2 sees zero consents
    list_u2 = await api_client.get("/v1/me/consents", headers=h2)
    assert list_u2.status_code == 200
    assert list_u2.json()["items"] == []


@pytest.mark.asyncio
async def test_preferences_patch_if_match_idempotency_and_encrypted_exif_free_wallpaper(
    api_client: httpx.AsyncClient,
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    u1 = (
        await api_client.post("/v1/auth/apple", json={"identityToken": "dev_apple:prefs-u1"})
    ).json()
    u2 = (
        await api_client.post("/v1/auth/apple", json={"identityToken": "dev_apple:prefs-u2"})
    ).json()
    user1_id = UUID(u1["userId"])
    h1 = {"Authorization": f"Bearer {u1['accessToken']}"}
    h2 = {"Authorization": f"Bearer {u2['accessToken']}"}

    # 1. Initial GET /me/preferences
    init_resp = await api_client.get("/v1/me/preferences", headers=h1)
    assert init_resp.status_code == 200
    init_prefs = init_resp.json()
    assert init_prefs == {
        "preferredEdition": "cotton",
        "cupidLayout": "a",
        "wallpaperPreset": "warm_paper",
        "hasCustomWallpaper": False,
        "wallpaperUrl": None,
        "wallpaperBlur": 12,
        "wallpaperVignette": 20,
        "appLockEnabled": False,
        "lateNightCareEnabled": True,
        "timezone": "Asia/Shanghai",
        "version": 1,
    }

    # 2. Invalid field bounds & timezone rejected with 422
    bad_tz = await api_client.patch(
        "/v1/me/preferences",
        headers={**h1, "Idempotency-Key": "bad-tz"},
        json={"timezone": "Not/AZone"},
    )
    assert bad_tz.status_code == 422

    bad_blur = await api_client.patch(
        "/v1/me/preferences",
        headers={**h1, "Idempotency-Key": "bad-blur"},
        json={"wallpaperBlur": 49},
    )
    assert bad_blur.status_code == 422

    # 3. Valid PATCH with If-Match: 1 -> increments version to 2
    patch_body = {
        "preferredEdition": "hobonichi",
        "cupidLayout": "c",
        "wallpaperPreset": "stone_linen",
        "wallpaperBlur": 28,
        "wallpaperVignette": 55,
        "appLockEnabled": True,
        "lateNightCareEnabled": False,
        "timezone": "Asia/Tokyo",
    }
    patched = await api_client.patch(
        "/v1/me/preferences",
        headers={**h1, "Idempotency-Key": "patch-1", "If-Match": '"1"'},
        json=patch_body,
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2
    assert patched.json()["preferredEdition"] == "hobonichi"
    assert patched.json()["cupidLayout"] == "c"
    assert patched.json()["timezone"] == "Asia/Tokyo"
    assert patched.json()["wallpaperUrl"] is None

    # Idempotent replay with same Idempotency-Key + If-Match: 1 returns cached 200!
    patch_replay = await api_client.patch(
        "/v1/me/preferences",
        headers={**h1, "Idempotency-Key": "patch-1", "If-Match": '"1"'},
        json=patch_body,
    )
    assert patch_replay.status_code == 200
    assert patch_replay.json()["version"] == 2

    # Stale If-Match: 1 with new Idempotency-Key -> 412 Precondition Failed!
    stale_patch = await api_client.patch(
        "/v1/me/preferences",
        headers={**h1, "Idempotency-Key": "patch-stale", "If-Match": "1"},
        json={"wallpaperBlur": 8},
    )
    assert stale_patch.status_code == 412
    assert stale_patch.json()["code"] == "version_mismatch"

    # 4. Wallpaper lifecycle: initially 404; missing Idempotency-Key on POST/DELETE -> 400 (P1-7)
    no_wp = await api_client.get("/v1/me/wallpaper", headers=h1)
    assert no_wp.status_code == 404

    missing_idem_post = await api_client.post(
        "/v1/me/wallpaper",
        headers=h1,
        files={"file": ("photo.jpg", _make_exif_jpeg(), "image/jpeg")},
    )
    assert missing_idem_post.status_code == 400

    missing_idem_del = await api_client.delete("/v1/me/wallpaper", headers=h1)
    assert missing_idem_del.status_code == 400

    # Reject non-image spoofed upload
    spoof_wp = await api_client.post(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-spoof"},
        files={"file": ("fake.jpg", b"not-a-real-jpeg", "image/jpeg")},
    )
    assert spoof_wp.status_code == 422

    # Reject oversized upload > max_image_bytes
    orig_max = db_container.settings.max_image_bytes
    db_container.settings.max_image_bytes = 64
    try:
        oversized_wp = await api_client.post(
            "/v1/me/wallpaper",
            headers={**h1, "Idempotency-Key": "wp-oversized"},
            files={"file": ("big.jpg", _make_exif_jpeg(), "image/jpeg")},
        )
        assert oversized_wp.status_code == 413
    finally:
        db_container.settings.max_image_bytes = orig_max

    # Upload JPEG containing sensitive EXIF tags
    jpeg_with_exif = _make_exif_jpeg()
    assert b"iPhonePrivateMake" in jpeg_with_exif
    up1 = await api_client.post(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-up-1"},
        files={"file": ("photo.jpg", jpeg_with_exif, "application/octet-stream")},
    )
    assert up1.status_code == 200
    assert up1.json()["hasCustomWallpaper"] is True
    assert up1.json()["version"] == 3
    signed_wp_url = up1.json()["wallpaperUrl"]
    assert isinstance(signed_wp_url, str)
    assert signed_wp_url.startswith(f"/v1/me/wallpaper?uid={user1_id}&exp=")
    assert "&sig=" in signed_wp_url

    # Idempotent replay of POST /v1/me/wallpaper returns cached version=3; different bytes -> 409!
    up1_replay = await api_client.post(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-up-1"},
        files={"file": ("photo.jpg", jpeg_with_exif, "application/octet-stream")},
    )
    assert up1_replay.status_code == 200
    assert up1_replay.json()["version"] == 3

    up1_conflict = await api_client.post(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-up-1"},
        files={"file": ("other.png", _make_png(), "image/png")},
    )
    assert up1_conflict.status_code == 409
    assert up1_conflict.json()["code"] == "idempotency_conflict"

    # Inspect raw disk storage: must be encrypted under {user_id}/wallpaper/{uuid} with zero plaintext!
    scope1 = UserScope(user1_id)
    async with db_container.db.user_session(scope1) as sess:
        pref_row = await sess.get(UserPreferenceRow, user1_id)
        assert pref_row is not None and pref_row.wallpaper_object_key is not None
        first_key = pref_row.wallpaper_object_key

    assert first_key.startswith(f"{user1_id}/wallpaper/")
    first_disk_path = db_container.settings.blob_storage_dir / first_key
    assert first_disk_path.exists()
    encrypted_on_disk = first_disk_path.read_bytes()
    assert encrypted_on_disk[0] == 0x01  # Envelope cipher v1 header
    assert not encrypted_on_disk.startswith(b"\xff\xd8\xff")
    assert b"iPhonePrivateMake" not in encrypted_on_disk

    # Stream decrypted wallpaper via Bearer header -> valid JPEG, ZERO EXIF!
    stream1 = await api_client.get("/v1/me/wallpaper", headers=h1)
    assert stream1.status_code == 200
    assert stream1.headers["content-type"] == "image/jpeg"
    assert b"iPhonePrivateMake" not in stream1.content
    with Image.open(io.BytesIO(stream1.content)) as decoded1:
        assert decoded1.format == "JPEG"
        assert len(decoded1.getexif()) == 0

    # [P2-1] Browser `<img src>` streaming via signed media URL WITHOUT Authorization header!
    browser_img = await api_client.get(signed_wp_url)
    assert browser_img.status_code == 200
    assert browser_img.headers["content-type"] == "image/jpeg"
    assert browser_img.content == stream1.content

    # Unauthenticated without query ticket -> 401; tampered signature -> 401
    unauth_wp = await api_client.get("/v1/me/wallpaper")
    assert unauth_wp.status_code == 401

    tampered_wp = await api_client.get(f"{signed_wp_url[:-4]}0000")
    assert tampered_wp.status_code == 401

    # Expired signed URL -> 401
    manual_clock.advance(seconds=901)
    expired_wp = await api_client.get(signed_wp_url)
    assert expired_wp.status_code == 401
    refreshed_u1 = (
        await api_client.post("/v1/auth/refresh", json={"refreshToken": u1["refreshToken"]})
    ).json()
    h1 = {"Authorization": f"Bearer {refreshed_u1['accessToken']}"}

    # Cross-tenant isolation: User 2 has no custom wallpaper (404)
    refreshed_u2 = (
        await api_client.post("/v1/auth/refresh", json={"refreshToken": u2["refreshToken"]})
    ).json()
    h2 = {"Authorization": f"Bearer {refreshed_u2['accessToken']}"}
    u2_wp = await api_client.get("/v1/me/wallpaper", headers=h2)
    assert u2_wp.status_code == 404

    # Replace wallpaper with PNG -> old JPEG blob on disk MUST be deleted!
    up2 = await api_client.post(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-up-2"},
        files={"file": ("next.png", _make_png(), "image/png")},
    )
    assert up2.status_code == 200
    assert not first_disk_path.exists()

    stream2 = await api_client.get("/v1/me/wallpaper", headers=h1)
    assert stream2.status_code == 200
    assert stream2.headers["content-type"] == "image/png"

    async with db_container.db.user_session(scope1) as sess:
        pref_row2 = await sess.get(UserPreferenceRow, user1_id)
        assert pref_row2 is not None and pref_row2.wallpaper_object_key is not None
        second_disk_path = db_container.settings.blob_storage_dir / pref_row2.wallpaper_object_key
        assert second_disk_path.exists()

    # DELETE /v1/me/wallpaper with Idempotency-Key -> removes disk blob and clears wallpaper_object_key
    del_resp = await api_client.delete(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-del-1"},
    )
    assert del_resp.status_code == 204
    assert not second_disk_path.exists()

    after_del_prefs = await api_client.get("/v1/me/preferences", headers=h1)
    assert after_del_prefs.json()["hasCustomWallpaper"] is False
    assert after_del_prefs.json()["wallpaperUrl"] is None
    version_after_del = after_del_prefs.json()["version"]

    # Idempotent replay of DELETE /v1/me/wallpaper returns 204 without mutating version again
    del_replay = await api_client.delete(
        "/v1/me/wallpaper",
        headers={**h1, "Idempotency-Key": "wp-del-1"},
    )
    assert del_replay.status_code == 204
    after_replay_prefs = await api_client.get("/v1/me/preferences", headers=h1)
    assert after_replay_prefs.json()["version"] == version_after_del

    after_del_wp = await api_client.get("/v1/me/wallpaper", headers=h1)
    assert after_del_wp.status_code == 404
