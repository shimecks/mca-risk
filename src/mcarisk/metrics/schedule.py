"""Payment schedules on the Fed ACH calendar (METRICS.md §0 business days; D12, D15, D16).

METRICS.md does not spell out which days are *scheduled payment days*. This module fixes it, and the
SQL views read the same table (``cal_sched``), so Python and SQL agree by construction:

* ``daily`` deals: every business day strictly after the funding date.
* ``weekly`` deals: one payment day per calendar week, on weekday *w* = the weekday of the first
  business day after funding. If that day is a Fed holiday, the payment day is the last business day
  before it in the same week; if there is none (a Monday holiday on a Monday schedule), that week has no
  scheduled payment day.

``cal_sched`` has one row per (schedule code, calendar date): ``is_sched`` and ``n`` = the number of
scheduled days on or before the date. Codes: ``daily`` and ``w0`` .. ``w4`` (Mon .. Fri).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cache

from mcarisk.calendar import is_business_day

CAL_START = date(2022, 1, 1)
CAL_END = date(2027, 12, 31)
CODES = ("daily", "w0", "w1", "w2", "w3", "w4")


def _all_days() -> list[date]:
    n = (CAL_END - CAL_START).days + 1
    return [CAL_START + timedelta(days=i) for i in range(n)]


def _is_weekly_payday(d: date, w: int) -> bool:
    if not is_business_day(d):
        return False
    wd = d.weekday()
    if wd == w:
        return True
    if wd > w:
        return False
    # wd < w: d is the payday only if weekday w this week is a holiday and d is the last business day
    # before it in the same week
    return all(not is_business_day(d + timedelta(days=i)) for i in range(1, w - wd + 1))


@dataclass(frozen=True)
class CalIndex:
    """Precomputed lookups over ``CAL_START .. CAL_END``. Use :func:`cal_index` (cached)."""

    bd_dates: tuple[date, ...]
    sched: dict[str, tuple[date, ...]] = field(default_factory=dict)

    def _chk(self, d: date) -> None:
        if not (CAL_START <= d <= CAL_END):
            raise ValueError(f"{d} outside the schedule calendar {CAL_START}..{CAL_END}")

    def bd_idx(self, d: date) -> int:
        """Number of business days on or before ``d`` (within the calendar)."""
        self._chk(d)
        return bisect.bisect_right(self.bd_dates, d)

    def bdays_between(self, a: date, b: date) -> int:
        """Business days in ``(a, b]`` (same convention as ``calendar.business_days_between``)."""
        return self.bd_idx(b) - self.bd_idx(a)

    def add_bdays(self, d: date, n: int) -> date | None:
        """The n-th business day strictly after ``d`` (n >= 1); None if beyond the calendar."""
        i = self.bd_idx(d) + n - 1
        return self.bd_dates[i] if 0 <= i < len(self.bd_dates) else None

    def n_sched(self, code: str, d: date) -> int:
        self._chk(d)
        return bisect.bisect_right(self.sched[code], d)

    def nth_sched_after(self, code: str, d: date, k: int) -> date | None:
        """The k-th scheduled payment day strictly after ``d`` (k >= 1); None if beyond the calendar."""
        i = self.n_sched(code, d) + k - 1
        days = self.sched[code]
        return days[i] if i < len(days) else None


@cache
def cal_index() -> CalIndex:
    days = _all_days()
    bds = tuple(d for d in days if is_business_day(d))
    sched = {"daily": bds}
    for w in range(5):
        sched[f"w{w}"] = tuple(d for d in bds if _is_weekly_payday(d, w))
    return CalIndex(bd_dates=bds, sched=sched)


def sched_code(frequency: str | None, funding_date: date) -> str:
    """Schedule code for a deal: ``daily`` or ``w<weekday of the first business day after funding>``."""
    if frequency == "weekly":
        first = cal_index().add_bdays(funding_date, 1)
        if first is None:
            raise ValueError(f"funding date {funding_date} too late for the schedule calendar")
        return f"w{first.weekday()}"
    return "daily"


def cal_sched_rows() -> list[dict]:
    """Rows of the ``cal_sched`` table (written by the ledger stage, read by the SQL views)."""
    ci = cal_index()
    out = []
    days = _all_days()
    for code in CODES:
        s = set(ci.sched[code])
        n = 0
        for d in days:
            hit = d in s
            n += hit
            out.append({"code": code, "d": d, "is_sched": hit, "n": n})
    return out
