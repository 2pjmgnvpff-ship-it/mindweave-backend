"""Application settings with production compliance guardrails."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_KEK_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DEFAULT_DEV_JWT_SECRET = "mindweave-dev-jwt-secret-change-in-production-32bytes!"  # noqa: S105
_EMAIL_ADDR_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]{2,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINDWEAVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "test", "staging", "prod_cn"] = "dev"
    service_name: str = "mindweave-backend"
    api_prefix: str = "/v1"
    debug: bool = False
    embed_worker_in_api: bool = False

    # PostgreSQL connections:
    # - app_database_url MUST connect as a NOBYPASSRLS role (enforces RLS!)
    # - system_database_url connects as BYPASSRLS role ONLY for auth bootstrap & cron scan
    # - migration_database_url connects as DDL table owner for Alembic
    app_database_url: str = (
        "postgresql+asyncpg://mindweave_app:mindweave_app@127.0.0.1:55432/mindweave"
    )
    system_database_url: str = (
        "postgresql+asyncpg://mindweave_system:mindweave_system@127.0.0.1:55432/mindweave"
    )
    migration_database_url: str = (
        "postgresql+asyncpg://mindweave_owner:mindweave_owner@127.0.0.1:55432/mindweave"
    )

    # Cryptography & Auth
    kek_id: str = "kek-local-v1"
    kek_hex: str = Field(default=DEFAULT_DEV_KEK_HEX, min_length=64, max_length=64)
    dek_cache_ttl_seconds: int = 300
    dek_cache_max_entries: int = 2048

    jwt_secret: str = Field(default=DEFAULT_DEV_JWT_SECRET, min_length=32)
    jwt_issuer: str = "mindweave"
    access_token_ttl_seconds: int = 900  # 15 min
    reauth_token_ttl_seconds: int = 300  # 5 min step-up token for export / account deletion
    refresh_token_ttl_days: int = 30
    otp_ttl_seconds: int = 600  # 10 min
    otp_max_attempts: int = 5
    allow_dev_mock_apple_auth: bool = True
    expose_dev_otp_in_response: bool = False

    # Email OTP Delivery (A3 Primary: resend; Zero-domain fallback: smtp)
    email_provider: Literal["noop", "resend", "smtp"] = "noop"
    email_from_name: str = "心络 MindWeave"
    email_from_address: str = ""
    email_send_timeout_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    resend_api_key: str = ""
    resend_base_url: str = "https://api.resend.com"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    smtp_use_starttls: bool = False
    smtp_enforce_from_match_user: bool = True

    # OTP Anti-Abuse & Proxy Hops
    otp_resend_cooldown_seconds: int = Field(default=60, ge=0)
    otp_max_per_hour: int = Field(default=10, ge=1)
    otp_max_per_day: int = Field(default=30, ge=1)
    otp_ip_rate_limit_per_minute: int = Field(default=10, ge=1)
    trusted_proxy_hops: int = Field(default=1, ge=0, le=5)

    # Storage
    blob_storage_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / ".data" / "blobs"
    )
    ephemeral_screenshot_by_default: bool = True
    max_audio_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_audio_duration_sec: int = 120
    max_image_bytes: int = 10 * 1024 * 1024  # 10 MB

    # Product PWA static root served at `/` (repo-root `web/`; Docker image copies it to /app/web)
    web_dist_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[3] / "web")

    # Push providers. "log" adapters only write a zero-PII log line and report
    # `delivered=False` honestly; they are forbidden in staging / prod_cn.
    push_apns_adapter: Literal["log", "disabled"] = "log"
    push_web_push_adapter: Literal["log", "disabled"] = "log"

    # AI Model Gateway
    ai_primary_provider: Literal["fake", "gemini", "dashscope", "ark", "deepseek"] = "fake"
    ai_fallback_provider: Literal["none", "fake", "gemini", "dashscope", "ark", "deepseek"] = "none"
    ai_asr_provider: Literal["inherit", "fake", "gemini", "dashscope", "ark"] = "inherit"
    gemini_api_key: str = ""
    gemini_use_vertexai: bool = False
    gemini_vertex_project: str = ""
    gemini_vertex_location: str = "global"
    gemini_model_fast: str = "gemini-3.8-flash"
    gemini_model_chat: str = "gemini-3.8-flash"
    gemini_model_lite: str = "gemini-3.5-flash-lite"
    gemini_paid_tier_acknowledged: bool = False

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_extract_model: str = "qwen-plus"
    dashscope_vision_model: str = "qwen-vl-max"
    dashscope_asr_model: str = "qwen3-omni-flash"

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_chat_model: str = "doubao-1-5-pro-32k"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_chat_model: str = "deepseek-chat"  # deepseek-reasoner/R1 is explicitly forbidden

    ai_timeout_seconds: float = 18.0
    safety_timeout_seconds: float = 5.0
    prompts_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / "prompts"
    )

    # Domain policies & quotas
    gate_confidence_threshold: float = 0.80
    undo_window_seconds: int = 30
    venting_hold_expiry_hours: int = 72
    soft_delete_retention_days: int = 30
    export_download_ttl_hours: int = 24
    daily_chat_quota: int = 100
    daily_voice_minutes_quota: int = 60
    daily_capture_quota: int = 150
    daily_image_quota: int = 40
    daily_ai_cost_micros_cny_per_user: int = 5_000_000
    daily_global_ai_cost_micros_cny: int = 200_000_000
    late_night_start_hour: int = 0
    late_night_end_hour: int = 5
    late_night_continuous_minutes: int = 45
    default_timezone: str = "Asia/Shanghai"

    # CORS
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:8790", "http://localhost:8790"]
    )

    @model_validator(mode="after")
    def _validate_production_guardrails(self) -> Settings:
        # Step 1: ADR-001 forbids reasoning models (DeepSeek-R1/reasoner)
        if (
            "reasoner" in self.deepseek_chat_model.lower()
            or "-r1" in self.deepseek_chat_model.lower()
        ):
            raise ValueError(
                "ADR-001 forbids reasoning models (DeepSeek-R1/reasoner) due to first-token latency"
            )

        # Step 2: Dev auth shortcuts & debug mode forbidden in staging / prod_cn
        if self.environment in ("staging", "prod_cn") and (
            self.allow_dev_mock_apple_auth or self.expose_dev_otp_in_response
        ):
            raise ValueError("Dev auth shortcuts must be disabled in staging and prod_cn")
        if self.environment in ("staging", "prod_cn") and self.debug:
            raise ValueError("MINDWEAVE_DEBUG must be false in staging and prod_cn")

        # Step 3: Compliance Gate M6 — Gemini strictly forbidden in prod_cn
        if self.environment == "prod_cn" and (
            self.ai_primary_provider == "gemini"
            or self.ai_fallback_provider == "gemini"
            or self.ai_asr_provider == "gemini"
        ):
            raise ValueError(
                "Compliance Gate M6: Gemini provider is strictly forbidden in prod_cn; "
                "switch to a domestic filed model (dashscope / ark / deepseek)."
            )

        # Step 4: No Fake AI in staging AND prod_cn
        if self.environment in ("staging", "prod_cn") and (
            self.ai_primary_provider == "fake"
            or self.ai_fallback_provider == "fake"
            or self.ai_asr_provider == "fake"
        ):
            raise ValueError(f"{self.environment} must not use fake AI provider")

        # Step 5: Non-default KEK / JWT secrets in prod_cn
        if self.environment == "prod_cn":
            if self.kek_hex == DEFAULT_DEV_KEK_HEX:
                raise ValueError("prod_cn must not use DEFAULT_DEV_KEK_HEX")
            if self.jwt_secret == DEFAULT_DEV_JWT_SECRET:
                raise ValueError("prod_cn must not use DEFAULT_DEV_JWT_SECRET")

        # Step 6: Honest push adapters in staging / prod_cn
        if self.environment in ("staging", "prod_cn") and "log" in (
            self.push_apns_adapter,
            self.push_web_push_adapter,
        ):
            raise ValueError(
                "Log-only push adapters never deliver notifications and are forbidden in "
                "staging and prod_cn; configure a real provider or set "
                "MINDWEAVE_PUSH_APNS_ADAPTER / MINDWEAVE_PUSH_WEB_PUSH_ADAPTER=disabled"
            )

        # Step 7: Real email provider mandatory in staging / prod_cn
        if self.environment in ("staging", "prod_cn"):
            if self.email_provider == "noop":
                raise ValueError(
                    "Email OTP provider 'noop' is forbidden in staging and prod_cn; "
                    "configure 'resend' or 'smtp'"
                )
            if self.email_provider == "resend" and self.email_from_address.strip().lower().endswith(
                "@resend.dev"
            ):
                raise ValueError(
                    "Sandbox sender '@resend.dev' is forbidden in staging and prod_cn; "
                    "verify a sending domain in Resend or use SMTP"
                )

        # Step 8: Email provider config integrity (ALL environments when resend/smtp)
        if self.email_provider == "resend":
            api_key = self.resend_api_key.strip()
            if not api_key:
                raise ValueError("MINDWEAVE_RESEND_API_KEY is required when email_provider='resend'")
            base_url = self.resend_base_url.strip().rstrip("/")
            self.resend_base_url = base_url
            if base_url == "https://api.resend.com" and not api_key.startswith("re_"):
                raise ValueError(
                    "MINDWEAVE_RESEND_API_KEY must start with 're_' for https://api.resend.com"
                )
            from_addr = self.email_from_address.strip()
            if not _EMAIL_ADDR_RE.match(from_addr):
                raise ValueError(
                    "MINDWEAVE_EMAIL_FROM_ADDRESS must be a valid email address for Resend"
                )
            self.email_from_address = from_addr
        elif self.email_provider == "smtp":
            host = self.smtp_host.strip()
            user = self.smtp_username.strip()
            password = "".join(self.smtp_password.split())
            if not host or not user or not password:
                raise ValueError(
                    "MINDWEAVE_SMTP_HOST, MINDWEAVE_SMTP_USERNAME, and MINDWEAVE_SMTP_PASSWORD "
                    "are required when email_provider='smtp'"
                )
            self.smtp_host = host
            self.smtp_username = user
            self.smtp_password = password
            if self.smtp_port == 25:
                raise ValueError(
                    "SMTP port 25 is blocked on Google Cloud; use 465 (SSL) or 587 (STARTTLS)"
                )
            if self.smtp_use_ssl and self.smtp_use_starttls:
                raise ValueError(
                    "smtp_use_ssl and smtp_use_starttls are mutually exclusive"
                )
            if self.environment != "test" and not (self.smtp_use_ssl or self.smtp_use_starttls):
                raise ValueError(
                    "SMTP requires either smtp_use_ssl=True (465) or smtp_use_starttls=True (587)"
                )
            from_addr = self.email_from_address.strip() or user
            if not _EMAIL_ADDR_RE.match(from_addr):
                raise ValueError("MINDWEAVE_EMAIL_FROM_ADDRESS must be a valid email address")
            self.email_from_address = from_addr
            if (
                host.lower().endswith(".qq.com") or self.smtp_enforce_from_match_user
            ) and from_addr.lower() != user.lower():
                raise ValueError(
                    "SMTP From address must exactly match smtp_username "
                    "(required by QQ Mail 501/550 policy)"
                )

        if (
            self.environment != "test"
            and self.email_provider in ("resend", "smtp")
            and self.expose_dev_otp_in_response
        ):
            raise ValueError(
                "expose_dev_otp_in_response must be False when a real email provider is active"
            )

        # Step 8b: Normalize Vertex AI ADC configuration when enabled
        if self.gemini_use_vertexai:
            resolved_project = (
                self.gemini_vertex_project.strip()
                or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
                or os.environ.get("GCP_PROJECT", "").strip()
            )
            self.gemini_vertex_project = resolved_project
            self.gemini_vertex_location = self.gemini_vertex_location.strip() or "global"

        # Step 9: Non-blank live AI credentials (exempt environment == "test" for unit spy adapters)
        if self.environment != "test" and self.ai_primary_provider in (
            "gemini",
            "dashscope",
            "ark",
            "deepseek",
        ):
            if self.ai_primary_provider == "gemini":
                if self.gemini_use_vertexai:
                    if not self.gemini_vertex_project.strip():
                        raise ValueError(
                            "Missing GCP project for Gemini Vertex AI ADC "
                            "(set MINDWEAVE_GEMINI_VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT)"
                        )
                elif not self.gemini_api_key.strip():
                    raise ValueError(
                        "Missing API key for configured AI provider 'gemini'"
                    )
            else:
                key_by_provider = {
                    "dashscope": self.dashscope_api_key,
                    "ark": self.ark_api_key,
                    "deepseek": self.deepseek_api_key,
                }
                if not key_by_provider[self.ai_primary_provider].strip():
                    raise ValueError(
                        f"Missing API key for configured AI provider '{self.ai_primary_provider}'"
                    )

        return self

    def is_gemini_configured(self) -> bool:
        """Return True iff Gemini has either Vertex AI ADC project or a non-blank API key."""
        if self.gemini_use_vertexai:
            return bool(self.gemini_vertex_project.strip())
        return bool(self.gemini_api_key.strip())

    @property
    def gemini_auth_mode(self) -> str:
        """Report active Gemini authentication mode without exposing secrets."""
        if self.gemini_use_vertexai and self.gemini_vertex_project.strip():
            return "vertex_adc"
        if self.gemini_api_key.strip():
            return "api_key"
        return "none"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
