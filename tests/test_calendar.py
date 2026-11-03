from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcarisk.calendar import (
    SUPPORTED_YEARS,
    add_business_days,
    business_days_between,
    holidays,
    is_business_day,
    next_business_day,
    previous_business_day,
)

# Federal Reserve holiday schedule as published (observed dates), 2022-2027.
FED_HOLIDAYS = {
    2022: [
        "2022-01-17",
        "2022-02-21",
        "2022-05-30",
        "2022-06-20",
        "2022-07-04",
        "2022-09-05",
        "2022-10-10",
        "2022-11-11",
        "2022-11-24",
        "2022-12-26",
    ],  # Jan 1 = Sat -> not moved
    2023: [
        "2023-01-02",
        "2023-01-16",
        "2023-02-20",
        "2023-05-29",
        "2023-06-19",
        "2023-07-04",
        "2023-09-04",
        "2023-10-09",
        "2023-11-23",
        "2023-12-25",
    ],  # Nov 11 = Sat -> not moved
    2024: [
        "2024-01-01",
        "2024-01-15",
        "2024-02-19",
        "2024-05-27",
        "2024-06-19",
        "2024-07-04",
        "2024-09-02",
        "2024-10-14",
        "2024-11-11",
        "2024-11-28",
        "2024-12-25",
    ],
    2025: [
        "2025-01-01",
        "2025-01-20",
        "2025-02-17",
        "2025-05-26",
        "2025-06-19",
        "2025-07-04",
        "2025-09-01",
        "2025-10-13",
        "2025-11-11",
        "2025-11-27",
        "2025-12-25",
    ],
    2026: [
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-05-25",
        "2026-06-19",
        "2026-09-07",
        "2026-10-12",
        "2026-11-11",
        "2026-11-26",
        "2026-12-25",
    ],  # Jul 4 = Sat -> not moved
    2027: [
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-05-31",
        "2027-07-05",
        "2027-09-06",
        "2027-10-11",
        "2027-11-11",
        "2027-11-25",
    ],  # Jun 19 & Dec 25 = Sat; Jul 4 = Sun -> Mon
}


@pytest.mark.parametrize("year", list(SUPPORTED_YEARS))
def test_holiday_schedule_matches_fed(year):
    assert sorted(d.isoformat() for d in holidays(year)) == FED_HOLIDAYS[year]


@pytest.mark.parametrize(
    "d", ["2023-07-04", "2024-11-28", "2026-01-01", "2023-01-02", "2022-06-20", "2022-12-26", "2027-07-05"]
)
def test_known_holidays_are_not_business_days(d):
    assert not is_business_day(date.fromisoformat(d))


@pytest.mark.parametrize(
    "d", ["2026-07-03", "2027-12-24", "2027-06-18", "2021-12-31", "2023-11-10", "2023-01-03", "2024-11-29"]
)
def test_saturday_holidays_not_moved_to_friday(d):
    # Fed rule: a Saturday holiday is not observed on Friday, so these Fridays/days are business days.
    assert is_business_day(date.fromisoformat(d))


def test_sunday_new_year_2023_observed_monday():
    assert date(2023, 1, 1).weekday() == 6
    assert not is_business_day(date(2023, 1, 2))
    assert holidays(2023)[date(2023, 1, 2)] == "New Year's Day"


def test_weekends_are_not_business_days():
    assert not is_business_day(date(2024, 6, 1))  # Saturday
    assert not is_business_day(date(2024, 6, 2))  # Sunday


def test_add_business_days_over_weekend_and_holiday():
    assert add_business_days(date(2024, 11, 27), 1) == date(2024, 11, 29)  # skips Thanksgiving
    assert add_business_days(date(2023, 12, 29), 1) == date(2024, 1, 2)  # skips weekend + New Year
    assert add_business_days(date(2024, 1, 2), -1) == date(2023, 12, 29)
    assert add_business_days(date(2024, 6, 1), 0) == date(2024, 6, 1)
    assert add_business_days(date(2024, 5, 31), 1) == date(2024, 6, 3)


def test_business_days_between_convention():
    assert business_days_between(date(2024, 5, 31), date(2024, 6, 3)) == 1  # Fri -> Mon
    assert business_days_between(date(2024, 6, 3), date(2024, 5, 31)) == -1
    assert business_days_between(date(2024, 1, 2), date(2024, 1, 2)) == 0
    # 2024 has 366 days, 262 weekdays, 11 holidays all on weekdays -> 251 business days in (Dec 31, Dec 31]
    assert business_days_between(date(2023, 12, 31), date(2024, 12, 31)) == 251


def test_roll_helpers():
    assert next_business_day(date(2026, 7, 4)) == date(2026, 7, 6)
    assert previous_business_day(date(2026, 7, 4)) == date(2026, 7, 3)
    assert next_business_day(date(2026, 7, 6)) == date(2026, 7, 6)


def test_rejects_datetime():
    from datetime import datetime

    with pytest.raises(TypeError):
        is_business_day(datetime(2024, 1, 2))


dates = st.dates(min_value=date(2022, 1, 1), max_value=date(2027, 12, 31))


@given(dates, st.integers(min_value=-300, max_value=300))
def test_add_then_count_roundtrip(d, n):
    end = add_business_days(d, n)
    assert is_business_day(end) or n == 0
    if n >= 0 or is_business_day(d):
        # going backwards from a non-business day the half-open interval convention (end, start] loses one
        assert business_days_between(d, end) == n
    else:
        assert business_days_between(d, end) == n + 1


@given(dates, dates)
def test_between_matches_brute_force(a, b):
    lo, hi = min(a, b), max(a, b)
    brute = sum(is_business_day(lo + timedelta(days=i)) for i in range(1, (hi - lo).days + 1))
    assert business_days_between(lo, hi) == brute
    assert business_days_between(hi, lo) == -brute
