"""Parsers (mcarisk.parse): unit tests on the variants seen in raw/, plus hypothesis properties."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcarisk import parse as P
from mcarisk.mapping import DERIVATIONS

F = P.ParseFailure


# ------------------------------------------------------------------------------------------- money


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("$38,500.00", 3_850_000),  # 2023 tracker, dollar style
        ("$60,500", 6_050_000),  # 2023 tracker, whole-dollar style
        ("$410.67", 41_067),
        ("$1,150", 115_000),
        ("102000", 10_200_000),  # xlsx int
        ("301.5", 30_150),  # xlsx float repr
        ("2929.69", 292_969),
        ("$4,984", 498_400),  # scorecard Avg Mo Rev
        ("$54,744.44", 5_474_444),  # collections Balance
        ("0", 0),
        ("  $1,200.00  ", 120_000),
        ("12.500", 1_250),  # extra zero decimals are exact
    ],
)
def test_money_variants(text, cents):
    assert P.parse_money(text) == cents


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("-37300.0", -3_730_000),
        ("410.67", 41_067),
        ("-410.67", -41_067),
        ("($1,000.00)", -100_000),
        ("$-5", -500),
    ],
)
def test_money_signed_when_allowed(text, cents):
    assert P.parse_money(text, allow_negative=True) == cents


@pytest.mark.parametrize("text", ["-37300.0", "($1,000.00)", "-$5", "$-5"])
def test_money_negative_is_a_failure_by_default(text):
    assert P.parse_money(text) == F("money_negative", text)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("abc", "money_garbage"),
        ("$1,23", "money_garbage"),  # bad thousands grouping
        ("1,2345.00", "money_garbage"),
        ("$12.3.4", "money_garbage"),
        ("$", "money_garbage"),
        ("(-5)", "money_garbage"),  # two sign markers
        ("410.666", "money_sub_cent"),  # never rounded away
        ("$2,812.14 wkly", "money_garbage"),  # suffixes belong to parse_payment
        ("1e5", "money_garbage"),
    ],
)
def test_money_garbage(text, reason):
    r = P.parse_money(text)
    assert isinstance(r, F) and r.reason == reason


@pytest.mark.parametrize("text", [None, "", "   "])
def test_money_missing_is_none(text):
    assert P.parse_money(text) is None


def _fmt_dollar(c: int) -> str:
    return f"${c // 100:,}.{c % 100:02d}"


@settings(max_examples=400)
@given(st.integers(min_value=0, max_value=10**12))
def test_money_round_trips(c):
    assert P.parse_money(_fmt_dollar(c)) == c
    assert P.parse_money(f"{c // 100}.{c % 100:02d}") == c
    assert P.parse_money(repr(c / 100)) == c  # how ingest renders an xlsx float
    if c % 100 == 0:
        assert P.parse_money(f"${c // 100:,}") == c
        assert P.parse_money(str(c // 100)) == c


@settings(max_examples=300)
@given(st.integers(min_value=1, max_value=10**12))
def test_money_never_parses_negatives_silently(c):
    for s in (f"-{_fmt_dollar(c)}", f"({_fmt_dollar(c)})", f"-{c // 100}.{c % 100:02d}", f"$-{c / 100}"):
        r = P.parse_money(s)
        assert isinstance(r, F) and r.reason == "money_negative", s
        assert P.parse_money(s, allow_negative=True) == -c, s


@settings(max_examples=500)
@given(st.text(max_size=20))
def test_money_on_arbitrary_text_is_never_a_silent_negative(s):
    r = P.parse_money(s)
    assert r is None or isinstance(r, F) or (isinstance(r, int) and r >= 0)


# ----------------------------------------------------------------------------------------- numbers


@pytest.mark.parametrize(("text", "v"), [("1150", 1150), ("1150.0", 1150), (" 7 ", 7), ("0", 0)])
def test_int_variants(text, v):
    assert P.parse_int(text) == v


@pytest.mark.parametrize(
    ("text", "reason"),
    [("1150.5", "int_fractional"), ("-3", "int_negative"), ("x", "int_garbage"), ("1,150", "int_garbage")],
)
def test_int_failures(text, reason):
    assert P.parse_int(text).reason == reason


# ----------------------------------------------------------------------------------------- rates


@pytest.mark.parametrize(
    ("text", "unit", "frac"),
    [
        ("9.9%", P.RATE_FRACTION, Decimal("0.099")),  # 2023: the % sign wins over the declared unit
        ("9.9%", P.RATE_PERCENT, Decimal("0.099")),
        ("0.109", P.RATE_FRACTION, Decimal("0.109")),  # 2024 Comm
        ("0", P.RATE_FRACTION, Decimal("0")),
        ("10.1", P.RATE_PERCENT, Decimal("0.101")),  # 2025 iso_comm_pct
        ("3", P.RATE_PERCENT, Decimal("0.03")),
    ],
)
def test_rate_percent_vs_fraction(text, unit, frac):
    assert P.parse_rate(text, unit=unit) == frac


def test_rate_out_of_range_and_garbage():
    assert (
        P.parse_rate("10.1", unit=P.RATE_FRACTION, max_fraction=Decimal("0.5")).reason == "rate_out_of_range"
    )
    assert P.parse_rate("abc%", unit=P.RATE_PERCENT).reason == "rate_garbage"
    assert P.parse_rate("-1%", unit=P.RATE_PERCENT).reason == "rate_garbage"


@given(st.integers(min_value=0, max_value=10_000))
def test_rate_units_agree(tenth_bps):
    frac = Decimal(tenth_bps) / 100_000  # 0 .. 0.1 in steps of 0.001 bp
    pct = frac * 100
    a = P.parse_rate(f"{pct}%", unit=P.RATE_FRACTION)
    b = P.parse_rate(str(pct), unit=P.RATE_PERCENT)
    c = P.parse_rate(str(frac), unit=P.RATE_FRACTION)
    assert a == b == c == frac


# ---------------------------------------------------------------------------------------- factor


@pytest.mark.parametrize(
    ("text", "v"), [("1.35x", "1.35"), ("1.30x", "1.30"), ("1.28", "1.28"), ("1.3", "1.3"), ("1.4 X", "1.4")]
)
def test_factor_variants(text, v):
    assert P.parse_factor(text) == Decimal(v)


@pytest.mark.parametrize(
    ("text", "reason"),
    [("0.9", "factor_out_of_range"), ("2.5x", "factor_out_of_range"), ("x", "factor_garbage")],
)
def test_factor_failures(text, reason):
    assert P.parse_factor(text).reason == reason


# ----------------------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("text", "d", "fmt"),
    [
        ("01/19/23", date(2023, 1, 19), P.DATE_FMT_MDY2),  # 2023 tracker
        ("06/28/2024", date(2024, 6, 28), P.DATE_FMT_MDY4),  # ACH before the July 2024 switch
        ("2024-06-28", date(2024, 6, 28), P.DATE_FMT_ISO),  # ACH from July 2024; 2025 tracker; scorecards
        ("2024-01-02", date(2024, 1, 2), P.DATE_FMT_ISO),  # real Excel date rendered by ingest
        ("2024-01-02T00:00:00", date(2024, 1, 2), P.DATE_FMT_ISO),
        ("1/2/2024", date(2024, 1, 2), P.DATE_FMT_MDY4),
        ("2024/01/02", date(2024, 1, 2), P.DATE_FMT_ISO),
        ("45293", date(2024, 1, 2), P.DATE_FMT_SERIAL),  # Excel serial
        ("45293.0", date(2024, 1, 2), P.DATE_FMT_SERIAL),
    ],
)
def test_date_variants(text, d, fmt):
    assert P.parse_date_fmt(text) == (d, fmt)
    assert P.parse_date(text) == d


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("13/01/2024", "date_invalid"),  # day-first is never silently swapped
        ("02/30/2024", "date_invalid"),
        ("2024-06-28T13:45:00", "date_has_time"),
        ("1150", "date_unrecognized"),  # a deal number is not a date
        ("99999", "date_serial_out_of_range"),
        ("June 28", "date_unrecognized"),
    ],
)
def test_date_failures(text, reason):
    assert P.parse_date(text).reason == reason


@settings(max_examples=400)
@given(st.dates(min_value=date(1990, 1, 1), max_value=date(2099, 12, 31)))
def test_date_parser_handles_every_format(d):
    forms = [
        d.strftime("%m/%d/%Y"),
        d.isoformat(),
        d.isoformat() + "T00:00:00",
        f"{d.month}/{d.day}/{d.year}",
        d.strftime("%Y/%m/%d"),
        str((d - P.EXCEL_EPOCH).days),
    ]
    if 1970 <= d.year <= 2069:  # two-digit years pivot at 70
        forms.append(d.strftime("%m/%d/%y"))
    for s in forms:
        assert P.parse_date(s) == d, s


def test_excel_serial_matches_excel_epoch():
    assert P.parse_date("45292") == date(2024, 1, 1)
    assert P.EXCEL_EPOCH + timedelta(days=45292) == date(2024, 1, 1)


# ----------------------------------------------------------------------------- frequency / term / payment


@pytest.mark.parametrize(
    ("text", "f"),
    [
        ("D", "daily"),
        ("Daily", "daily"),
        ("daily", "daily"),
        ("W", "weekly"),
        ("Weekly", "weekly"),
        ("wkly", "weekly"),
        ("wk", "weekly"),
    ],
)
def test_frequency_variants(text, f):
    assert P.parse_frequency(text) == f


def test_frequency_unknown():
    assert P.parse_frequency("monthly").reason == "frequency_unrecognized"


@pytest.mark.parametrize(
    ("text", "bdays", "hint"),
    [
        ("120", 120, None),
        ("24 wks", 120, "weekly"),
        ("16 wks", 80, "weekly"),
        ("1 wk", 5, "weekly"),
        ("60 days", 60, None),
        ("100.0", 100, None),
    ],
)
def test_term_variants(text, bdays, hint):
    assert P.parse_term(text) == P.Term(bdays, hint)


@pytest.mark.parametrize(
    ("text", "reason"),
    [("0", "term_not_positive"), ("6 mo", "term_unrecognized"), ("abc", "term_unrecognized")],
)
def test_term_failures(text, reason):
    assert P.parse_term(text).reason == reason


@given(st.integers(min_value=1, max_value=200))
def test_weeks_are_five_business_days(n):
    assert P.parse_term(f"{n} wks").bdays == 5 * n


@pytest.mark.parametrize(
    ("text", "pay"),
    [
        ("$410.67", P.Payment(41_067, None, True)),
        ("$2,812.14 wkly", P.Payment(281_214, "weekly", True)),
        ("$3,964 wkly", P.Payment(396_400, "weekly", False)),  # whole-dollar: cents unknown
        ("$1,150", P.Payment(115_000, None, False)),
        ("301.5", P.Payment(30_150, None, True)),
        ("6630", P.Payment(663_000, None, False)),
        ("$500/day", P.Payment(50_000, "daily", False)),
        ("$1,000.00 per week", P.Payment(100_000, "weekly", True)),
    ],
)
def test_payment_variants(text, pay):
    assert P.parse_payment(text) == pay


@pytest.mark.parametrize(
    ("text", "reason"),
    [("-$5 wkly", "payment_negative"), ("wkly", "payment_garbage"), ("$4.105 wkly", "payment_sub_cent")],
)
def test_payment_failures(text, reason):
    assert P.parse_payment(text).reason == reason


def test_installment_matches_tracker_rows():
    # deal 1001: RTR $49,280 over 120 business days -> $410.67 daily
    assert P.installment_cents(4_928_000, 120, "daily") == 41_067
    # deal 1123 (2024): RTR $78,740, weekly $2,812.14 -> 140 business days
    assert P.installment_cents(7_874_000, 140, "weekly") == 281_214


@settings(max_examples=300)
@given(
    st.integers(min_value=100_000, max_value=50_000_000),
    st.integers(min_value=20, max_value=400),
    st.sampled_from(["daily", "weekly"]),
)
def test_term_derivation_round_trips(rtr, term, freq):
    pay = P.installment_cents(rtr, term, freq)
    got, err = DERIVATIONS["rtr_over_payment"]({"rtr_cents": rtr, "payment_cents": pay, "frequency": freq})
    if err is None:
        assert P.installment_cents(rtr, got, freq) == pay  # never accepts an inexact term
    else:
        assert err == "term_underivable_no_exact_match"


# ---------------------------------------------------------------------------------- ids and lookups


def test_ein_and_state():
    assert P.parse_ein("20-8752044") == "20-8752044"
    assert P.parse_ein("208752044") == "20-8752044"
    assert P.parse_ein("20-875204").reason == "ein_invalid"
    assert P.parse_state(" ny ") == "NY"
    assert P.parse_state("New York").reason == "state_invalid"


def test_text_is_verbatim():
    assert P.parse_text("  Harbor Bistro LLC ") == "  Harbor Bistro LLC "
    assert P.parse_text("   ") is None


def test_lookup_is_case_and_space_insensitive():
    table = {P.norm_lookup_key("restaurant/bar"): "Restaurant"}
    assert P.parse_lookup("Restaurant/Bar ", table, reason="industry_unmapped") == "Restaurant"
    assert P.parse_lookup("bakery", table, reason="industry_unmapped") == F("industry_unmapped", "bakery")
