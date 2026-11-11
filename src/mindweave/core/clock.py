"""Injectable UTC clock abstraction for deterministic testing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Deterministic controllable clock for unit and integration tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        base = initial or datetime(2026, 11, 12, 4, 0, 0, tzinfo=UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        self._current = base.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current = dt.astimezone(UTC)

    def advance(self, **kwargs: float) -> datetime:
        self._current = self._current + timedelta(**kwargs)
        return self._current


def to_user_local(dt_utc: datetime, tz_name: str) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(ZoneInfo(tz_name))
