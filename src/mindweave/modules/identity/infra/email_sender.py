"""Real Email OTP delivery adapters: Resend HTTP API (primary) & QQ/Gmail SMTP SSL (fallback).

Design & Security Invariants:
- Inline non-blocking `await` (never FastAPI BackgroundTasks, which freeze under Cloud Run CPU throttling).
- Zero-PII logging & errors (Invariant I10): never log or echo recipient email, 6-digit OTP, API keys, or SMTP passwords.
- Zero HTML injection (`immich GHSA-v94m-9pp2-pm59`): strictly validates `^\\d{6}$` and escapes all interpolated strings.
- Strict TLS verification (`ssl.CERT_REQUIRED`, `check_hostname=True`, `immich PR #9506/#22832`).
- 100% 7-bit ASCII RFC 2047 headers (`email.policy.SMTP`) with explicit `Date` and `Message-ID` for QQ Mail compliance.
"""

from __future__ import annotations

import asyncio
import email.policy
import html
import random
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any
from uuid import UUID

import httpx

from mindweave.core.config import Settings
from mindweave.core.errors import EmailDeliveryError
from mindweave.core.logging import get_logger
from mindweave.modules.identity.domain.models import EmailDeliveryReceipt, OtpPurpose
from mindweave.modules.identity.domain.ports import EmailOtpSenderPort
from mindweave.modules.identity.infra.apple_verifier import NoopEmailOtpSender

logger = get_logger(__name__)

_STRICT_OTP_RE = re.compile(r"^\d{6}$")
_USER_AGENT = "MindWeave-Backend/1.0 (+https://mindweave.app)"


def render_otp_content(
    *,
    otp_code: str,
    purpose: OtpPurpose,
    expires_in_seconds: int,
) -> tuple[str, str, str]:
    """Build iOS QuickType-compatible, anti-spam, HTML-injection-safe `(subject, text, html)`."""
    if not isinstance(otp_code, str) or not _STRICT_OTP_RE.fullmatch(otp_code):
        raise ValueError("OTP code must be strictly 6 ASCII digits")

    ttl_minutes = max(1, round(int(expires_in_seconds) / 60))
    action_label = "安全验证" if purpose is OtpPurpose.REAUTH else "登录验证码"
    subject = (
        f"心络安全验证：{otp_code}"
        if purpose is OtpPurpose.REAUTH
        else f"心络登录验证码：{otp_code}"
    )
    text_body = (
        f"你的心络{action_label}是：{otp_code}（{ttl_minutes} 分钟内有效）。\n"
        "如果不是你本人操作，请忽略此邮件。\n"
    )
    safe_code = html.escape(otp_code, quote=True)
    safe_label = html.escape(action_label, quote=True)
    safe_minutes = html.escape(str(ttl_minutes), quote=True)
    html_body = (
        "<!doctype html>"
        '<html lang="zh-CN"><body style="margin:0;padding:24px;background:#FCFBF9;'
        'font-family:-apple-system,BlinkMacSystemFont,PingFang SC,Helvetica Neue,sans-serif;'
        'color:#231F1C;">'
        '<div style="max-width:420px;margin:0 auto;padding:28px 24px;background:#FFFFFF;'
        'border:1px solid #ECE6DE;border-radius:16px;">'
        '<p style="margin:0 0 8px;font-size:13px;letter-spacing:0.08em;color:#8D8276;">'
        "心络 MindWeave</p>"
        f'<p style="margin:0 0 18px;font-size:15px;line-height:1.6;">你的心络{safe_label}是：</p>'
        '<div style="margin:0 0 18px;padding:16px;background:#FCFBF9;border-radius:12px;'
        'text-align:center;font-size:32px;font-weight:600;letter-spacing:0.28em;color:#1F1B18;">'
        f"{safe_code}</div>"
        f'<p style="margin:0;font-size:13px;line-height:1.6;color:#736A61;">'
        f"验证码在 {safe_minutes} 分钟内有效。如果不是你本人操作，请直接忽略这封邮件。</p>"
        "</div></body></html>"
    )
    return subject, text_body, html_body


def build_smtp_otp_message(
    *,
    from_name: str,
    from_address: str,
    to_address: str,
    otp_code: str,
    purpose: OtpPurpose,
    expires_in_seconds: int,
) -> EmailMessage:
    """Build RFC 5322 / RFC 2047 100% 7-bit ASCII header MIME message (`email.policy.SMTP`)."""
    subject, text_body, html_body = render_otp_content(
        otp_code=otp_code,
        purpose=purpose,
        expires_in_seconds=expires_in_seconds,
    )
    msg = EmailMessage(policy=email.policy.SMTP)
    domain = from_address.partition("@")[2] or "mindweave.app"
    msg["From"] = formataddr((from_name, from_address)) if from_name else from_address
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg


class UnconfiguredEmailOtpSender:
    """Fail-closed adapter returned when staging/prod lacks a real email provider."""

    @property
    def provider_name(self) -> str:
        return "unconfigured"

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: UUID | None = None,
    ) -> EmailDeliveryReceipt:
        del email, otp_code, purpose, expires_in_seconds, challenge_id
        raise EmailDeliveryError(
            "Email delivery provider is not configured.",
            error_code="email_provider_not_configured",
            status_code=503,
            title="Email Provider Not Configured",
        )

    async def aclose(self) -> None:
        return None


class ResendEmailOtpSender:
    """Primary production Email OTP adapter calling Resend HTTP API over a pooled AsyncClient."""

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        from_name: str = "心络 MindWeave",
        base_url: str = "https://api.resend.com",
        timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
        retry_delay_seconds: float = 0.3,
    ) -> None:
        self._api_key = api_key.strip()
        self._from_address = from_address.strip()
        self._from_name = from_name.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = max(1.0, float(timeout_seconds))
        self._retry_delay = max(0.0, float(retry_delay_seconds))
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=self._timeout,
                connect=min(2.0, self._timeout),
                read=min(4.0, self._timeout),
            ),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    @property
    def provider_name(self) -> str:
        return "resend"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _from_header(self) -> str:
        if self._from_name:
            return f"{self._from_name} <{self._from_address}>"
        return self._from_address

    @staticmethod
    def _classify_resend_error(
        status_code: int,
        payload: dict[str, Any],
    ) -> tuple[EmailDeliveryError, bool]:
        """Map Resend HTTP status + JSON error body to zero-PII EmailDeliveryError & retry flag."""
        err_name = str(payload.get("name", "")).strip().lower()
        err_msg = str(payload.get("message", "")).strip().lower()

        if status_code == 403:
            if "only send testing emails" in err_msg:
                return (
                    EmailDeliveryError(
                        "Resend sandbox sender cannot deliver to external recipients.",
                        error_code="email_sender_sandbox_restricted",
                        status_code=502,
                    ),
                    False,
                )
            if "domain is not verified" in err_msg or "not verified" in err_msg:
                return (
                    EmailDeliveryError(
                        "Sending domain is not verified with the email provider.",
                        error_code="email_sender_domain_unverified",
                        status_code=502,
                    ),
                    False,
                )
            return (
                EmailDeliveryError(
                    "Email provider rejected authentication or access.",
                    error_code="email_provider_auth_failed",
                    status_code=502,
                ),
                False,
            )

        if status_code == 401:
            return (
                EmailDeliveryError(
                    "Email provider authentication failed.",
                    error_code="email_provider_auth_failed",
                    status_code=502,
                ),
                False,
            )

        if status_code == 409:
            if err_name == "concurrent_idempotent_requests" or "concurrent" in err_msg:
                return (
                    EmailDeliveryError(
                        "Email provider reported concurrent duplicate request.",
                        error_code="email_provider_rate_limited",
                        status_code=503,
                    ),
                    True,
                )
            return (
                EmailDeliveryError(
                    "Email provider rejected idempotency request.",
                    error_code="email_provider_auth_failed",
                    status_code=502,
                ),
                False,
            )

        if status_code == 429:
            if (
                err_name in ("daily_quota_exceeded", "monthly_quota_exceeded")
                or "quota" in err_msg
            ):
                return (
                    EmailDeliveryError(
                        "Email provider daily or monthly quota exceeded.",
                        error_code="email_provider_quota_exceeded",
                        status_code=503,
                    ),
                    False,
                )
            return (
                EmailDeliveryError(
                    "Email provider rate limit exceeded.",
                    error_code="email_provider_rate_limited",
                    status_code=503,
                ),
                True,
            )

        if status_code in (400, 422):
            if "recipient" in err_msg or "to" in err_msg or "invalid" in err_msg:
                return (
                    EmailDeliveryError(
                        "Email address was rejected by the mail provider.",
                        error_code="email_recipient_rejected",
                        status_code=422,
                    ),
                    False,
                )
            return (
                EmailDeliveryError(
                    "Email delivery request was rejected by the mail provider.",
                    error_code="email_provider_auth_failed",
                    status_code=502,
                ),
                False,
            )

        if status_code >= 500:
            return (
                EmailDeliveryError(
                    "Email provider timed out or encountered a server error.",
                    error_code="email_delivery_timeout",
                    status_code=503,
                ),
                True,
            )

        return (
            EmailDeliveryError(
                "Email delivery failed unexpectedly.",
                error_code="email_delivery_timeout",
                status_code=503,
            ),
            False,
        )

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: UUID | None = None,
    ) -> EmailDeliveryReceipt:
        subject, text_body, html_body = render_otp_content(
            otp_code=otp_code,
            purpose=purpose,
            expires_in_seconds=expires_in_seconds,
        )
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if challenge_id is not None:
            headers["Idempotency-Key"] = f"otp/{challenge_id}"

        body: dict[str, Any] = {
            "from": self._from_header(),
            "to": [email],
            "subject": subject,
            "text": text_body,
            "html": html_body,
        }
        url = f"{self._base_url}/emails"

        last_error: EmailDeliveryError | None = None
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    self._client.post(url, headers=headers, json=body),
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, TimeoutError) as exc:
                last_error = EmailDeliveryError(
                    "Email provider request timed out.",
                    error_code="email_delivery_timeout",
                    status_code=503,
                )
                logger.warning(
                    "auth.email_delivery_timeout",
                    provider=self.provider_name,
                    challenge_id=str(challenge_id) if challenge_id else None,
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                )
                if attempt == 0:
                    await asyncio.sleep(self._retry_delay * (0.85 + 0.3 * random.random()))  # noqa: S311
                    continue
                raise last_error from None
            except Exception as exc:
                logger.warning(
                    "auth.email_delivery_network_error",
                    provider=self.provider_name,
                    challenge_id=str(challenge_id) if challenge_id else None,
                    error_type=type(exc).__name__,
                )
                raise EmailDeliveryError(
                    "Email delivery service is temporarily unreachable.",
                    error_code="email_delivery_timeout",
                    status_code=503,
                ) from None

            if response.status_code in (200, 201):
                try:
                    data = response.json()
                except Exception:
                    data = {}
                msg_id = str(data.get("id", "")).strip() if isinstance(data, dict) else ""
                if not msg_id:
                    logger.warning(
                        "auth.email_delivery_missing_id",
                        provider=self.provider_name,
                        challenge_id=str(challenge_id) if challenge_id else None,
                        http_status=response.status_code,
                    )
                    raise EmailDeliveryError(
                        "Email provider returned an incomplete delivery receipt.",
                        error_code="email_delivery_timeout",
                        status_code=503,
                    )
                return EmailDeliveryReceipt(
                    delivered=True,
                    provider=self.provider_name,
                    delivery_status="delivered",
                    provider_message_id=msg_id,
                )

            try:
                err_payload = response.json()
                if not isinstance(err_payload, dict):
                    err_payload = {}
            except Exception:
                err_payload = {}

            mapped_err, retryable = self._classify_resend_error(response.status_code, err_payload)
            last_error = mapped_err
            logger.warning(
                "auth.email_delivery_provider_error",
                provider=self.provider_name,
                challenge_id=str(challenge_id) if challenge_id else None,
                http_status=response.status_code,
                error_code=mapped_err.error_code,
                attempt=attempt + 1,
                retryable=retryable,
            )
            if retryable and attempt == 0:
                await asyncio.sleep(self._retry_delay * (0.85 + 0.3 * random.random()))  # noqa: S311
                continue
            raise mapped_err

        assert last_error is not None
        raise last_error


class SmtpEmailOtpSender:
    """Zero-domain Email OTP fallback supporting QQ Mail (465 SSL) and Gmail (465 SSL / 587 STARTTLS)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str = "",
        from_name: str = "心络 MindWeave",
        use_ssl: bool = True,
        use_starttls: bool = False,
        enforce_from_match_user: bool = True,
        timeout_seconds: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._host = host.strip()
        self._port = int(port)
        self._username = username.strip()
        self._password = "".join(password.split())
        self._from_address = (from_address.strip() or self._username).strip()
        self._from_name = from_name.strip()
        self._use_ssl = bool(use_ssl)
        self._use_starttls = bool(use_starttls)
        self._enforce_from_match = bool(enforce_from_match_user) or self._host.lower().endswith(
            ".qq.com"
        )
        self._socket_timeout = max(1.0, min(5.0, float(timeout_seconds)))
        self._overall_timeout = max(6.0, float(timeout_seconds) + 1.0)
        self._ssl_context = ssl_context

    @property
    def provider_name(self) -> str:
        return "smtp"

    async def aclose(self) -> None:
        return None

    def _build_ssl_context(self) -> ssl.SSLContext:
        if self._ssl_context is not None:
            return self._ssl_context
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    @staticmethod
    def _classify_smtp_error(exc: Exception) -> EmailDeliveryError:
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return EmailDeliveryError(
                "SMTP authentication failed.",
                error_code="email_provider_auth_failed",
                status_code=502,
            )

        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            codes = [int(code) for code, _ in exc.recipients.values()]
            msgs = " ".join(
                (msg.decode("utf-8", errors="ignore") if isinstance(msg, bytes) else str(msg)).lower()
                for _, msg in exc.recipients.values()
            )
            if 554 in codes or "spam" in msgs or "0/92" in msgs or "policy" in msgs:
                return EmailDeliveryError(
                    "Recipient mail server rejected the verification message.",
                    error_code="email_recipient_rejected",
                    status_code=502,
                )
            return EmailDeliveryError(
                "Recipient email address was rejected.",
                error_code="email_recipient_rejected",
                status_code=422,
            )

        if isinstance(exc, smtplib.SMTPSenderRefused):
            code = int(exc.smtp_code)
            raw_msg = (
                exc.smtp_error.decode("utf-8", errors="ignore")
                if isinstance(exc.smtp_error, bytes)
                else str(exc.smtp_error)
            ).lower()
            if code == 501 or ("from" in raw_msg and ("same" in raw_msg or "invalid" in raw_msg or "missing" in raw_msg)):
                return EmailDeliveryError(
                    "SMTP From address does not match authorized sender.",
                    error_code="email_smtp_from_mismatch",
                    status_code=502,
                )
            if code in (421, 451) or "5.4.5" in raw_msg or "frequency" in raw_msg:
                return EmailDeliveryError(
                    "SMTP sender quota or frequency limit exceeded.",
                    error_code="email_provider_quota_exceeded",
                    status_code=503,
                )
            return EmailDeliveryError(
                "SMTP sender was rejected by mail server.",
                error_code="email_smtp_from_mismatch",
                status_code=502,
            )

        if isinstance(exc, smtplib.SMTPResponseException):
            code = int(exc.smtp_code)
            raw_msg = (
                exc.smtp_error.decode("utf-8", errors="ignore")
                if isinstance(exc.smtp_error, bytes)
                else str(exc.smtp_error)
            ).lower()
            if code == 501 or ("from" in raw_msg and ("same" in raw_msg or "invalid" in raw_msg)):
                return EmailDeliveryError(
                    "SMTP From address does not match authorized sender.",
                    error_code="email_smtp_from_mismatch",
                    status_code=502,
                )
            if code == 535:
                return EmailDeliveryError(
                    "SMTP authentication failed.",
                    error_code="email_provider_auth_failed",
                    status_code=502,
                )
            if code in (421, 451) or "5.4.5" in raw_msg or "frequency" in raw_msg or "limit" in raw_msg:
                return EmailDeliveryError(
                    "SMTP sender quota or frequency limit exceeded.",
                    error_code="email_provider_quota_exceeded",
                    status_code=503,
                )
            if code == 554 or "spam" in raw_msg or "0/92" in raw_msg:
                return EmailDeliveryError(
                    "Mail server rejected verification message content.",
                    error_code="email_recipient_rejected",
                    status_code=502,
                )
            if code == 550:
                return EmailDeliveryError(
                    "Recipient mailbox unavailable or rejected.",
                    error_code="email_recipient_rejected",
                    status_code=422,
                )

        if isinstance(exc, smtplib.SMTPException | ssl.SSLError | TimeoutError | OSError):
            return EmailDeliveryError(
                "SMTP mail server timed out or connection failed.",
                error_code="email_delivery_timeout",
                status_code=503,
            )

        return EmailDeliveryError(
            "Email delivery failed.",
            error_code="email_delivery_timeout",
            status_code=503,
        )

    def _send_sync(self, msg: EmailMessage, recipient: str) -> str:
        if self._enforce_from_match and self._from_address.lower() != self._username.lower():
            raise EmailDeliveryError(
                "SMTP From address must match SMTP username.",
                error_code="email_smtp_from_mismatch",
                status_code=502,
            )

        ctx = self._build_ssl_context()
        if self._use_ssl:
            with smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._socket_timeout,
                context=ctx,
            ) as client:
                client.login(self._username, self._password)
                client.send_message(msg, from_addr=self._from_address, to_addrs=[recipient])
        else:
            with smtplib.SMTP(
                self._host,
                self._port,
                timeout=self._socket_timeout,
            ) as client:
                if self._use_starttls:
                    client.starttls(context=ctx)
                client.login(self._username, self._password)
                client.send_message(msg, from_addr=self._from_address, to_addrs=[recipient])
        return str(msg["Message-ID"]).strip()

    async def send_otp(
        self,
        *,
        email: str,
        otp_code: str,
        purpose: OtpPurpose,
        expires_in_seconds: int,
        challenge_id: UUID | None = None,
    ) -> EmailDeliveryReceipt:
        msg = build_smtp_otp_message(
            from_name=self._from_name,
            from_address=self._from_address,
            to_address=email,
            otp_code=otp_code,
            purpose=purpose,
            expires_in_seconds=expires_in_seconds,
        )
        try:
            message_id = await asyncio.wait_for(
                asyncio.to_thread(self._send_sync, msg, email),
                timeout=self._overall_timeout,
            )
        except EmailDeliveryError:
            raise
        except TimeoutError:
            logger.warning(
                "auth.smtp_delivery_timeout",
                provider=self.provider_name,
                challenge_id=str(challenge_id) if challenge_id else None,
            )
            raise EmailDeliveryError(
                "SMTP mail server timed out.",
                error_code="email_delivery_timeout",
                status_code=503,
            ) from None
        except Exception as exc:
            mapped = self._classify_smtp_error(exc)
            logger.warning(
                "auth.smtp_delivery_error",
                provider=self.provider_name,
                challenge_id=str(challenge_id) if challenge_id else None,
                error_code=mapped.error_code,
                error_type=type(exc).__name__,
                smtp_code=getattr(exc, "smtp_code", None),
            )
            raise mapped from None

        return EmailDeliveryReceipt(
            delivered=True,
            provider=self.provider_name,
            delivery_status="delivered",
            provider_message_id=message_id or None,
        )


def is_real_email_sender(sender: object) -> bool:
    """Return True only for a real configured outbound email provider (never noop/unconfigured)."""
    return isinstance(sender, ResendEmailOtpSender | SmtpEmailOtpSender)


def build_email_otp_sender(settings: Settings) -> EmailOtpSenderPort:
    """Construct the Email OTP sender cleanly without raising inside `create_app()`."""
    if settings.email_provider == "resend":
        if not settings.resend_api_key.strip() or not settings.email_from_address.strip():
            return UnconfiguredEmailOtpSender()
        return ResendEmailOtpSender(
            api_key=settings.resend_api_key,
            from_address=settings.email_from_address,
            from_name=settings.email_from_name,
            base_url=settings.resend_base_url,
            timeout_seconds=settings.email_send_timeout_seconds,
        )
    if settings.email_provider == "smtp":
        if (
            not settings.smtp_host.strip()
            or not settings.smtp_username.strip()
            or not settings.smtp_password.strip()
        ):
            return UnconfiguredEmailOtpSender()
        return SmtpEmailOtpSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_address=settings.email_from_address or settings.smtp_username,
            from_name=settings.email_from_name,
            use_ssl=settings.smtp_use_ssl,
            use_starttls=settings.smtp_use_starttls,
            enforce_from_match_user=settings.smtp_enforce_from_match_user,
            timeout_seconds=settings.email_send_timeout_seconds,
        )
    if settings.environment in ("staging", "prod_cn"):
        return UnconfiguredEmailOtpSender()
    return NoopEmailOtpSender()
