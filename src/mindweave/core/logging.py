"""Structured logging with mandatory zero-PII redaction processor (Invariant I10)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from mindweave.shared.pii import mask_hard_pii

SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "reauth_token",
        "id_token",
        "authorization",
        "cookie",
        "otp",
        "code",
        "dek",
        "wrapped_dek",
        "kek",
        "kek_hex",
        "api_key",
        "gemini_api_key",
        "dashscope_api_key",
        "ark_api_key",
        "deepseek_api_key",
        "resend_api_key",
        "smtp_password",
        "smtp_pass",
        "smtp_username",
        "otp_code",
        "verification_code",
        "dev_otp_code",
        "audio_base64",
        "image_base64",
        "photo_base64",
        "raw_transcript",
        "corrected_transcript",
        "client_transcript",
        "user_query",
        "prompt",
        "sig",
        "x_api_key",
        "x_resend_signature",
        "raw_text",
        "transcript",
        "transcript_html",
        "transcript_marked",
        "body",
        "content",
        "snippet",
        "message",
        "reply_text",
        "whisper",
        "whisper_note",
        "display_name",
        "name_cn",
        "person_name",
        "alias",
        "email",
        "phone",
        "phone_or_email",
        "short_memo",
        "traits",
        "perspectives",
        "suggestions",
    }
)

_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_API_KEY_RE = re.compile(
    r"\b(?:AIza[0-9A-Za-z\-_]{25,}|re_[0-9A-Za-z_]{16,}|sk-[0-9A-Za-z\-_]{16,})\b"
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:uid|exp|key|api_key|token|sig|access_token|refresh_token)=)[^&\s\"']+",
    re.IGNORECASE,
)
_OTP_CONTEXT_RE = re.compile(
    r"((?:验证码|otp(?:_code)?|verification_code)\s*[=:：]?\s*)\d{6}\b",
    re.IGNORECASE,
)


def _normalize_key(key: str) -> str:
    # Convert camelCase to snake_case for matching sensitive keys
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return s1.lower().strip()


def sanitize_log_value(value: Any, *, key_hint: str | None = None) -> Any:
    if key_hint is not None and _normalize_key(key_hint) in SENSITIVE_FIELD_NAMES:
        return "[REDACTED]"

    if isinstance(value, Mapping):
        return {str(k): sanitize_log_value(v, key_hint=str(k)) for k, v in value.items()}

    if isinstance(value, list | tuple | set):
        return [sanitize_log_value(item) for item in value]

    if isinstance(value, bytes | bytearray):
        return f"<bytes:{len(value)}>"

    if isinstance(value, str):
        masked = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", value)
        masked = _API_KEY_RE.sub("[REDACTED_API_KEY]", masked)
        masked = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", masked)
        masked = _OTP_CONTEXT_RE.sub(r"\1[REDACTED_OTP]", masked)
        masked = _EMAIL_RE.sub("[REDACTED_EMAIL]", masked)
        masked = mask_hard_pii(masked).masked_text
        return masked

    return value


def redact_sensitive_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key in ("level", "timestamp", "logger"):
            continue
        if key == "event" and isinstance(event_dict[key], str):
            event_dict[key] = sanitize_log_value(event_dict[key])
        else:
            event_dict[key] = sanitize_log_value(event_dict[key], key_hint=key)
    return event_dict


def configure_logging(*, json_logs: bool = True, level: int = logging.INFO) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    shared_pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive_processor,
    ]
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_sensitive_processor,
            renderer,
        ],
    )
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    else:
        for existing_handler in root_logger.handlers:
            existing_handler.setFormatter(formatter)
    root_logger.setLevel(level)

    for noisy in (
        "httpx",
        "httpcore",
        "google_genai",
        "google.genai",
        "aiosmtplib",
        "uvicorn.access",
        "sqlalchemy.engine",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
