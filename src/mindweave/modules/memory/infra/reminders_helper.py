"""Reminder derivation and scheduling helper for `date` and `promise` facts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from lunar_python import Lunar, LunarMonth, LunarYear, Solar
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mindweave.core.crypto import EnvelopeCipher
from mindweave.core.dto import strip_forbidden_dev_jargon
from mindweave.core.ids import new_id
from mindweave.core.orm import FactRow, ReminderRow
from mindweave.shared.enums import FactType, ReminderRule, ReminderStatus
from mindweave.shared.scopes import UserScope

_YEARLY_KEYWORDS = ("生日", "纪念日", "周年", "诞辰", "母亲节", "父亲节", "结婚")


def infer_reminder_rule(fact_type: str, content: str, *, is_lunar: bool) -> ReminderRule:
    if fact_type == FactType.DATE.value and (
        is_lunar or any(kw in content for kw in _YEARLY_KEYWORDS)
    ):
        return ReminderRule.YEARLY
    return ReminderRule.ONCE


def _resolve_next_lunar_solar_date(
    *,
    lunar_month: int,
    lunar_day: int,
    is_leap_month: bool,
    today_local: date,
) -> date | None:
    if not (1 <= lunar_month <= 12 and 1 <= lunar_day <= 30):
        return None
    base_lunar_year = (
        Solar.fromYmd(today_local.year, today_local.month, today_local.day).getLunar().getYear()
    )
    for year_offset in range(-1, 5):
        ly = base_lunar_year + year_offset
        try:
            ly_obj = LunarYear.fromYear(ly)
            leap_m = int(ly_obj.getLeapMonth())
            month_arg = -lunar_month if (is_leap_month and leap_m == lunar_month) else lunar_month
            lm = LunarMonth.fromYm(ly, month_arg)
            if lm is None:
                lm = LunarMonth.fromYm(ly, lunar_month)
            if lm is None:
                continue
            clamped_day = min(lunar_day, int(lm.getDayCount()))
            solar = Lunar.fromYmd(ly, int(lm.getMonth()), clamped_day).getSolar()
            cand = date(solar.getYear(), solar.getMonth(), solar.getDay())
            if cand >= today_local:
                return cand
        except ValueError:
            continue
    return None


def _resolve_next_solar_yearly_date(*, month: int, day: int, today_local: date) -> date | None:
    for year_offset in range(0, 5):
        yr = today_local.year + year_offset
        try:
            cand = date(yr, month, day)
        except ValueError:
            # Handle Feb 29 in non-leap years -> Feb 28
            if month == 2 and day == 29:
                cand = date(yr, 2, 28)
            else:
                return None
        if cand >= today_local:
            return cand
    return None


def compute_next_fire_slot(
    *,
    rule: ReminderRule,
    once_date: date | None,
    is_lunar: bool,
    target_month: int | None,
    target_day: int | None,
    is_leap_month: bool,
    lead_days: Sequence[int],
    fire_hour: int,
    fire_minute: int,
    tz_name: str,
    now_utc: datetime,
) -> tuple[datetime | None, int | None]:
    """Compute `(next_fire_at_utc, next_lead_day)` for a reminder."""
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    today_local = local_now.date()

    event_date: date | None = None
    if rule is ReminderRule.ONCE:
        if once_date is not None:
            event_date = once_date
        elif is_lunar and target_month is not None and target_day is not None:
            event_date = _resolve_next_lunar_solar_date(
                lunar_month=target_month,
                lunar_day=target_day,
                is_leap_month=is_leap_month,
                today_local=today_local,
            )
    else:
        if is_lunar and target_month is not None and target_day is not None:
            event_date = _resolve_next_lunar_solar_date(
                lunar_month=target_month,
                lunar_day=target_day,
                is_leap_month=is_leap_month,
                today_local=today_local,
            )
        elif target_month is not None and target_day is not None:
            event_date = _resolve_next_solar_yearly_date(
                month=target_month,
                day=target_day,
                today_local=today_local,
            )
        elif once_date is not None:
            event_date = _resolve_next_solar_yearly_date(
                month=once_date.month,
                day=once_date.day,
                today_local=today_local,
            )

    if event_date is None:
        return None, None

    sorted_leads = sorted({max(0, int(d)) for d in lead_days}, reverse=True) or [0]
    for lead in sorted_leads:
        fire_day = event_date - timedelta(days=lead)
        fire_local = datetime.combine(fire_day, time(fire_hour, fire_minute), tzinfo=tz)
        fire_utc = fire_local.astimezone(UTC)
        if fire_utc >= now_utc:
            return fire_utc, lead

    # If event_date is today (after fire_hour), schedule 5 minutes from now so user isn't missed
    if event_date == today_local:
        return now_utc + timedelta(minutes=5), 0

    # If yearly and all slots this year passed, advance to next year
    if rule is ReminderRule.YEARLY:
        next_day = today_local + timedelta(days=1)
        return compute_next_fire_slot(
            rule=rule,
            once_date=once_date,
            is_lunar=is_lunar,
            target_month=target_month,
            target_day=target_day,
            is_leap_month=is_leap_month,
            lead_days=lead_days,
            fire_hour=fire_hour,
            fire_minute=fire_minute,
            tz_name=tz_name,
            now_utc=datetime.combine(next_day, time(0, 0), tzinfo=tz).astimezone(UTC),
        )

    return None, None


def fact_has_schedulable_date(fact: FactRow) -> bool:
    if fact.type not in (FactType.DATE.value, FactType.PROMISE.value):
        return False
    if fact.trigger_date is not None:
        return True
    return bool(fact.is_lunar and fact.lunar_month is not None and fact.lunar_day is not None)


async def upsert_reminder_for_fact(
    session: AsyncSession,
    *,
    scope: UserScope,
    dek: bytes,
    cipher: EnvelopeCipher,
    fact: FactRow,
    fact_content: str,
    desired_status: ReminderStatus,
    now: datetime,
    tz_name: str = "Asia/Shanghai",
) -> ReminderRow | None:
    """Create or update a `ReminderRow` derived from a `date` or `promise` fact."""
    if fact.person_id is None:
        return None
    if fact.type not in (FactType.DATE.value, FactType.PROMISE.value):
        return None

    has_date = fact_has_schedulable_date(fact)
    existing = await session.scalar(
        select(ReminderRow).where(
            ReminderRow.user_id == scope.user_id,
            ReminderRow.fact_id == fact.id,
        )
    )
    local_today = now.astimezone(ZoneInfo(tz_name)).date()
    # When capture is unconfirmed (`proposed`), only create reminder when date/lunar is present;
    # when confirmed (`active`), create/update if date/lunar is present (or fallback +1 day for promise without explicit date).
    effective_trigger_date = fact.trigger_date
    if not has_date:
        if desired_status is ReminderStatus.PROPOSED:
            return existing
        # Confirmed promise/date without explicit date defaults to tomorrow 09:00 so user gets reminded
        effective_trigger_date = local_today + timedelta(days=1)

    rule = infer_reminder_rule(fact.type, fact_content, is_lunar=fact.is_lunar)
    target_month: int | None = None
    target_day: int | None = None
    once_date: date | None = None

    if fact.is_lunar and fact.lunar_month is not None and fact.lunar_day is not None:
        target_month = fact.lunar_month
        target_day = fact.lunar_day
        if rule is ReminderRule.ONCE:
            once_date = effective_trigger_date or _resolve_next_lunar_solar_date(
                lunar_month=fact.lunar_month,
                lunar_day=fact.lunar_day,
                is_leap_month=fact.lunar_is_leap_month,
                today_local=local_today,
            )
            if once_date is None:
                once_date = local_today + timedelta(days=1)
    elif effective_trigger_date is not None:
        if rule is ReminderRule.YEARLY:
            target_month = effective_trigger_date.month
            target_day = effective_trigger_date.day
        else:
            once_date = effective_trigger_date
            target_month = effective_trigger_date.month
            target_day = effective_trigger_date.day

    lead_days = [7, 1, 0] if fact.type == FactType.DATE.value else [1, 0]
    next_fire_at, next_lead_day = compute_next_fire_slot(
        rule=rule,
        once_date=once_date,
        is_lunar=fact.is_lunar,
        target_month=target_month,
        target_day=target_day,
        is_leap_month=fact.lunar_is_leap_month,
        lead_days=lead_days,
        fire_hour=9,
        fire_minute=0,
        tz_name=tz_name,
        now_utc=now,
    )
    clean_title = strip_forbidden_dev_jargon(fact_content)[:80] or "重要事项提醒"

    if existing is not None:
        existing.person_id = fact.person_id
        existing.title_enc = cipher.encrypt_str(
            dek,
            clean_title,
            user_id=scope.user_id,
            table="reminders",
            column="title_enc",
            row_id=existing.id,
        )
        existing.rule = rule.value
        existing.is_lunar = fact.is_lunar
        existing.target_month = target_month
        existing.target_day = target_day
        existing.is_leap_month = fact.lunar_is_leap_month
        existing.once_date = once_date
        existing.lead_days = lead_days
        existing.timezone = tz_name
        existing.next_fire_at = next_fire_at
        existing.next_lead_day = next_lead_day
        existing.status = desired_status.value
        existing.updated_at = now
        existing.version += 1
        await session.flush()
        return existing

    rem_id = new_id()
    title_enc = cipher.encrypt_str(
        dek,
        clean_title,
        user_id=scope.user_id,
        table="reminders",
        column="title_enc",
        row_id=rem_id,
    )
    row = ReminderRow(
        id=rem_id,
        user_id=scope.user_id,
        person_id=fact.person_id,
        fact_id=fact.id,
        title_enc=title_enc,
        rule=rule.value,
        is_lunar=fact.is_lunar,
        target_month=target_month,
        target_day=target_day,
        is_leap_month=fact.lunar_is_leap_month,
        once_date=once_date,
        fire_hour=9,
        fire_minute=0,
        lead_days=lead_days,
        timezone=tz_name,
        next_fire_at=next_fire_at,
        next_lead_day=next_lead_day,
        status=desired_status.value,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def cancel_reminders_for_facts(
    session: AsyncSession,
    *,
    user_id: UUID,
    fact_ids: Sequence[UUID],
    now: datetime,
) -> int:
    if not fact_ids:
        return 0
    rows = (
        await session.scalars(
            select(ReminderRow).where(
                ReminderRow.user_id == user_id,
                ReminderRow.fact_id.in_(list(fact_ids)),
                ReminderRow.status.in_(
                    [ReminderStatus.PROPOSED.value, ReminderStatus.ACTIVE.value]
                ),
            )
        )
    ).all()
    for r in rows:
        r.status = ReminderStatus.CANCELLED.value
        r.updated_at = now
        r.version += 1
    if rows:
        await session.flush()
    return len(rows)
