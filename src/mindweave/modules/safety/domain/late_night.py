"""Pure late-night anti-addiction & warm rest care policy from SOP v0.2 §3.3 (GB 45438-2025).

100% pure Python domain policy — never blocks the user, only appends a warm care note.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOP_LATE_NIGHT_CARE_MESSAGE: str = (
    "今天受了这么多委屈，骂出来痛快多了吧？现在夜深了，先好好睡一觉养足精神，"
    "你的手账我都帮你锁好了，明天醒来我们再看下一步。"
)


@dataclass(frozen=True, slots=True)
class LateNightCareDecision:
    """Result of evaluating continuous late-night session duration."""

    should_remind: bool
    care_message: str | None
    local_hour: int
    continuous_minutes: float


class LateNightCarePolicy:
    """Checks if local time is in `[00:00, 05:00)` and continuous session >= `45` minutes."""

    def __init__(
        self,
        *,
        start_hour: int = 0,
        end_hour: int = 5,
        threshold_minutes: int = 45,
        default_tz: str = "Asia/Shanghai",
    ) -> None:
        self._start_hour = start_hour
        self._end_hour = end_hour
        self._threshold_minutes = threshold_minutes
        self._default_tz = default_tz

    def evaluate(
        self,
        *,
        now_utc: datetime,
        session_started_at_utc: datetime | None,
        timezone_name: str | None = None,
        enabled: bool = True,
    ) -> LateNightCareDecision:
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        tz_key = (timezone_name or self._default_tz).strip() or self._default_tz
        try:
            tz = ZoneInfo(tz_key)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo(self._default_tz)

        local_dt = now_utc.astimezone(tz)
        local_hour = local_dt.hour

        if session_started_at_utc is None:
            return LateNightCareDecision(
                should_remind=False,
                care_message=None,
                local_hour=local_hour,
                continuous_minutes=0.0,
            )

        if session_started_at_utc.tzinfo is None:
            session_started_at_utc = session_started_at_utc.replace(tzinfo=UTC)

        elapsed_minutes = max(0.0, (now_utc - session_started_at_utc).total_seconds() / 60.0)
        in_late_night_window = self._start_hour <= local_hour < self._end_hour

        if enabled and in_late_night_window and elapsed_minutes >= float(self._threshold_minutes):
            return LateNightCareDecision(
                should_remind=True,
                care_message=SOP_LATE_NIGHT_CARE_MESSAGE,
                local_hour=local_hour,
                continuous_minutes=elapsed_minutes,
            )

        return LateNightCareDecision(
            should_remind=False,
            care_message=None,
            local_hour=local_hour,
            continuous_minutes=elapsed_minutes,
        )
