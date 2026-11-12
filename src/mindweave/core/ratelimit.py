"""Token-bucket rate limiter and daily AI/voice quota enforcement."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.clock import Clock
from mindweave.core.errors import QuotaExceededError, RateLimitedError
from mindweave.core.orm import CaptureRow, ChatMessageRow
from mindweave.shared.scopes import UserScope


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
                )
            bucket.tokens -= 1.0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class QuotaService:
    """Enforces daily chat message and voice duration quotas (Assumptions A-08 / A-09)."""

    def __init__(
        self,
        clock: Clock,
        *,
        daily_chat_quota: int = 100,
        daily_voice_minutes_quota: int = 60,
    ) -> None:
        self._clock = clock
        self._daily_chat_quota = daily_chat_quota
        self._daily_voice_ms_quota = daily_voice_minutes_quota * 60 * 1000

    def _day_bounds_utc(self, tz_name: str) -> tuple[datetime, datetime]:
        tz = ZoneInfo(tz_name)
        local_now = self._clock.now().astimezone(tz)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta

        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    async def check_chat_quota(
        self, session: AsyncSession, scope: UserScope, *, tz_name: str = "Asia/Shanghai"
    ) -> int:
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
        return self._daily_chat_quota - used_count

    async def check_voice_quota(
        self,
        session: AsyncSession,
        scope: UserScope,
        *,
        additional_ms: int = 0,
        tz_name: str = "Asia/Shanghai",
    ) -> int:
        start_utc, end_utc = self._day_bounds_utc(tz_name)
        used = await session.scalar(
            select(func.coalesce(func.sum(CaptureRow.duration_ms), 0)).where(
                CaptureRow.user_id == scope.user_id,
                CaptureRow.source == "voice",
                CaptureRow.created_at >= start_utc,
                CaptureRow.created_at < end_utc,
            )
        )
        used_ms = int(used or 0)
        if used_ms + additional_ms > self._daily_voice_ms_quota:
            raise QuotaExceededError(
                "Daily voice transcription quota reached.",
                extensions={
                    "limitMs": self._daily_voice_ms_quota,
                    "usedMs": used_ms,
                },
            )
        return max(0, self._daily_voice_ms_quota - used_ms)
