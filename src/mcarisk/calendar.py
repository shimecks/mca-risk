"""Federal Reserve ACH business-day calendar (METRICS.md §0 "Business days").

A business day is a weekday (Mon-Fri) that is not a Federal Reserve holiday.

Federal Reserve holiday rules (the Fed's own observance, which differs from the federal-government rule):
  * Fixed-date holidays that fall on a **Sunday** are observed on the following **Monday**.
  * Fixed-date holidays that fall on a **Saturday** are **not** moved: the Fed is open the Friday before.
    (Saturday is not a business day anyway, so the holiday simply costs no business day.)

Holidays: New Year's Day (Jan 1), Martin Luther King Jr. Day (3rd Mon Jan), Washington's Birthday
(3rd Mon Feb), Memorial Day (last Mon May), Juneteenth (Jun 19; observed by the Fed from 2022),
Independence Day (Jul 4), Labor Day (1st Mon Sep), Columbus Day (2nd Mon Oct), Veterans Day (Nov 11),
Thanksgiving (4th Thu Nov), Christmas (Dec 25).

The rules are computed for any year (``SUPPORTED_YEARS`` is the range the tests pin down: 2022-2027).
All functions take and return ``datetime.date`` (a ``datetime`` is rejected to avoid silent time handling).
"""

from __future__ import annotations

import calendar as _cal
from datetime import date, datetime, timedelta
from functools import cache

SUPPORTED_YEARS = range(2022, 2028)
JUNETEENTH_FIRST_YEAR = 2022  # first year the Federal Reserve Banks closed for Juneteenth

_MON, _THU, _SUN = 0, 3, 6


def _check(d: object) -> date:
    if isinstance(d, datetime) or not isinstance(d, date):
        raise TypeError(f"expected datetime.date, got {type(d).__name__}")
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, _cal.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """Fed observance of a fixed-date holiday: Sunday -> Monday; Saturday -> not observed."""
    if d.weekday() == _SUN:
        return d + timedelta(days=1)
    if d.weekday() == 5:
        return None
    return d


def holidays(year: int) -> dict[date, str]:
    """Federal Reserve holidays *as observed* (business days lost) in ``year``: {date: name}."""
    return dict(_holidays(year))


@cache
def _holidays(year: int) -> dict[date, str]:
    fixed = {
        "New Year's Day": date(year, 1, 1),
        "Independence Day": date(year, 7, 4),
        "Veterans Day": date(year, 11, 11),
        "Christmas Day": date(year, 12, 25),
    }
    if year >= JUNETEENTH_FIRST_YEAR:
        fixed["Juneteenth"] = date(year, 6, 19)
    out: dict[date, str] = {}
    for name, d in fixed.items():
        obs = _observed(d)
        if obs is not None:
            out[obs] = name
    out[_nth_weekday(year, 1, _MON, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, _MON, 3)] = "Washington's Birthday"
    out[_last_weekday(year, 5, _MON)] = "Memorial Day"
    out[_nth_weekday(year, 9, _MON, 1)] = "Labor Day"
    out[_nth_weekday(year, 10, _MON, 2)] = "Columbus Day"
    out[_nth_weekday(year, 11, _THU, 4)] = "Thanksgiving Day"
    return dict(sorted(out.items()))


def is_holiday(d: date) -> bool:
    d = _check(d)
    return d in _holidays(d.year)


def is_business_day(d: date) -> bool:
    """True if ``d`` is a weekday and not a Federal Reserve holiday."""
    d = _check(d)
    return d.weekday() < 5 and d not in _holidays(d.year)


def add_business_days(d: date, n: int) -> date:
    """Move ``n`` business days from ``d`` (n may be negative).

    ``n == 0`` returns ``d`` unchanged even if ``d`` is not a business day; use
    :func:`next_business_day` / :func:`previous_business_day` to roll. For ``n > 0`` the result is the
    n-th business day strictly after ``d`` (e.g. ``add_business_days(friday, 1)`` is the next Monday,
    or Tuesday if that Monday is a holiday).
    """
    d = _check(d)
    if not isinstance(n, int):
        raise TypeError("n must be an int")
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if is_business_day(d):
            remaining -= 1
    return d


def next_business_day(d: date) -> date:
    """``d`` if it is a business day, otherwise the first business day after it."""
    d = _check(d)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def previous_business_day(d: date) -> date:
    """``d`` if it is a business day, otherwise the last business day before it."""
    d = _check(d)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def business_days_between(start: date, end: date) -> int:
    """Number of business days in the half-open interval ``(start, end]``.

    Equivalently: the ``n`` such that ``add_business_days(start, n) == end`` whenever ``end`` is a
    business day. Negative if ``end < start``; 0 if equal. Example: Friday -> next Monday is 1.
    METRICS.md D12 ("business days from funding to the as-of date") and D15 use this convention.
    """
    start, end = _check(start), _check(end)
    if end < start:
        return -business_days_between(end, start)
    count = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_business_day(d):
            count += 1
    return count
