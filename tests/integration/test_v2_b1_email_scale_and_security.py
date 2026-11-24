"""Comprehensive V2 Lane A-1 (B1) combat verification suite.

Covers:
- T1: Fail-closed staging/prod Settings guardrails (Email OTP, Gemini 3.8 Flash, SMTP, debug=False).
- T2: ResendEmailOtpSender HTTP matrix (User-Agent, Idempotency-Key, 401/403/409/429/503/timeout, HTML escape).
- T3: SmtpEmailOtpSender QQ/Gmail MIME (7-bit ASCII RFC 2047, SSL CERT_REQUIRED, whitespace strip, SMTP codes).
- T4: Live PG15 OTP race, 5-way burst cooldown, failure-safe preservation, purpose-scoped reauth, GHSA-2f9c
      atomic brute-force lock, X-Forwarded-For trusted_proxy_hops & IP rate limit, /request-otp alias.
- T5/T6: Multi-year timeline-buckets (weeklyCounts[4]), happened_on keyset pagination, EXPLAIN covering index,
         encrypted Chinese hybrid search (synonym recall + self-name downweighting + matchedTerms),
         multi-instance crypto-shredding & MAX_TOTAL_CACHED_SLIPS LRU eviction.
- T9/T11: Idempotency 72h TTL / 45s stale / Retry-After: 2; 7-step [换人] cross-person reassignment + HELD
          promotion + fact restoration + dual-person cache eviction; HTTP 206/416 media range; lunar reminder
          nextOccurrenceDate; V2 data_rights export (银盾 · 边界 + media embeds); refcount blob GC & zero-PII.
"""

from __future__ import annotations

import asyncio
import io
import json
import smtplib
import ssl
import zipfile
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update

from mindweave.core.clock import ManualClock
from mindweave.core.config import Settings
from mindweave.core.dependencies import AppContainer
from mindweave.core.errors import AppError, EmailDeliveryError
from mindweave.core.idempotency import IdempotencyService
from mindweave.core.ids import new_id
from mindweave.core.orm import (
    AiCallRow,
    CaptureRow,
    ChatCitationRow,
    ChatMessageRow,
    ChatThreadRow,
    EmailOtpChallengeRow,
    FactRow,
    InboxItemRow,
    RecordRow,
    ReminderRow,
    UserKeyRow,
)
from mindweave.core.storage import filter_unreferenced_blob_keys
from mindweave.main import create_app
from mindweave.modules.identity.domain.models import EmailDeliveryReceipt, OtpPurpose
from mindweave.modules.identity.infra.email_sender import (
    ResendEmailOtpSender,
    SmtpEmailOtpSender,
    UnconfiguredEmailOtpSender,
    build_smtp_otp_message,
    render_otp_content,
)
from mindweave.modules.memory.domain.zh_search import CompactDocMeta
from mindweave.modules.memory.infra.search_cache import (
    PersonSearchIndexCache,
    PersonSearchIndexEntry,
)
from mindweave.shared.enums import (
    CaptureSource,
    CaptureStatus,
    InboxItemKind,
    InboxItemStatus,
    RecordReviewState,
    RouteMode,
)
from mindweave.shared.scopes import UserScope
from mindweave.workers.runner import sanitize_job_error


def _make_jpeg_bytes(color: tuple[int, int, int] = (210, 178, 142)) -> bytes:
    img = Image.new("RGB", (64, 64), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def _auth_via_dev_otp(
    client: AsyncClient,
    email: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[dict[str, str], UUID]:
    req_resp = await client.post(
        "/v1/auth/email/otp:request",
        json={"email": email, "purpose": "login"},
        headers=headers or {},
    )
    assert req_resp.status_code == 200, req_resp.text
    body = req_resp.json()
    verify_resp = await client.post(
        "/v1/auth/email/otp:verify",
        json={
            "challengeId": body["challengeId"],
            "email": email,
            "code": body["devOtpCode"],
        },
        headers=headers or {},
    )
    assert verify_resp.status_code == 200, verify_resp.text
    vbody = verify_resp.json()
    return {"Authorization": f"Bearer {vbody['accessToken']}"}, UUID(vbody["userId"])


async def _create_person(
    client: AsyncClient,
    auth_headers: dict[str, str],
    *,
    display_name: str,
    tier: str = "gold",
    role_tag: str = "",
    traits: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "nameCn": display_name,
        "tier": tier,
        "roleTag": role_tag,
    }
    if traits is not None:
        payload["traits"] = traits
    resp = await client.post(
        "/v1/persons",
        headers={**auth_headers, "Idempotency-Key": str(uuid4())},
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# =============================================================================
# T1: Fail-Closed Staging / Production Settings Guardrails
# =============================================================================


def test_t1_staging_and_prod_settings_fail_closed_matrix() -> None:
    resend_baseline: dict[str, Any] = {
        "environment": "staging",
        "debug": False,
        "kek_hex": "11" * 32,
        "jwt_secret": "s" * 48,
        "ai_primary_provider": "gemini",
        "gemini_api_key": "AIzaSyValidTestKeyForStaging123456",
        "ai_fallback_provider": "none",
        "email_provider": "resend",
        "resend_api_key": "re_test_valid_123456",
        "email_from_address": "noreply@mail.mindweave.app",
        "push_apns_adapter": "disabled",
        "push_web_push_adapter": "disabled",
        "allow_dev_mock_apple_auth": False,
        "expose_dev_otp_in_response": False,
    }
    ok_resend = Settings(**resend_baseline)
    assert ok_resend.gemini_model_fast == "gemini-3.8-flash"
    assert ok_resend.gemini_model_chat == "gemini-3.8-flash"
    assert ok_resend.gemini_model_lite == "gemini-3.5-flash-lite"
    assert ok_resend.undo_window_seconds == 30

    # Valid QQ Mail SMTP SSL 465 baseline
    smtp_baseline = {
        **resend_baseline,
        "email_provider": "smtp",
        "resend_api_key": "",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_use_ssl": True,
        "smtp_use_starttls": False,
        "smtp_username": "12345678@qq.com",
        "smtp_password": "abcd efgh ijkl mnop",
        "email_from_address": "12345678@qq.com",
    }
    ok_smtp = Settings(**smtp_baseline)
    assert ok_smtp.email_provider == "smtp"

    mutations: list[tuple[dict[str, Any], str]] = [
        ({"email_provider": "noop"}, "Email OTP provider"),
        ({"email_from_address": "onboarding@resend.dev"}, "resend.dev"),
        ({"resend_api_key": ""}, "MINDWEAVE_RESEND_API_KEY"),
        ({"ai_primary_provider": "fake"}, "fake AI provider"),
        ({"gemini_api_key": ""}, "Missing API key"),
        ({"debug": True}, "MINDWEAVE_DEBUG"),
        ({"environment": "prod_cn", "debug": True}, "MINDWEAVE_DEBUG"),
        ({"expose_dev_otp_in_response": True}, "Dev auth shortcuts"),
        ({"allow_dev_mock_apple_auth": True}, "Dev auth shortcuts"),
    ]
    for override, expected_match in mutations:
        with pytest.raises(ValidationError, match=expected_match):
            Settings(**{**resend_baseline, **override})

    # SMTP specific fail-closed checks
    with pytest.raises(ValidationError, match="TLS"):
        Settings(**{**smtp_baseline, "smtp_port": 25, "smtp_use_ssl": False, "smtp_use_starttls": False})
    with pytest.raises(ValidationError, match="SMTP From address"):
        Settings(**{**smtp_baseline, "email_from_address": "other@mindweave.app"})


# =============================================================================
# T2: ResendEmailOtpSender HTTP Transport Matrix & HTML Injection Guard
# =============================================================================


@pytest.mark.asyncio
async def test_t2_resend_email_otp_sender_combat_matrix() -> None:
    # HTML injection / non-6-digit rejection (GHSA-v94m-9pp2-pm59)
    with pytest.raises(ValueError, match="6 ASCII digits"):
        render_otp_content(otp_code="12<b>3", purpose=OtpPurpose.LOGIN, expires_in_seconds=600)
    with pytest.raises(ValueError, match="6 ASCII digits"):
        render_otp_content(otp_code="12345", purpose=OtpPurpose.LOGIN, expires_in_seconds=600)

    subj, txt, html_body = render_otp_content(
        otp_code="654321", purpose=OtpPurpose.LOGIN, expires_in_seconds=600
    )
    assert subj == "心络登录验证码：654321"
    assert "654321" in txt and "654321" in html_body
    assert "<script" not in html_body

    # 1. Happy path verifies headers, Idempotency-Key, User-Agent, and receipt
    seen_requests: list[httpx.Request] = []

    def _ok_handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"id": "re_msg_001"})

    cid = new_id()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler)) as http_client:
        sender = ResendEmailOtpSender(
            api_key="re_live_secret_999",
            from_address="noreply@mail.mindweave.app",
            http_client=http_client,
            retry_delay_seconds=0.0,
        )
        receipt = await sender.send_otp(
            email="alice@example.com",
            otp_code="123456",
            purpose=OtpPurpose.LOGIN,
            expires_in_seconds=600,
            challenge_id=cid,
        )
    assert receipt.delivered is True
    assert receipt.provider == "resend"
    assert receipt.provider_message_id == "re_msg_001"
    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.headers["authorization"] == "Bearer re_live_secret_999"
    assert req.headers["idempotency-key"] == f"otp/{cid}"
    assert "MindWeave-Backend" in req.headers["user-agent"]
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["to"] == ["alice@example.com"]
    assert "123456" in payload["subject"]

    # 2. Non-retryable error matrix (exact 1 attempt)
    non_retry_cases: list[tuple[int, dict[str, Any], str, int]] = [
        (403, {"name": "validation_error", "message": "You can only send testing emails"}, "email_sender_sandbox_restricted", 502),
        (403, {"name": "validation_error", "message": "The domain is not verified"}, "email_sender_domain_unverified", 502),
        (401, {"name": "invalid_api_key", "message": "Invalid key"}, "email_provider_auth_failed", 502),
        (409, {"name": "invalid_idempotent_request", "message": "Mismatch"}, "email_provider_auth_failed", 502),
        (429, {"name": "daily_quota_exceeded", "message": "Daily quota reached"}, "email_provider_quota_exceeded", 503),
    ]
    for status_code, body, expected_code, expected_status in non_retry_cases:
        attempts = 0

        def _err_handler(_request: httpx.Request, sc: int = status_code, b: dict[str, Any] = body) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(sc, json=b)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_err_handler)) as hc:
            s = ResendEmailOtpSender(
                api_key="re_key",
                from_address="noreply@mail.mindweave.app",
                http_client=hc,
                retry_delay_seconds=0.0,
            )
            with pytest.raises(EmailDeliveryError) as exc_info:
                await s.send_otp(
                    email="victim@example.com",
                    otp_code="777888",
                    purpose=OtpPurpose.LOGIN,
                    expires_in_seconds=600,
                    challenge_id=new_id(),
                )
        assert attempts == 1
        assert exc_info.value.error_code == expected_code
        assert exc_info.value.status_code == expected_status
        assert "victim@example.com" not in exc_info.value.detail
        assert "777888" not in exc_info.value.detail

    # 3. Retryable error matrix: 409 concurrent_idempotent_requests, 429 rate_limit_exceeded, 503 -> succeeds on 2nd try
    retry_cases = [
        (409, {"name": "concurrent_idempotent_requests", "message": "Concurrent request"}),
        (429, {"name": "rate_limit_exceeded", "message": "Too many requests"}),
        (503, {"name": "internal_server_error", "message": "Temporary error"}),
    ]
    for status_code, body in retry_cases:
        attempts = 0

        def _retry_handler(_request: httpx.Request, sc: int = status_code, b: dict[str, Any] = body) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(sc, json=b)
            return httpx.Response(200, json={"id": f"re_ok_{sc}"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_retry_handler)) as hc:
            s = ResendEmailOtpSender(
                api_key="re_key",
                from_address="noreply@mail.mindweave.app",
                http_client=hc,
                retry_delay_seconds=0.0,
            )
            rec = await s.send_otp(
                email="retry@example.com",
                otp_code="222333",
                purpose=OtpPurpose.REAUTH,
                expires_in_seconds=600,
                challenge_id=new_id(),
            )
        assert attempts == 2
        assert rec.delivered is True
        assert rec.provider_message_id == f"re_ok_{status_code}"

    # 4. Timeout retries 1x then raises email_delivery_timeout (503)
    timeout_attempts = 0

    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal timeout_attempts
        timeout_attempts += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_timeout_handler)) as hc:
        s = ResendEmailOtpSender(
            api_key="re_key",
            from_address="noreply@mail.mindweave.app",
            http_client=hc,
            retry_delay_seconds=0.0,
        )
        with pytest.raises(EmailDeliveryError) as timeout_exc:
            await s.send_otp(
                email="slow@example.com",
                otp_code="999000",
                purpose=OtpPurpose.LOGIN,
                expires_in_seconds=600,
                challenge_id=new_id(),
            )
    assert timeout_attempts == 2
    assert timeout_exc.value.error_code == "email_delivery_timeout"
    assert timeout_exc.value.status_code == 503


# =============================================================================
# T3: SmtpEmailOtpSender QQ/Gmail MIME, TLS, and Error Mapping
# =============================================================================


@pytest.mark.asyncio
async def test_t3_smtp_email_otp_sender_qq_gmail_mime_and_errors() -> None:
    msg = build_smtp_otp_message(
        from_name="心络 MindWeave",
        from_address="12345678@qq.com",
        to_address="target@example.com",
        otp_code="456789",
        purpose=OtpPurpose.LOGIN,
        expires_in_seconds=600,
    )
    raw_bytes = msg.as_bytes()
    header_block = raw_bytes.split(b"\r\n\r\n", 1)[0]
    # RFC 2047 100% 7-bit ASCII headers (zero raw UTF-8 bytes in headers -> QQ Mail 554 safe)
    header_block.decode("ascii")
    assert msg["Date"] is not None
    assert msg["Message-ID"] is not None and "@qq.com>" in msg["Message-ID"]
    assert msg.get_content_type() == "multipart/alternative"

    # Unconfigured sender fails closed
    unconf = UnconfiguredEmailOtpSender()
    with pytest.raises(EmailDeliveryError) as uexc:
        await unconf.send_otp(
            email="a@b.com", otp_code="111222", purpose=OtpPurpose.LOGIN, expires_in_seconds=600
        )
    assert uexc.value.error_code == "email_provider_not_configured"
    assert uexc.value.status_code == 503

    # QQ From mismatch fails before opening socket
    bad_qq = SmtpEmailOtpSender(
        host="smtp.qq.com",
        port=465,
        username="12345678@qq.com",
        password="abcd efgh ijkl mnop",
        from_address="noreply@other.com",
        use_ssl=True,
    )
    with pytest.raises(EmailDeliveryError) as mismatch_exc:
        await bad_qq.send_otp(
            email="u@example.com", otp_code="111222", purpose=OtpPurpose.LOGIN, expires_in_seconds=600
        )
    assert mismatch_exc.value.error_code == "email_smtp_from_mismatch"
    assert mismatch_exc.value.status_code == 502

    # Happy path SSL 465 verifies CERT_REQUIRED, check_hostname, and whitespace-stripped App Password
    good_qq = SmtpEmailOtpSender(
        host="smtp.qq.com",
        port=465,
        username="12345678@qq.com",
        password="abcd efgh ijkl mnop",
        from_address="12345678@qq.com",
        use_ssl=True,
    )
    ctx = good_qq._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    with patch("smtplib.SMTP_SSL", return_value=fake_client) as mock_ssl:
        rec = await good_qq.send_otp(
            email="friend@example.com",
            otp_code="888999",
            purpose=OtpPurpose.LOGIN,
            expires_in_seconds=600,
            challenge_id=new_id(),
        )
    assert rec.delivered is True
    assert rec.provider == "smtp"
    assert mock_ssl.call_args.kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    fake_client.login.assert_called_once_with("12345678@qq.com", "abcdefghijklmnop")
    fake_client.send_message.assert_called_once()

    # Error classification matrix
    assert SmtpEmailOtpSender._classify_smtp_error(
        smtplib.SMTPAuthenticationError(535, b"5.7.8 Auth failed")
    ).error_code == "email_provider_auth_failed"
    assert SmtpEmailOtpSender._classify_smtp_error(
        smtplib.SMTPSenderRefused(501, b"Mail from address must be same as authorization user", "x@y.com")
    ).error_code == "email_smtp_from_mismatch"
    assert SmtpEmailOtpSender._classify_smtp_error(
        smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"No such user")})
    ).status_code == 422
    assert SmtpEmailOtpSender._classify_smtp_error(
        smtplib.SMTPRecipientsRefused({"bad@example.com": (554, b"Spam 0/92 rejected")})
    ).status_code == 502
    assert SmtpEmailOtpSender._classify_smtp_error(
        smtplib.SMTPResponseException(421, b"4.7.0 frequency limit exceeded")
    ).error_code == "email_provider_quota_exceeded"
    assert SmtpEmailOtpSender._classify_smtp_error(TimeoutError("timeout")).error_code == "email_delivery_timeout"


# =============================================================================
# T4: Live PG15 OTP Race, Cooldown, Failure Preservation, GHSA-2f9c, Proxy IP
# =============================================================================


class _FlakyCaptureOtpSender:
    """Test double toggling between success and EmailDeliveryError."""

    def __init__(self) -> None:
        self.should_fail = False
        self.sent: list[tuple[str, str, OtpPurpose]] = []

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: UUID | None = None,
    ) -> EmailDeliveryReceipt:
        del expires_in_seconds
        if self.should_fail:
            raise EmailDeliveryError(
                "Upstream mail provider temporarily unavailable.",
                error_code="email_delivery_timeout",
                status_code=503,
            )
        self.sent.append((email, otp_code, purpose))
        return EmailDeliveryReceipt(
            delivered=True,
            provider="resend",
            delivery_status="delivered",
            provider_message_id=f"mock-{challenge_id}",
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_t4_live_pg_otp_race_cooldown_reauth_proxy_ip_and_alias(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    strict_settings = db_container.settings.model_copy(
        update={
            "otp_resend_cooldown_seconds": 60,
            "trusted_proxy_hops": 1,
            "otp_ip_rate_limit_per_minute": 10,
            "expose_dev_otp_in_response": False,
        }
    )
    container = AppContainer.build(strict_settings, clock=manual_clock)
    flaky = _FlakyCaptureOtpSender()
    container.extras["otp_sender"] = flaky
    app = create_app(container=container)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. 5-way concurrent burst for same email -> exactly 1x 200, 4x 429 + Retry-After
            burst = await asyncio.gather(
                *[
                    client.post(
                        "/v1/auth/email/otp:request",
                        json={"email": "burst@example.com", "purpose": "login"},
                        headers={"X-Forwarded-For": "198.51.100.11"},
                    )
                    for _ in range(5)
                ]
            )
            statuses = sorted(r.status_code for r in burst)
            assert statuses == [200, 429, 429, 429, 429], statuses
            ok_resp = next(r for r in burst if r.status_code == 200)
            ok_body = ok_resp.json()
            assert ok_body["delivered"] is True
            assert ok_body["provider"] == "resend"
            assert "devOtpCode" not in ok_body
            first_cid = ok_body["challengeId"]
            first_code = flaky.sent[0][1]

            for r in burst:
                if r.status_code == 429:
                    assert r.json()["code"] == "otp_cooldown_active"
                    assert int(r.headers["retry-after"]) >= 1

            # 2. Purpose-scoped cooldown: login cooldown does NOT block immediate reauth OTP!
            reauth_resp = await client.post(
                "/v1/auth/email/request-otp",  # also verifies alias route!
                json={"email": "burst@example.com", "purpose": "reauth"},
                headers={"X-Forwarded-For": "198.51.100.12"},
            )
            assert reauth_resp.status_code == 200, reauth_resp.text
            assert reauth_resp.json()["delivered"] is True

            # 3. Advance clock past cooldown, fail resend -> prior valid OTP MUST remain verifiable!
            manual_clock.advance(seconds=65)
            flaky.should_fail = True
            fail_resend = await client.post(
                "/v1/auth/email/otp:request",
                json={"email": "burst@example.com", "purpose": "login"},
                headers={"X-Forwarded-For": "198.51.100.13"},
            )
            assert fail_resend.status_code == 503
            assert "burst@example.com" not in fail_resend.text
            assert first_code not in fail_resend.text

            # Verify failed row persisted with delivery_status='failed'
            async with container.db.system_session() as sys_sess:
                failed_count = await sys_sess.scalar(
                    select(func.count(EmailOtpChallengeRow.id)).where(
                        EmailOtpChallengeRow.delivery_status == "failed"
                    )
                )
                assert int(failed_count or 0) == 1

            # 4. Concurrent valid verify (GHSA-2f9c atomic consume): exactly 1x 200, rest 401!
            verify_race = await asyncio.gather(
                *[
                    client.post(
                        "/v1/auth/email/otp:verify",
                        json={
                            "challengeId": first_cid,
                            "email": "burst@example.com",
                            "code": first_code,
                        },
                    )
                    for _ in range(5)
                ]
            )
            assert sorted(r.status_code for r in verify_race) == [200, 401, 401, 401, 401]

            # 5. Concurrent wrong-code brute force (GHSA-2f9c atomic attempt increment)
            flaky.should_fail = False
            req2 = await client.post(
                "/v1/auth/email/otp:request",
                json={"email": "brute@example.com", "purpose": "login"},
                headers={"X-Forwarded-For": "198.51.100.14"},
            )
            assert req2.status_code == 200
            cid2 = req2.json()["challengeId"]
            real_code2 = flaky.sent[-1][1]
            wrong_code = "000000" if real_code2 != "000000" else "999999"

            wrong_race = await asyncio.gather(
                *[
                    client.post(
                        "/v1/auth/email/otp:verify",
                        json={"challengeId": cid2, "email": "brute@example.com", "code": wrong_code},
                    )
                    for _ in range(6)
                ]
            )
            assert all(r.status_code in (401, 429) for r in wrong_race)
            # Even the REAL code is now locked out!
            locked_resp = await client.post(
                "/v1/auth/email/otp:verify",
                json={"challengeId": cid2, "email": "brute@example.com", "code": real_code2},
            )
            assert locked_resp.status_code == 401
            assert locked_resp.json()["code"] == "otp_max_attempts_exceeded"

            # 6. trusted_proxy_hops=1 resists spoofed leftmost X-Forwarded-For & enforces 10/min per real IP
            ip_statuses: list[int] = []
            for idx in range(11):
                r = await client.post(
                    "/v1/auth/email/otp:request",
                    json={"email": f"ipbomb_{idx}@example.com", "purpose": "login"},
                    headers={"X-Forwarded-For": f"10.0.0.{idx + 1}, 203.0.113.77"},
                )
                ip_statuses.append(r.status_code)
            assert ip_statuses[:10] == [200] * 10
            assert ip_statuses[10] == 429
    finally:
        await container.db.dispose()


# =============================================================================
# T5 / T6: Multi-Year Timeline Buckets, Keyset Pagination, Encrypted ZH Search & Crypto-Shred
# =============================================================================


@pytest.mark.asyncio
async def test_t5_t6_multiyear_timeline_buckets_keyset_pagination_zh_search_and_crypto_shred(
    db_container: AppContainer,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _auth_via_dev_otp(client, "journal_scale@example.com")
        mom = await _create_person(client, headers, display_name="妈妈", tier="gold", role_tag="家人")
        boss = await _create_person(client, headers, display_name="张总监", tier="silver", role_tag="职场")
        mom_id = mom["id"]
        boss_id = boss["id"]

        # Silver tier rejects photo creation with canonical V2 boundary copy
        silver_photo = await client.post(
            f"/v1/persons/{boss_id}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            data={"kind": "photo", "title": "合影", "body": "团建"},
            files={"photo": ("p.jpg", _make_jpeg_bytes(), "image/jpeg")},
        )
        assert silver_photo.status_code == 422
        assert "银盾边界页默认不保存照片" in silver_photo.text

        # Seed 26 multi-year slips (2024-2026) for Mom, including our semantic needle!
        slips_spec = [
            ("2024-12-03", "冬日手札", "妈妈说膝盖一降温就发凉，想要一副浅灰羊绒护膝保暖", True),
            ("2024-12-18", "日常碎片", "妈妈今天包了韭菜鸡蛋饺子，嘱咐我多吃点", False),
            ("2025-01-05", "新年电话", "妈妈说家里阳台绿萝长新芽了", False),
            ("2025-06-14", "夏季提醒", "妈妈血压最近很稳，晚上散步四十分钟", False),
            ("2025-11-02", "秋末备忘", "妈妈提醒立冬记得添衣服，别着凉", False),
            ("2025-11-09", "周末通话", "妈妈买了新的陶瓷炖锅煲莲藕排骨汤", False),
            ("2025-11-18", "体检记录", "妈妈今年体检血糖指标全部正常", True),
            ("2025-11-26", "感恩节", "妈妈晒了好多红薯干寄过来", False),
            ("2026-03-11", "春日随笔", "妈妈和阿姨去植物园看早樱了", False),
            ("2026-10-08", "重阳节", "陪妈妈去老字号买了低糖桂花糕", True),
        ]
        # Add 16 more filler slips across 2025-11 to thoroughly test pagination & weeklyCounts
        for day_i in range(1, 17):
            slips_spec.append(
                (f"2025-10-{day_i:02d}", f"日常记录{day_i}", f"妈妈随口聊起社区团购第{day_i}期苹果很甜", False)
            )

        needle_id: str | None = None
        for happened, title, body, pinned in slips_spec:
            r = await client.post(
                f"/v1/persons/{mom_id}/materials",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={
                    "kind": "note",
                    "title": title,
                    "body": body,
                    "happenedOn": happened,
                    "pinned": pinned,
                },
            )
            assert r.status_code == 201, r.text
            if "羊绒护膝" in body:
                needle_id = r.json()["id"]
        assert needle_id is not None

        # 1. Timeline buckets + weeklyCounts[4] + year summary
        buckets_resp = await client.get(f"/v1/persons/{mom_id}/timeline-buckets", headers=headers)
        assert buckets_resp.status_code == 200, buckets_resp.text
        bdata = buckets_resp.json()
        assert bdata["totalCount"] == len(slips_spec)
        assert [y["year"] for y in bdata["years"]] == [2026, 2025, 2024]
        for bucket in bdata["buckets"]:
            wc = bucket["weeklyCounts"]
            assert len(wc) == 4
            assert sum(wc) == bucket["count"]
        nov25 = next(b for b in bdata["buckets"] if b["bucket"] == "2025-11")
        assert nov25["count"] == 4
        assert nov25["weeklyCounts"] == [1, 1, 1, 1]

        # 2. Keyset pagination by happened_desc drains all 26 records with 0 dupes & 0 drops
        collected_ids: list[str] = []
        collected_dates: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 7, "sort": "happened"}
            if cursor:
                params["cursor"] = cursor
            page = await client.get(f"/v1/persons/{mom_id}/materials", params=params, headers=headers)
            assert page.status_code == 200, page.text
            pdata = page.json()
            for item in pdata["items"]:
                collected_ids.append(item["id"])
                collected_dates.append(item["happenedOn"])
            if not pdata["hasMore"]:
                break
            cursor = pdata["nextCursor"]
        assert len(collected_ids) == len(slips_spec)
        assert len(set(collected_ids)) == len(slips_spec)
        assert collected_dates == sorted(collected_dates, reverse=True)

        # Month & pinned filters
        nov_page = await client.get(
            f"/v1/persons/{mom_id}/materials",
            params={"month": "2025-11", "pinned": "true"},
            headers=headers,
        )
        assert nov_page.status_code == 200
        assert len(nov_page.json()["items"]) == 1
        assert nov_page.json()["items"][0]["happenedOn"] == "2025-11-18"

        # 3. Verify covering partial index exists in Postgres catalog
        async with db_container.db.system_session() as sys_sess:
            idx_def = await sys_sess.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'records' AND indexname = 'ix_records_user_person_happened_active'"
                )
            )
            assert idx_def is not None and "happened_on DESC" in idx_def and "INCLUDE" in idx_def

        # 4. Encrypted Chinese hybrid search: synonym recall + self-name downweighting + matchedTerms
        search_resp = await client.get(
            f"/v1/persons/{mom_id}/search",
            params={"q": "妈妈冬天怕冷"},
            headers=headers,
        )
        assert search_resp.status_code == 200, search_resp.text
        sdata = search_resp.json()
        assert len(sdata["items"]) >= 1
        top_hit = sdata["items"][0]
        assert top_hit["material"]["id"] == needle_id
        assert len(top_hit["matchedTerms"]) >= 1
        assert "护膝" in top_hit["highlightSnippet"] or "发凉" in top_hit["highlightSnippet"]

        # Temporal intent extraction
        temp_resp = await client.get(
            f"/v1/persons/{mom_id}/search",
            params={"q": "2024年冬天怕冷"},
            headers=headers,
        )
        assert temp_resp.status_code == 200
        assert temp_resp.json()["inferredDateRange"] is not None
        assert temp_resp.json()["inferredDateRange"]["from"] == "2024-11-01"

        # 5. Cache eviction hook on clear_cache() (handles None even if DEK already expired!)
        cache = db_container.extras["search_index_cache"]
        assert isinstance(cache, PersonSearchIndexCache)
        assert cache.entry_count >= 1
        db_container.cipher.clear_cache()
        assert cache.entry_count == 0
        assert cache.total_slips == 0

        # 6. Multi-instance crypto-shredding: if DEK is deleted in DB while cache is warm, search MUST fail closed!
        warm = await client.get(f"/v1/persons/{mom_id}/search", params={"q": "护膝"}, headers=headers)
        assert warm.status_code == 200 and cache.entry_count >= 1
        async with db_container.db.user_session(UserScope(user_id)) as sess:
            await sess.execute(delete(UserKeyRow).where(UserKeyRow.user_id == user_id))
        # Simulate another instance where cipher DEK cache didn't have the key, while search cache is warm:
        db_container.cipher._cache.pop(user_id, None)
        shred_resp = await client.get(
            f"/v1/persons/{mom_id}/search", params={"q": "护膝"}, headers=headers
        )
        assert shred_resp.status_code in (401, 404, 410, 500)
        assert shred_resp.status_code != 200

    # 7. Unit verification of MAX_TOTAL_CACHED_SLIPS LRU cap
    small_cache = PersonSearchIndexCache(max_total_slips=5)
    u = new_id()
    for _ in range(3):
        pid = new_id()
        docs = [
            CompactDocMeta(
                record_id=new_id(),
                kind="note",
                happened_on=date(2026, 1, 1),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                pinned=False,
                review_state="normal",
                doc_len=1,
            )
            for _ in range(3)
        ]
        small_cache.put(
            user_id=u,
            person_id=pid,
            include_held=False,
            entry=PersonSearchIndexEntry(
                user_id=u,
                person_id=pid,
                watermark=(3, None),
                expires_at=9999999999.0,
                vocab={},
                inv_postings={},
                docs=docs,
                avgdl=1.0,
            ),
        )
    assert small_cache.total_slips <= 5
    assert small_cache.entry_count == 1


# =============================================================================
# T9 / T11: Idempotency, 7-Step [换人], 206 Range, Lunar Reminder, V2 Export, GC
# =============================================================================


@pytest.mark.asyncio
async def test_t9_t11_idempotency_7step_reassign_206_range_reminders_export_and_gc(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _auth_via_dev_otp(client, "v2_combat_full@example.com")
        scope = UserScope(user_id)

        # 0. Runtime status meta endpoint
        meta = await client.get("/v1/meta/runtime-status")
        assert meta.status_code == 200
        mbody = meta.json()
        assert mbody["emailProvider"] == "noop"
        assert mbody["emailDeliveryReady"] is False
        assert mbody["nativeSpeechSupported"] is True
        assert mbody["databaseMode"] in ("external", "embedded")

        # 1. T9 Idempotency: 72h TTL, Retry-After: 2 on active in_progress, 45s stale reclaim
        idem = IdempotencyService(clock=manual_clock, ttl_hours=72, stale_in_progress_seconds=45)
        async with db_container.db.user_session(scope) as sess:
            row1, hit1 = await idem.claim_or_replay(
                sess, scope=scope, idem_key="k-stale-1", method="POST", path="/v1/test", request_hash="abc"
            )
            assert row1 is not None and hit1 is None
            assert (row1.expires_at - manual_clock.now()).total_seconds() == pytest.approx(72 * 3600, abs=5)
        # Immediately contending while in_progress (<45s) raises 409 with Retry-After: 2
        async with db_container.db.user_session(scope) as sess:
            with pytest.raises(Exception) as exc_info:
                await idem.claim_or_replay(
                    sess,
                    scope=scope,
                    idem_key="k-stale-1",
                    method="POST",
                    path="/v1/test",
                    request_hash="abc",
                    coalesce_wait_seconds=0.0,
                )
            assert getattr(exc_info.value, "error_code", "") == "idempotency_in_progress"
            assert getattr(exc_info.value, "headers", {}).get("Retry-After") == "2"
        # Advance 46s -> stale row reclaimed cleanly!
        manual_clock.advance(seconds=46)
        async with db_container.db.user_session(scope) as sess:
            reclaimed, hit2 = await idem.claim_or_replay(
                sess,
                scope=scope,
                idem_key="k-stale-1",
                method="POST",
                path="/v1/test",
                request_hash="abc",
                coalesce_wait_seconds=0.0,
            )
            assert reclaimed is not None and hit2 is None

        # 2. Create Gold (Mom), Crystal (Colleague), Silver (Boss) with traits for distinguishingCue
        mom = await _create_person(
            client, headers, display_name="妈妈", tier="gold", role_tag="家人", traits=["爱喝普洱茶"]
        )
        colleague = await _create_person(
            client, headers, display_name="林同事", tier="crystal", role_tag="研发", traits=["产品部三楼"]
        )
        boss = await _create_person(
            client, headers, display_name="张主管", tier="silver", role_tag="职场"
        )
        mom_id, col_id, boss_id = UUID(mom["id"]), UUID(colleague["id"]), UUID(boss["id"])

        # Upload Gold photo & verify RFC 9110 HTTP 206 Partial Content / 416 Range!
        photo_bytes = _make_jpeg_bytes((190, 150, 120))
        photo_resp = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            data={"kind": "photo", "title": "秋日银杏", "body": "公园散步合影"},
            files={"photo": ("autumn.jpg", photo_bytes, "image/jpeg")},
        )
        assert photo_resp.status_code == 201, photo_resp.text
        photo_dto = photo_resp.json()
        media_url = photo_dto["mediaUrl"]
        assert media_url is not None

        full_media = await client.get(media_url)
        assert full_media.status_code == 200
        assert full_media.headers.get("accept-ranges") == "bytes"
        assert full_media.headers.get("x-content-type-options") == "nosniff"
        total_len = len(full_media.content)
        assert total_len > 32

        r_probe = await client.get(media_url, headers={"Range": "bytes=0-1"})
        assert r_probe.status_code == 206
        assert r_probe.headers.get("x-content-type-options") == "nosniff"
        assert r_probe.headers["content-range"] == f"bytes 0-1/{total_len}"
        assert len(r_probe.content) == 2

        r_suffix = await client.get(media_url, headers={"Range": "bytes=-16"})
        assert r_suffix.status_code == 206
        assert len(r_suffix.content) == 16
        assert r_suffix.content == full_media.content[-16:]

        r_oob = await client.get(media_url, headers={"Range": f"bytes={total_len + 50}-{total_len + 100}"})
        assert r_oob.status_code == 416
        assert r_oob.headers["content-range"] == f"bytes */{total_len}"

        # Reject reassigning Gold photo -> Silver person (422)
        bad_move = await client.patch(
            f"/v1/materials/{photo_dto['id']}",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": f'"{photo_dto["version"]}"',
            },
            json={"personId": str(boss_id)},
        )
        assert bad_move.status_code == 422
        assert "银盾边界页默认不保存照片" in bad_move.text

        # 3. 7-Step [换人] cross-person reassignment with fact supersession restore + HELD -> NORMAL + citation purge + inbox cue
        old_fact_resp = await client.post(
            f"/v1/persons/{mom_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"type": "preference", "polarity": "positive", "content": "喜欢喝铁观音"},
        )
        assert old_fact_resp.status_code == 201
        old_fact_id = UUID(old_fact_resp.json()["id"])

        slip_resp = await client.post(
            f"/v1/persons/{mom_id}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"kind": "note", "title": "咖啡偏好", "body": "记错了其实是林同事喜欢冰美式无糖"},
        )
        assert slip_resp.status_code == 201
        slip_id = UUID(slip_resp.json()["id"])
        slip_ver = slip_resp.json()["version"]

        # Create a fact on Mom backed by `slip_id` that explicitly supersedes `old_fact_id`, plus a DATE fact with reminder
        new_fact_resp = await client.post(
            f"/v1/persons/{mom_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "preference",
                "polarity": "positive",
                "content": "喜欢喝冰美式无糖",
                "recordId": str(slip_id),
                "supersedesFactId": str(old_fact_id),
            },
        )
        assert new_fact_resp.status_code == 201
        new_fact_id = UUID(new_fact_resp.json()["id"])

        date_fact_resp = await client.post(
            f"/v1/persons/{mom_id}/facts",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "type": "date",
                "polarity": "neutral",
                "content": "项目上线复盘会",
                "recordId": str(slip_id),
                "triggerDate": "2026-12-20",
            },
        )
        assert date_fact_resp.status_code == 201
        date_fact_id = UUID(date_fact_resp.json()["id"])

        # Seed a HELD state + PERSON_DISAMBIGUATION inbox item + ChatCitationRow pointing at `slip_id`
        async with db_container.db.user_session(scope) as sess:
            dek = await db_container.load_user_dek(sess, scope)
            await sess.execute(
                update(RecordRow)
                .where(RecordRow.id == slip_id)
                .values(review_state=RecordReviewState.HELD.value)
            )
            inbox_id = new_id()
            sess.add(
                InboxItemRow(
                    id=inbox_id,
                    user_id=user_id,
                    kind=InboxItemKind.PERSON_DISAMBIGUATION.value,
                    status=InboxItemStatus.OPEN.value,
                    person_id=mom_id,
                    record_id=slip_id,
                    fact_ids=[new_fact_id],
                    candidate_person_ids=[mom_id, col_id],
                    payload_enc=db_container.cipher.encrypt_json(
                        dek,
                        {"summary": "需要确认是谁喜欢冰美式", "mention": "林"},
                        user_id=user_id,
                        table="inbox_items",
                        column="payload_enc",
                        row_id=inbox_id,
                    ),
                )
            )
            thread_id = new_id()
            sess.add(ChatThreadRow(id=thread_id, user_id=user_id, person_id=mom_id))
            await sess.flush()
            msg_id = new_id()
            sess.add(
                ChatMessageRow(
                    id=msg_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    person_id=mom_id,
                    role="assistant",
                    tier_snapshot="gold",
                    content_enc=db_container.cipher.encrypt_str(
                        dek,
                        "引用测试",
                        user_id=user_id,
                        table="chat_messages",
                        column="content_enc",
                        row_id=msg_id,
                    ),
                )
            )
            await sess.flush()
            sess.add(
                ChatCitationRow(
                    id=new_id(),
                    user_id=user_id,
                    message_id=msg_id,
                    record_id=slip_id,
                    citation_index=1,
                )
            )

        # Verify inbox hydration exposes distinguishingCue on candidates!
        inbox_list = await client.get("/v1/inbox", headers=headers)
        assert inbox_list.status_code == 200
        inbox_item = next(i for i in inbox_list.json()["items"] if i["id"] == str(inbox_id))
        cues = {c["displayName"]: c["distinguishingCue"] for c in inbox_item["candidatePersons"]}
        assert cues["妈妈"] is not None and "爱喝普洱茶" in cues["妈妈"]
        assert cues["林同事"] is not None and "产品部三楼" in cues["林同事"]

        # Warm search caches on BOTH Mom and Colleague before reassignment
        assert (await client.get(f"/v1/persons/{mom_id}/search", params={"q": "冰美式", "includeHeld": "true"}, headers=headers)).json()["items"]
        assert not (await client.get(f"/v1/persons/{col_id}/search", params={"q": "冰美式"}, headers=headers)).json()["items"]

        # Execute 7-Step [换人] Mom -> Colleague!
        reassign = await client.patch(
            f"/v1/materials/{slip_id}",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": f'"{slip_ver}"',
            },
            json={"personId": str(col_id)},
        )
        assert reassign.status_code == 200, reassign.text
        re_body = reassign.json()
        assert re_body["personId"] == str(col_id)
        assert re_body["reviewState"] == "normal"  # HELD automatically promoted!

        # Verify ALL 7 steps atomically succeeded in Postgres!
        async with db_container.db.user_session(scope) as sess:
            restored_old = await sess.get(FactRow, old_fact_id)
            assert restored_old is not None
            assert restored_old.invalid_at is None
            assert restored_old.superseded_by_id is None

            moved_fact = await sess.get(FactRow, new_fact_id)
            assert moved_fact is not None and moved_fact.person_id == col_id

            moved_rem = await sess.scalar(
                select(ReminderRow).where(ReminderRow.fact_id == date_fact_id)
            )
            assert moved_rem is not None and moved_rem.person_id == col_id

            cit_left = await sess.scalar(
                select(func.count(ChatCitationRow.id)).where(ChatCitationRow.record_id == slip_id)
            )
            assert int(cit_left or 0) == 0

            resolved_inbox = await sess.get(InboxItemRow, inbox_id)
            assert resolved_inbox is not None and resolved_inbox.status == InboxItemStatus.RESOLVED.value

        # Verify BOTH Mom & Colleague search caches were evicted immediately!
        assert not (
            await client.get(f"/v1/persons/{mom_id}/search", params={"q": "冰美式", "includeHeld": "true"}, headers=headers)
        ).json()["items"]
        col_hits = (
            await client.get(f"/v1/persons/{col_id}/search", params={"q": "冰美式"}, headers=headers)
        ).json()["items"]
        assert len(col_hits) == 1 and col_hits[0]["material"]["id"] == str(slip_id)

        # 4. Lunar yearly reminder nextOccurrenceDate (both active AND paused where next_fire_at=None!)
        lunar_rem = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": str(mom_id),
                "title": "妈妈农历生日",
                "rule": "yearly",
                "isLunar": True,
                "targetMonth": 8,
                "targetDay": 15,
                "status": "active",
            },
        )
        assert lunar_rem.status_code == 201, lunar_rem.text
        lbody = lunar_rem.json()
        assert lbody["nextOccurrenceDate"] is not None
        date.fromisoformat(lbody["nextOccurrenceDate"])

        paused_lunar = await client.patch(
            f"/v1/reminders/{lbody['id']}",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": f'"{lbody["version"]}"',
            },
            json={"status": "paused"},
        )
        assert paused_lunar.status_code == 200, paused_lunar.text
        pbody = paused_lunar.json()
        assert pbody["nextOccurrenceDate"] is not None
        date.fromisoformat(pbody["nextOccurrenceDate"])  # valid ISO Gregorian date even when paused!

        # Also create an active solar yearly reminder to test Chinese status label in export
        active_rem = await client.post(
            "/v1/reminders",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "personId": str(mom_id),
                "title": "妈妈体检复查",
                "rule": "yearly",
                "isLunar": False,
                "targetMonth": 11,
                "targetDay": 20,
                "status": "active",
            },
        )
        assert active_rem.status_code == 201

        # 5. V2 data_rights Export: Silver/Crystal headings, Chinese reminder statuses, media embeds!
        reauth_req = await client.post(
            "/v1/auth/email/otp:request",
            json={"email": "v2_combat_full@example.com", "purpose": "reauth"},
        )
        assert reauth_req.status_code == 200
        reauth_ver = await client.post(
            "/v1/auth/email/otp:verify",
            headers=headers,
            json={
                "challengeId": reauth_req.json()["challengeId"],
                "email": "v2_combat_full@example.com",
                "code": reauth_req.json()["devOtpCode"],
            },
        )
        assert reauth_ver.status_code == 200
        reauth_tok = reauth_ver.json()["reauthToken"]

        exp_resp = await client.post(
            "/v1/me/exports",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "X-Reauth-Token": reauth_tok,
            },
            json={"asyncMode": False},
        )
        assert exp_resp.status_code == 201, exp_resp.text
        exp_body = exp_resp.json()
        dl_resp = await client.get(
            f"/v1/me/exports/{exp_body['exportId']}/download",
            params={"token": exp_body["downloadToken"]},
            headers=headers,
        )
        assert dl_resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(dl_resp.content), "r") as zf:
            journal_md = zf.read("journal.md").decode("utf-8")
            assert "## 金箭 · 珍重" in journal_md
            assert "## 银盾 · 边界" in journal_md
            assert "## 水晶 · 日常" in journal_md
            assert "记爱" not in journal_md
            assert "银箭" not in journal_md and "记恨" not in journal_md
            assert "水晶箭" not in journal_md and "记繁琐" not in journal_md
            assert "（每年 11-20 · 提醒中）" in journal_md
            assert "（每年 农历八月十五 · 已暂停）" in journal_md
            assert "· active" not in journal_md
            assert "![秋日银杏](media/" in journal_md

        # 6. Refcount-safe blob GC (preserves shared audio referenced by sibling record / capture)
        shared_key = f"{user_id}/captures/shared-audio.m4a"
        orphan_key = f"{user_id}/captures/orphan-audio.m4a"
        async with db_container.db.user_session(scope) as sess:
            dek = await db_container.load_user_dek(sess, scope)
            cap_id = new_id()
            sess.add(
                CaptureRow(
                    id=cap_id,
                    user_id=user_id,
                    source=CaptureSource.VOICE.value,
                    route_mode=RouteMode.GLOBAL.value,
                    status=CaptureStatus.COMPLETED.value,
                    audio_object_key=shared_key,
                    idempotency_key=f"gc-{cap_id}",
                )
            )
            await sess.flush()
            rec_alive = new_id()
            sess.add(
                RecordRow(
                    id=rec_alive,
                    user_id=user_id,
                    person_id=mom_id,
                    capture_id=cap_id,
                    kind="note",
                    title_enc=db_container.cipher.encrypt_str(
                        dek, "t", user_id=user_id, table="records", column="title_enc", row_id=rec_alive
                    ),
                    body_enc=db_container.cipher.encrypt_str(
                        dek, "b", user_id=user_id, table="records", column="body_enc", row_id=rec_alive
                    ),
                    snippet_enc=db_container.cipher.encrypt_str(
                        dek, "s", user_id=user_id, table="records", column="snippet_enc", row_id=rec_alive
                    ),
                    media_object_key=shared_key,
                    happened_on=date(2026, 11, 20),
                )
            )
            await sess.flush()
            safe_to_delete = await filter_unreferenced_blob_keys(
                sess, candidate_keys={shared_key, orphan_key}
            )
        assert shared_key not in safe_to_delete
        assert orphan_key in safe_to_delete

        # 7. Worker sanitize_job_error strips PII / raw secrets / raw str(exc)
        sanitized = sanitize_job_error(
            RuntimeError("Failed sending to secret@example.com with key re_live_1234567890abcdef and otp 654321")
        )
        assert sanitized == "RuntimeError: internal_error"
        assert "secret@example.com" not in sanitized
        assert "re_live_" not in sanitized
        assert "654321" not in sanitized


@pytest.mark.asyncio
async def test_s10_global_cny_killswitch_blocks_cross_tenant_admissions(
    db_container: AppContainer,
    manual_clock: ManualClock,
) -> None:
    """S10.2: User A burning ¥200 global AI budget must pause User B with 503 ai_budget_paused + Retry-After: 300."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _headers_a, user_a = await _auth_via_dev_otp(client, "whale_a@example.com")
        headers_b, user_b = await _auth_via_dev_otp(client, "victim_b@example.com")
        person_b = await _create_person(client, headers_b, display_name="好友小周", tier="gold")
        scope_a = UserScope(user_a)
        scope_b = UserScope(user_b)

        # Before global spend limit: User B passes capture, voice, and chat admission checks
        async with db_container.db.user_session(scope_b) as sess_b:
            await db_container.quotas.check_capture_quota(sess_b, scope_b, source="text")
            await db_container.quotas.check_voice_quota(sess_b, scope_b, additional_ms=5000)
            await db_container.quotas.check_chat_quota(sess_b, scope_b)

        # Seed User A burning ¥200 (200_000_000 micros CNY) today
        async with db_container.db.user_session(scope_a) as sess_a:
            sess_a.add(
                AiCallRow(
                    id=new_id(),
                    user_id=user_a,
                    task="companion_chat",
                    provider="gemini",
                    model="gemini-3.8-flash",
                    input_tokens=50000,
                    output_tokens=10000,
                    latency_ms=420,
                    cost_micros_cny=200_000_000,
                    outcome="success",
                    created_at=manual_clock.now(),
                )
            )

        # Immediately after: User B is blocked across capture, voice, AND chat admissions with 503 + Retry-After: 300!
        async with db_container.db.user_session(scope_b) as sess_b:
            for check_coro in (
                db_container.quotas.check_capture_quota(sess_b, scope_b, source="text"),
                db_container.quotas.check_voice_quota(sess_b, scope_b, additional_ms=5000),
                db_container.quotas.check_chat_quota(sess_b, scope_b),
            ):
                with pytest.raises(AppError) as exc_info:
                    await check_coro
                assert exc_info.value.status_code == 503
                assert exc_info.value.error_code == "ai_budget_paused"
                assert (exc_info.value.headers or {}).get("Retry-After") == "300"

        # Live HTTP check: User B hitting POST /v1/persons/{id}/chat/messages returns HTTP 503 + Retry-After: 300!
        chat_resp = await client.post(
            f"/v1/persons/{person_b['id']}/chat/messages",
            headers={**headers_b, "Idempotency-Key": str(uuid4())},
            json={"message": "最近该怎么关心她？"},
        )
        assert chat_resp.status_code == 503, chat_resp.text
        assert chat_resp.json()["code"] == "ai_budget_paused"
        assert chat_resp.headers.get("retry-after") == "300"


@pytest.mark.asyncio
async def test_s08_silver_demotion_export_cloaks_photos_but_keeps_voice(
    db_container: AppContainer,
) -> None:
    """S08.1: Demoting Gold->Silver must exclude all Silver photos/avatars from export ZIP while keeping voice audio."""
    app = create_app(container=db_container)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, user_id = await _auth_via_dev_otp(client, "silver_export_cloak@example.com")
        scope = UserScope(user_id)

        # 1. Create Gold person with a real uploaded photo + a voice record blob (.m4a)
        ex = await _create_person(client, headers, display_name="前任林某", tier="gold", role_tag="往事")
        ex_id = UUID(ex["id"])

        photo_resp = await client.post(
            f"/v1/persons/{ex_id}/materials",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            data={"kind": "photo", "title": "旧合影", "body": "去年旅行照片"},
            files={"photo": ("ex_photo.jpg", _make_jpeg_bytes((120, 140, 180)), "image/jpeg")},
        )
        assert photo_resp.status_code == 201, photo_resp.text

        # Seed a voice RecordRow with real encrypted .m4a blob on the same person
        voice_key = f"{user_id}/records/silver_voice_evidence.m4a"
        async with db_container.db.user_session(scope) as sess:
            dek = await db_container.load_user_dek(sess, scope)
            await db_container.storage.put_encrypted(
                user_id=user_id,
                dek=dek,
                object_key=voice_key,
                plaintext=b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom",
                content_type="audio/mp4",
            )
            vrec_id = new_id()
            sess.add(
                RecordRow(
                    id=vrec_id,
                    user_id=user_id,
                    person_id=ex_id,
                    kind="voice",
                    title_enc=db_container.cipher.encrypt_str(
                        dek, "留痕录音", user_id=user_id, table="records", column="title_enc", row_id=vrec_id
                    ),
                    body_enc=db_container.cipher.encrypt_str(
                        dek, "对方承诺月底结清欠款", user_id=user_id, table="records", column="body_enc", row_id=vrec_id
                    ),
                    snippet_enc=db_container.cipher.encrypt_str(
                        dek, "对方承诺月底结清欠款", user_id=user_id, table="records", column="snippet_enc", row_id=vrec_id
                    ),
                    media_object_key=voice_key,
                    audio_duration_ms=8000,
                    happened_on=date(2026, 11, 10),
                )
            )

        # 2. Demote Gold -> Silver via PATCH /v1/persons/{id}/tier
        demote = await client.patch(
            f"/v1/persons/{ex_id}/tier",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "If-Match": f'"{ex["version"]}"',
            },
            json={"tier": "silver"},
        )
        assert demote.status_code == 200, demote.text
        assert demote.json()["tier"] == "silver"

        # 3. Export via step-up reauth and inspect ZIP contents
        reauth_req = await client.post(
            "/v1/auth/email/otp:request",
            json={"email": "silver_export_cloak@example.com", "purpose": "reauth"},
        )
        assert reauth_req.status_code == 200
        reauth_ver = await client.post(
            "/v1/auth/email/otp:verify",
            headers=headers,
            json={
                "challengeId": reauth_req.json()["challengeId"],
                "email": "silver_export_cloak@example.com",
                "code": reauth_req.json()["devOtpCode"],
            },
        )
        assert reauth_ver.status_code == 200
        exp = await client.post(
            "/v1/me/exports",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "X-Reauth-Token": reauth_ver.json()["reauthToken"],
            },
            json={"asyncMode": False},
        )
        assert exp.status_code == 201, exp.text
        ebody = exp.json()
        dl = await client.get(
            f"/v1/me/exports/{ebody['exportId']}/download",
            params={"token": ebody["downloadToken"]},
            headers=headers,
        )
        assert dl.status_code == 200

        with zipfile.ZipFile(io.BytesIO(dl.content), "r") as zf:
            names = set(zf.namelist())
            # ZERO Silver image files in media/, while Silver .m4a voice evidence IS preserved!
            assert not any(n.endswith((".jpg", ".jpeg", ".png", ".webp")) for n in names), names
            assert any(n.startswith("media/") and n.endswith(".m4a") for n in names), names

            data_doc = json.loads(zf.read("data.json").decode("utf-8"))
            silver_p = next(p for p in data_doc["persons"] if p["id"] == str(ex_id))
            assert silver_p["hasPhoto"] is False
            assert silver_p["avatarObjectKey"] is None
            assert silver_p["mainPhotoObjectKey"] is None
            assert silver_p["subPhotoObjectKey"] is None

            photo_mat = next(m for m in data_doc["materials"] if m["kind"] == "photo")
            assert photo_mat["mediaObjectKey"] is None
            voice_mat = next(m for m in data_doc["materials"] if m["kind"] == "voice")
            assert voice_mat["mediaObjectKey"] == voice_key

            journal_md = zf.read("journal.md").decode("utf-8")
            assert "## 银盾 · 边界" in journal_md
            assert "!(" not in journal_md and "![旧合影]" not in journal_md
            assert "[原声录音](media/" in journal_md


