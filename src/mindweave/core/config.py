"""Application settings with production compliance guardrails."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_KEK_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DEFAULT_DEV_JWT_SECRET = "mindweave-dev-jwt-secret-change-in-production-32bytes!"  # noqa: S105


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

    # Storage
    blob_storage_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / ".data" / "blobs"
    )
    ephemeral_screenshot_by_default: bool = True
    max_audio_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_audio_duration_sec: int = 120
    max_image_bytes: int = 10 * 1024 * 1024  # 10 MB

    # AI Model Gateway
    ai_primary_provider: Literal["fake", "gemini", "dashscope", "ark", "deepseek"] = "fake"
    ai_fallback_provider: Literal["none", "fake", "gemini", "dashscope", "ark", "deepseek"] = "none"
    gemini_api_key: str = ""
    gemini_model_fast: str = "gemini-2.5-flash"
    gemini_model_chat: str = "gemini-2.5-flash"
    gemini_paid_tier_acknowledged: bool = False

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_extract_model: str = "qwen-plus"
    dashscope_vision_model: str = "qwen-vl-max"

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
    undo_window_seconds: int = 5
    venting_hold_expiry_hours: int = 72
    soft_delete_retention_days: int = 30
    export_download_ttl_hours: int = 24
    daily_chat_quota: int = 100
    daily_voice_minutes_quota: int = 60
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
        if (
            "reasoner" in self.deepseek_chat_model.lower()
            or "-r1" in self.deepseek_chat_model.lower()
        ):
            raise ValueError(
                "ADR-001 forbids reasoning models (DeepSeek-R1/reasoner) due to first-token latency"
            )
        if self.environment in ("staging", "prod_cn") and (
            self.allow_dev_mock_apple_auth or self.expose_dev_otp_in_response
        ):
            raise ValueError("Dev auth shortcuts must be disabled in staging and prod_cn")
        if self.environment == "prod_cn":
            if self.ai_primary_provider == "gemini" or self.ai_fallback_provider == "gemini":
                raise ValueError(
                    "Compliance Gate M6: Gemini provider is strictly forbidden in prod_cn; "
                    "switch to a domestic filed model (dashscope / ark / deepseek)."
                )
            if self.ai_primary_provider == "fake" or self.ai_fallback_provider == "fake":
                raise ValueError("prod_cn must not use fake AI provider")
            if self.kek_hex == DEFAULT_DEV_KEK_HEX:
                raise ValueError("prod_cn must not use DEFAULT_DEV_KEK_HEX")
            if self.jwt_secret == DEFAULT_DEV_JWT_SECRET:
                raise ValueError("prod_cn must not use DEFAULT_DEV_JWT_SECRET")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
