"""Pure-domain Chinese colloquial temporal intent parser for long-horizon journal search.

Strictly zero framework, database, HTTP, or ORM imports (enforced by `.importlinter`).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_SEASON_MONTHS: dict[str, tuple[int, int]] = {
    "春天": (3, 5),
    "春季": (3, 5),
    "夏天": (6, 8),
    "夏季": (6, 8),
    "秋天": (9, 11),
    "秋季": (9, 11),
    "冬天": (11, 2),
    "冬季": (11, 2),  # spans Nov..Feb across year boundary
}

_YEAR_SEASON_RE = re.compile(r"(20\d{2})年?(春天|春季|夏天|夏季|秋天|秋季|冬天|冬季)")
_REL_SEASON_RE = re.compile(r"(去年|今年|前年)(春天|春季|夏天|夏季|秋天|秋季|冬天|冬季)")
_YEAR_MONTH_RE = re.compile(r"(20\d{2})[-年/.](\d{1,2})月?")
_YEAR_RE = re.compile(r"(20\d{2})年?")


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _season_bounds(year: int, season_label: str) -> tuple[date, date]:
    m_start, m_end = _SEASON_MONTHS[season_label]
    if m_start <= m_end:
        return date(year, m_start, 1), _month_end(year, m_end)
    return date(year, m_start, 1), _month_end(year + 1, m_end)


def parse_temporal_intent(query: str, today: date) -> tuple[date | None, date | None]:
    """Extract soft temporal date window `(date_from, date_to)` from colloquial Chinese queries."""
    if not query:
        return None, None

    m_year_season = _YEAR_SEASON_RE.search(query)
    if m_year_season:
        yr = int(m_year_season.group(1))
        return _season_bounds(yr, m_year_season.group(2))

    m_rel_season = _REL_SEASON_RE.search(query)
    if m_rel_season:
        offset = {"今年": 0, "去年": -1, "前年": -2}[m_rel_season.group(1)]
        yr = today.year + offset
        return _season_bounds(yr, m_rel_season.group(2))

    m_ym = _YEAR_MONTH_RE.search(query)
    if m_ym:
        yr = int(m_ym.group(1))
        mo = int(m_ym.group(2))
        if 1 <= mo <= 12:
            return date(yr, mo, 1), _month_end(yr, mo)

    m_year = _YEAR_RE.search(query)
    if m_year:
        yr = int(m_year.group(1))
        return date(yr, 1, 1), date(yr, 12, 31)

    if "去年" in query:
        yr = today.year - 1
        return date(yr, 1, 1), date(yr, 12, 31)
    if "前年" in query:
        yr = today.year - 2
        return date(yr, 1, 1), date(yr, 12, 31)
    if "上个月" in query or "上月" in query:
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if "这个月" in query or "本月" in query:
        return today.replace(day=1), _month_end(today.year, today.month)

    # Standalone season (e.g. "妈妈冬天怕冷"): pick most recent winter/season covering or preceding today
    for label in _SEASON_MONTHS:
        if label in query:
            start_cur, end_cur = _season_bounds(today.year, label)
            if start_cur <= today:
                return start_cur, end_cur
            return _season_bounds(today.year - 1, label)

    return None, None
