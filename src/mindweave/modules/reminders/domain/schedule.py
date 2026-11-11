"""Pure solar + Chinese lunar calendar reminder scheduling engine (zero IO / SDK imports)."""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lunar_python import Lunar, LunarMonth, LunarYear, Solar

from mindweave.shared.enums import ReminderRule

DEFAULT_LEAD_DAYS: tuple[int, ...] = (7, 1, 0)
MAX_LEAD_DAYS = 365
MIN_SUPPORTED_YEAR = 1901
MAX_SUPPORTED_YEAR = 2099


class ReminderScheduleError(ValueError):
    """Raised when reminder rule parameters, calendar bounds, or timezone are invalid."""


@dataclass(frozen=True, slots=True)
class NextFireSlot:
    """Resolved next UTC fire timestamp, lead-day offset, and target Gregorian date."""

    scheduled_for_utc: datetime
    lead_day: int
    occurrence_date: date

    @property
    def fire_at(self) -> datetime:
        return self.scheduled_for_utc


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def validate_timezone_name(timezone: str) -> ZoneInfo:
    cleaned = timezone.strip()
    if not cleaned:
        raise ReminderScheduleError("timezone must not be empty")
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ReminderScheduleError(f"Unknown IANA timezone {timezone!r}") from exc


def normalize_lead_days(lead_days: Sequence[int] | None) -> tuple[int, ...]:
    """Validate, deduplicate, and sort `lead_days` descending (e.g. `(7, 1, 0)`)."""
    if lead_days is None or len(lead_days) == 0:
        return (0,)
    deduped: set[int] = set()
    for raw in lead_days:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ReminderScheduleError("lead_days entries must be integers")
        if raw < 0 or raw > MAX_LEAD_DAYS:
            raise ReminderScheduleError(
                f"lead_days entry {raw} must be between 0 and {MAX_LEAD_DAYS}"
            )
        deduped.add(raw)
    return tuple(sorted(deduped, reverse=True))


def resolve_solar_yearly_date(solar_year: int, target_month: int, target_day: int) -> date:
    """Resolve Gregorian yearly month/day in `solar_year`, falling back Feb 29 -> Feb 28 in non-leap years."""
    if not (MIN_SUPPORTED_YEAR <= solar_year <= MAX_SUPPORTED_YEAR):
        raise ReminderScheduleError(f"solar_year {solar_year} outside supported calendar range")
    if not (1 <= target_month <= 12):
        raise ReminderScheduleError(f"target_month {target_month} must be in 1..12")
    if not (1 <= target_day <= 31):
        raise ReminderScheduleError(f"target_day {target_day} must be in 1..31")

    # Validate that (target_month, target_day) is valid in a reference leap year (2024).
    try:
        date(2024, target_month, target_day)
    except ValueError as exc:
        raise ReminderScheduleError(
            f"Invalid Gregorian month/day combination ({target_month}-{target_day})"
        ) from exc

    if target_month == 2 and target_day == 29 and not calendar.isleap(solar_year):
        return date(solar_year, 2, 28)
    return date(solar_year, target_month, target_day)


def resolve_lunar_yearly_date(
    lunar_year: int,
    target_month: int,
    target_day: int,
    *,
    is_leap_month: bool = False,
) -> date:
    """Resolve a Chinese lunar month/day in `lunar_year` to its Gregorian `date`.

    Edge-case guarantees:
    1. If `is_leap_month=True` and `lunar_year` does NOT have that leap month,
       cleanly falls back to the regular lunar month `target_month`.
    2. If `target_day=30` (大月生日/除夕) falls in a 29-day lunar month (小月) in `lunar_year`,
       cleanly clamps to day 29 instead of raising `ValueError`.
    """
    if not (MIN_SUPPORTED_YEAR <= lunar_year <= MAX_SUPPORTED_YEAR):
        raise ReminderScheduleError(f"lunar_year {lunar_year} outside supported calendar range")
    if not (1 <= target_month <= 12):
        raise ReminderScheduleError(f"Lunar target_month {target_month} must be in 1..12")
    if not (1 <= target_day <= 30):
        raise ReminderScheduleError(f"Lunar target_day {target_day} must be in 1..30")

    ly = LunarYear.fromYear(lunar_year)
    leap_month_in_year = int(ly.getLeapMonth())  # 0 when no leap month, else 1..12
    effective_month = (
        -target_month if (is_leap_month and leap_month_in_year == target_month) else target_month
    )

    lunar_month_obj = LunarMonth.fromYm(lunar_year, effective_month)
    if lunar_month_obj is None:
        raise ReminderScheduleError(
            f"Unable to resolve lunar month {effective_month} in lunar year {lunar_year}"
        )

    max_day_in_month = int(lunar_month_obj.getDayCount())
    effective_day = min(target_day, max_day_in_month)

    solar = Lunar.fromYmd(lunar_year, effective_month, effective_day).getSolar()
    return date(int(solar.getYear()), int(solar.getMonth()), int(solar.getDay()))


def _local_slot_to_utc(
    occurrence_date: date,
    *,
    lead_day: int,
    fire_hour: int,
    fire_minute: int,
    tz: ZoneInfo,
) -> datetime:
    fire_date = occurrence_date - timedelta(days=lead_day)
    local_dt = datetime(
        fire_date.year,
        fire_date.month,
        fire_date.day,
        fire_hour,
        fire_minute,
        0,
        tzinfo=tz,
    )
    return local_dt.astimezone(UTC)


def compute_next_fire_slot(
    *,
    rule: ReminderRule | str,
    timezone: str = "Asia/Shanghai",
    fire_hour: int = 9,
    fire_minute: int = 0,
    lead_days: Sequence[int] = DEFAULT_LEAD_DAYS,
    once_date: date | None = None,
    is_lunar: bool = False,
    target_month: int | None = None,
    target_day: int | None = None,
    is_leap_month: bool = False,
    reference_utc: datetime | None = None,
    after_utc: datetime | None = None,
) -> NextFireSlot | None:
    """Compute the earliest upcoming `(scheduled_for_utc, lead_day)` fire slot.

    - When `after_utc` is supplied (advancing after a fired slot), selects slots strictly `> after_utc`
      (and `>= reference_utc` if both are supplied).
    - When only `reference_utc` is supplied (initial creation / activation), selects slots `>= reference_utc`.
    - Returns `None` for `ReminderRule.ONCE` once all lead slots for `once_date` have passed.
    """
    try:
        rule_enum = ReminderRule(rule)
    except ValueError as exc:
        raise ReminderScheduleError(f"Unsupported reminder rule {rule!r}") from exc

    if isinstance(fire_hour, bool) or not (0 <= fire_hour <= 23):
        raise ReminderScheduleError(f"fire_hour {fire_hour} must be in 0..23")
    if isinstance(fire_minute, bool) or not (0 <= fire_minute <= 59):
        raise ReminderScheduleError(f"fire_minute {fire_minute} must be in 0..59")

    tz = validate_timezone_name(timezone)
    normalized_leads = normalize_lead_days(lead_days)

    if reference_utc is None and after_utc is None:
        raise ReminderScheduleError("At least one of reference_utc or after_utc must be provided")

    ref_norm = _ensure_utc(reference_utc) if reference_utc is not None else None
    after_norm = _ensure_utc(after_utc) if after_utc is not None else None

    def _is_eligible(slot_utc: datetime) -> bool:
        if after_norm is not None and slot_utc <= after_norm:
            return False
        if ref_norm is not None and slot_utc < ref_norm:
            return False
        return True

    anchor_utc = after_norm if after_norm is not None else ref_norm
    assert anchor_utc is not None
    if ref_norm is not None and ref_norm > anchor_utc:
        anchor_utc = ref_norm

    candidates: list[tuple[datetime, int, date]] = []

    if rule_enum is ReminderRule.ONCE:
        if once_date is None:
            raise ReminderScheduleError("once_date is required when rule is 'once'")
        for lead_day in normalized_leads:
            slot_utc = _local_slot_to_utc(
                once_date,
                lead_day=lead_day,
                fire_hour=fire_hour,
                fire_minute=fire_minute,
                tz=tz,
            )
            if _is_eligible(slot_utc):
                candidates.append((slot_utc, lead_day, once_date))
    else:
        if target_month is None or target_day is None:
            raise ReminderScheduleError(
                "target_month and target_day are required when rule is 'yearly'"
            )
        anchor_local = anchor_utc.astimezone(tz)
        if not is_lunar:
            # Validate month/day upfront even if a year lies near supported bounds
            resolve_solar_yearly_date(2024, target_month, target_day)
            for year_offset in (-1, 0, 1, 2):
                cand_year = anchor_local.year + year_offset
                if not (MIN_SUPPORTED_YEAR <= cand_year <= MAX_SUPPORTED_YEAR):
                    continue
                occ_date = resolve_solar_yearly_date(cand_year, target_month, target_day)
                for lead_day in normalized_leads:
                    slot_utc = _local_slot_to_utc(
                        occ_date,
                        lead_day=lead_day,
                        fire_hour=fire_hour,
                        fire_minute=fire_minute,
                        tz=tz,
                    )
                    if _is_eligible(slot_utc):
                        candidates.append((slot_utc, lead_day, occ_date))
        else:
            # Validate lunar month/day upfront
            resolve_lunar_yearly_date(2024, target_month, target_day, is_leap_month=is_leap_month)
            anchor_lunar_year = int(
                Solar.fromYmd(anchor_local.year, anchor_local.month, anchor_local.day)
                .getLunar()
                .getYear()
            )
            for year_offset in (-1, 0, 1, 2):
                cand_lunar_year = anchor_lunar_year + year_offset
                if not (MIN_SUPPORTED_YEAR <= cand_lunar_year <= MAX_SUPPORTED_YEAR):
                    continue
                occ_date = resolve_lunar_yearly_date(
                    cand_lunar_year,
                    target_month,
                    target_day,
                    is_leap_month=is_leap_month,
                )
                for lead_day in normalized_leads:
                    slot_utc = _local_slot_to_utc(
                        occ_date,
                        lead_day=lead_day,
                        fire_hour=fire_hour,
                        fire_minute=fire_minute,
                        tz=tz,
                    )
                    if _is_eligible(slot_utc):
                        candidates.append((slot_utc, lead_day, occ_date))

    if not candidates:
        return None

    # Sort by earliest UTC fire time, breaking ties in favor of larger lead_day first.
    candidates.sort(key=lambda item: (item[0], -item[1]))
    best_utc, best_lead, best_occ = candidates[0]
    return NextFireSlot(
        scheduled_for_utc=best_utc,
        lead_day=best_lead,
        occurrence_date=best_occ,
    )
