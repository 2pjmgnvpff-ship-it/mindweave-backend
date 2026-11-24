"""Token-bucket rate limiter and daily AI/voice quota enforcement."""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.errors import AppError, QuotaExceededError, RateLimitedError
from mindweave.core.orm import AiCallRow, CaptureRow, ChatMessageRow
from mindweave.shared.scopes import UserScope

if TYPE_CHECKING:
    from mindweave.core.db import DatabaseManager


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter keyed by `(scope, route)`."""

    def __init__(self, *, max_buckets: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._max_buckets = max_buckets

    def check(self, key: str, *, capacity: int, refill_per_second: float) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    # Evict oldest inserted key
                    oldest = next(iter(self._buckets))
                    del self._buckets[oldest]
                self._buckets[key] = _Bucket(tokens=float(capacity - 1), last_refill=now)
                return

            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(float(capacity), bucket.tokens + elapsed * refill_per_second)
            bucket.last_refill = now
            if bucket.tokens < 1.0:
                retry_after = max(1, int((1.0 - bucket.tokens) / max(refill_per_second, 0.001)))
                raise RateLimitedError(
                    "Rate limit exceeded. Please slow down and try again shortly.",
                    extensions={"retryAfterSeconds": retry_after},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.tokens -= 1.0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class UserConcurrencyLimiter:
    """Bounds concurrent expensive AI streams/extractions per user (zero Redis)."""

    def __init__(self, *, max_concurrent_per_user: int = 2) -> None:
        self._max = max(1, max_concurrent_per_user)
        self._lock = threading.Lock()
        self._active: dict[UUID, int] = {}

    @asynccontextmanager
    async def slot(self, user_id: UUID, *, max_concurrent: int | None = None) -> AsyncIterator[None]:
        limit = max(1, max_concurrent) if max_concurrent is not None else self._max
        with self._lock:
            current = self._active.get(user_id, 0)
            if current >= limit:
                raise RateLimitedError(
                    "Too many concurrent AI requests in progress. Please wait a moment.",
                    error_code="concurrent_limit_exceeded",
                    extensions={"retryAfterSeconds": 3, "maxConcurrent": limit},
                    headers={"Retry-After": "3"},
                )
            self._active[user_id] = current + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._active.get(user_id, 1) - 1
                if remaining <= 0:
                    self._active.pop(user_id, None)
                else:
                    self._active[user_id] = remaining


class QuotaService:
    """Enforces daily chat, voice, capture, image, per-user CNY, and global CNY kill-switch quotas."""

    def __init__(
        self,
        clock: Clock,
        *,
        db: DatabaseManager | None = None,
        daily_chat_quota: int = 100,
        daily_voice_minutes_quota: int = 60,
        daily_transcribe_calls_quota: int | None = None,
        daily_capture_quota: int = 150,
        daily_image_quota: int = 40,
        daily_ai_cost_micros_cny_per_user: int = 5_000_000,
        daily_global_ai_cost_micros_cny: int = 200_000_000,
        global_budget_cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._clock = clock
        self._db = db
        self._daily_chat_quota = daily_chat_quota
        self._daily_voice_ms_quota = daily_voice_minutes_quota * 60 * 1000
        self._daily_transcribe_calls_quota = (
            daily_transcribe_calls_quota
            if daily_transcribe_calls_quota is not None
            else (max(1, daily_voice_minutes_quota * 4) if daily_voice_minutes_quota > 0 else 0)
        )
        self._daily_capture_quota = daily_capture_quota
        self._daily_image_quota = daily_image_quota
        self._daily_ai_cost_micros_cny_per_user = daily_ai_cost_micros_cny_per_user
        self._daily_global_ai_cost_micros_cny = daily_global_ai_cost_micros_cny
        self._global_budget_cache_ttl_seconds = max(0.0, float(global_budget_cache_ttl_seconds))
        self._global_cost_cache_lock = threading.Lock()
        self._global_cost_cache: tuple[float, int] | None = None

    def _day_bounds_utc(self, tz_name: str) -> tuple[datetime, datetime]:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        local_now = self._clock.now().astimezone(tz)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    async def _enforce_global_budget_if_configured(self, *, tz_name: str) -> None:
        """Enforce global daily CNY kill-switch across all tenants via system_session."""
        if self._db is None or self._daily_global_ai_cost_micros_cny <= 0:
            return
        ttl = self._global_budget_cache_ttl_seconds
        if ttl > 0.0:
            now_mono = time.monotonic()
            with self._global_cost_cache_lock:
                cached = self._global_cost_cache
                if cached is not None and cached[0] > now_mono:
                    if cached[1] >= self._daily_global_ai_cost_micros_cny:
                        raise AppError(
                            "AI service is temporarily paused due to daily safety budget protection.",
                            error_code="ai_budget_paused",
                            status_code=503,
                            title="AI Service Temporarily Paused",
                            headers={"Retry-After": "300"},
                        )
                    return
        async with self._db.system_session() as sys_sess:
            await self.check_global_ai_budget(
                sys_sess,
                tz_name=tz_name,
                cache_ttl_seconds=ttl,
            )

    async def check_chat_quota(
        self, session: AsyncSession, scope: UserScope, *, tz_name: str = "Asia/Shanghai"
    ) -> int:
        await self._enforce_global_budget_if_configured(tz_name=tz_name)
        start_utc, end_utc = self._day_bounds_utc(tz_name)
        used = await session.scalar(
            select(func.count(ChatMessageRow.id)).where(
                ChatMessageRow.user_id == scope.user_id,
                ChatMessageRow.role == "user",
                ChatMessageRow.created_at >= start_utc,
                ChatMessageRow.created_at < end_utc,
            )
        )
        used_count = int(used or 0)
        if used_count >= self._daily_chat_quota:
            raise QuotaExceededError(
                "Daily companion chat quota reached.",
                extensions={"limit": self._daily_chat_quota, "used": used_count},
            )
        await self.check_ai_spend_budget(session, scope, tz_name=tz_name, _check_global=False)
        return self._daily_chat_quota - used_count

    async def check_capture_quota(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        source: str,
        tz_name: str = "Asia/Shanghai",
    ) -> int:
        await self._enforce_global_budget_if_configured(tz_name=tz_name)
        start_utc, end_utc = self._day_bounds_utc(tz_name)
        total_used = int(
            await session.scalar(
                select(func.count(CaptureRow.id)).where(
                    CaptureRow.user_id == scope.user_id,
                    CaptureRow.created_at >= start_utc,
                    CaptureRow.created_at < end_utc,
                )
            )
            or 0
        )
        if self._daily_capture_quota > 0 and total_used >= self._daily_capture_quota:
            raise QuotaExceededError(
                "Daily capture quota reached.",
                extensions={"limit": self._daily_capture_quota, "used": total_used},
            )
        if source == "image" and self._daily_image_quota > 0:
            image_used = int(
                await session.scalar(
                    select(func.count(CaptureRow.id)).where(
                        CaptureRow.user_id == scope.user_id,
                        CaptureRow.source == "image",
                        CaptureRow.created_at >= start_utc,
                        CaptureRow.created_at < end_utc,
                    )
                )
                or 0
            )
            if image_used >= self._daily_image_quota:
                raise QuotaExceededError(
                    "Daily image capture quota reached.",
                    extensions={"limit": self._daily_image_quota, "used": image_used},
                )
        await self.check_ai_spend_budget(session, scope, tz_name=tz_name, _check_global=False)
        return max(0, self._daily_capture_quota - total_used)

    async def check_voice_quota(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        additional_ms: int = 0,
        tz_name: str = "Asia/Shanghai",
    ) -> int:
        await self._enforce_global_budget_if_configured(tz_name=tz_name)
        start_utc, end_utc = self._day_bounds_utc(tz_name)
        used = await session.scalar(
            select(func.coalesce(func.sum(func.coalesce(CaptureRow.duration_ms, 15000)), 0)).where(
                CaptureRow.user_id == scope.user_id,
                CaptureRow.source == "voice",
                CaptureRow.created_at >= start_utc,
                CaptureRow.created_at < end_utc,
            )
        )
        used_ms = int(used or 0)
        if (
            used_ms >= self._daily_voice_ms_quota
            or used_ms + additional_ms > self._daily_voice_ms_quota
        ):
            raise QuotaExceededError(
                "Daily voice transcription quota reached.",
                extensions={
                    "limitMs": self._daily_voice_ms_quota,
                    "usedMs": used_ms,
                },
            )

        used_voice_count = int(
            await session.scalar(
                select(func.count(CaptureRow.id)).where(
                    CaptureRow.user_id == scope.user_id,
                    CaptureRow.source == "voice",
                    CaptureRow.created_at >= start_utc,
                    CaptureRow.created_at < end_utc,
                )
            )
            or 0
        )
        if self._daily_capture_quota > 0 and used_voice_count >= self._daily_capture_quota:
            raise QuotaExceededError(
                "Daily voice capture count quota reached.",
                extensions={
                    "limit": self._daily_capture_quota,
                    "used": used_voice_count,
                },
            )

        used_transcribe_calls = int(
            await session.scalar(
                select(func.count(AiCallRow.id)).where(
                    AiCallRow.user_id == scope.user_id,
                    AiCallRow.task == "transcribe",
                    AiCallRow.created_at >= start_utc,
                    AiCallRow.created_at < end_utc,
                )
            )
            or 0
        )
        if used_transcribe_calls >= self._daily_transcribe_calls_quota:
            raise QuotaExceededError(
                "Daily voice transcription quota reached.",
                extensions={
                    "limitCalls": self._daily_transcribe_calls_quota,
                    "usedCalls": used_transcribe_calls,
                },
            )
        await self.check_ai_spend_budget(session, scope, tz_name=tz_name, _check_global=False)
        return max(0, self._daily_voice_ms_quota - used_ms)

    async def check_ai_spend_budget(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        tz_name: str = "Asia/Shanghai",
        _check_global: bool = True,
    ) -> int:
        if _check_global:
            await self._enforce_global_budget_if_configured(tz_name=tz_name)
        if self._daily_ai_cost_micros_cny_per_user <= 0:
            return 0
        start_utc, end_utc = self._day_bounds_utc(tz_name)
        used_micros = int(
            await session.scalar(
                select(func.coalesce(func.sum(AiCallRow.cost_micros_cny), 0)).where(
                    AiCallRow.user_id == scope.user_id,
                    AiCallRow.created_at >= start_utc,
                    AiCallRow.created_at < end_utc,
                )
            )
            or 0
        )
        if used_micros >= self._daily_ai_cost_micros_cny_per_user:
            raise QuotaExceededError(
                "Daily AI usage budget reached.",
                extensions={
                    "limitMicrosCny": self._daily_ai_cost_micros_cny_per_user,
                    "usedMicrosCny": used_micros,
                },
            )
        return max(0, self._daily_ai_cost_micros_cny_per_user - used_micros)

    async def check_global_ai_budget(
        self,
        system_session: AsyncSession,
        *,
        tz_name: str = "Asia/Shanghai",
        cache_ttl_seconds: float | None = None,
    ) -> int:
        """L3 global daily AI spend kill-switch (cached sum over `ai_calls`)."""
        if self._daily_global_ai_cost_micros_cny <= 0:
            return 0
        ttl = (
            self._global_budget_cache_ttl_seconds
            if cache_ttl_seconds is None
            else max(0.0, float(cache_ttl_seconds))
        )
        now_mono = time.monotonic()
        used_global = -1
        if ttl > 0.0:
            with self._global_cost_cache_lock:
                cached = self._global_cost_cache
                if cached is not None and cached[0] > now_mono:
                    used_global = cached[1]
        if used_global < 0:
            start_utc, end_utc = self._day_bounds_utc(tz_name)
            used_global = int(
                await system_session.scalar(
                    select(func.coalesce(func.sum(AiCallRow.cost_micros_cny), 0)).where(
                        AiCallRow.created_at >= start_utc,
                        AiCallRow.created_at < end_utc,
                    )
                )
                or 0
            )
            if ttl > 0.0:
                with self._global_cost_cache_lock:
                    self._global_cost_cache = (now_mono + ttl, used_global)
        if used_global >= self._daily_global_ai_cost_micros_cny:
            raise AppError(
                "AI service is temporarily paused due to daily safety budget protection.",
                error_code="ai_budget_paused",
                status_code=503,
                title="AI Service Temporarily Paused",
                headers={"Retry-After": "300"},
            )
        return max(0, self._daily_global_ai_cost_micros_cny - used_global)
