"""Field parsers for Part 2 (standardize). Pure functions: text in, canonical value out.

Contract shared by every parser
-------------------------------
* Input is the raw cell text from a ``raw_*`` table (``str``) or ``None``. Ingest renders xlsx numbers as
  ``str(int)`` / ``repr(float)`` and real Excel dates as ISO text (see ``mcarisk.ingest``).
* ``None`` or whitespace-only text returns ``None``: the value is *missing*, which is not a failure.
* Anything that cannot be read unambiguously returns a :class:`ParseFailure` carrying a short reason
  code. Parsers never guess, never round away information and never return a negative number unless
  the caller passed ``allow_negative=True``. Callers decide whether a failure rejects the row or nulls
  the field; either way it is logged (METRICS.md Q7).

Canonical units (CLAUDE.md, METRICS.md §0)
------------------------------------------
* money       -> ``int`` cents (exact; ``Decimal`` arithmetic, no floats)
* dates       -> ``datetime.date``
* rates       -> ``Decimal`` fraction (``9.9%`` and ``0.099`` both give ``Decimal('0.099')``)
* factor      -> ``Decimal`` (``1.35x`` -> ``Decimal('1.35')``)
* frequency   -> ``"daily"`` | ``"weekly"``
* term        -> ``int`` **business days** of the contract = number of daily-equivalent remittance days.
  Weekly terms are converted at 5 business days per week (``24 wks`` -> 120). This is the unit of the
  trackers' ``Term (days)`` / ``term`` columns and of METRICS.md's daily equivalent (weekly payment / 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
BDAYS_PER_WEEK = 5
EXCEL_EPOCH = date(1899, 12, 30)  # Excel 1900 system, valid for serials >= 61 (after the fake 1900-02-29)
SERIAL_MIN, SERIAL_MAX = 32874, 73050  # 1990-01-01 .. 2100-01-01: plausible business dates only


@dataclass(frozen=True)
class ParseFailure:
    """Typed failure: ``reason`` is a short snake_case code, ``raw`` the offending text."""

    reason: str
    raw: str | None = None


def is_failure(v: Any) -> bool:
    return isinstance(v, ParseFailure)


def _clean(text: Any) -> str | None:
    if text is None:
        return None
    s = str(text).strip()
    return s or None


# ------------------------------------------------------------------------------------------- numbers

_NUM_BODY = re.compile(r"^(?P<int>\d{1,3}(?:,\d{3})+|\d+)?(?:\.(?P<frac>\d+))?$")


def _decimal(body: str, raw: str) -> Decimal | ParseFailure:
    m = _NUM_BODY.match(body)
    if not m or (m.group("int") is None and m.group("frac") is None):
        return ParseFailure("not_a_number", raw)
    try:
        return Decimal(body.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the regex already excludes this
        return ParseFailure("not_a_number", raw)


def _split_sign(s: str) -> tuple[bool, str] | None:
    """Strip one sign marker: leading ``-``/``+`` (before or after ``$``) or accounting parentheses."""
    neg, markers = False, 0
    if s.startswith("(") and s.endswith(")"):
        neg, s, markers = True, s[1:-1].strip(), 1
    if s.startswith(("+", "-")):
        neg, s, markers = neg or s[0] == "-", s[1:].strip(), markers + 1
    if s.startswith("$"):
        s = s[1:].strip()
        if s.startswith(("+", "-")):
            neg, s, markers = neg or s[0] == "-", s[1:].strip(), markers + 1
    if markers > 1:
        return None  # "(-5)", "-$-5": more than one sign marker is not a number we can trust
    return neg, s


def parse_money(text: Any, *, allow_negative: bool = False) -> int | ParseFailure | None:
    """``"$38,500.00"`` / ``"38500"`` / ``"409.6"`` / ``"-37300.0"`` -> integer cents.

    Accepts an optional ``$``, thousands separators in valid groups of three, at most 2 decimals (more
    only if the extra digits are zeros), a leading sign or accounting parentheses. Negative amounts are
    a failure (``money_negative``) unless ``allow_negative``. Sub-cent amounts are a failure
    (``money_sub_cent``), never rounded.
    """
    s = _clean(text)
    if s is None:
        return None
    split = _split_sign(s)
    if split is None:
        return ParseFailure("money_garbage", s)
    neg, body = split
    d = _decimal(body, s)
    if isinstance(d, ParseFailure):
        return ParseFailure("money_garbage", s)
    cents = d * 100
    if cents != cents.to_integral_value():
        return ParseFailure("money_sub_cent", s)
    value = int(cents)
    if neg and value != 0:
        if not allow_negative:
            return ParseFailure("money_negative", s)
        value = -value
    return value


def money_has_cents(text: Any) -> bool:
    """True if the text states cents explicitly (``$410.67``); False for whole-dollar text (``$411``)."""
    s = _clean(text)
    return bool(s) and re.search(r"\.\d", s) is not None


def parse_int(text: Any, *, allow_negative: bool = False) -> int | ParseFailure | None:
    """``"1150"`` / ``"1150.0"`` (an Excel float that is integral) -> 1150. Fractions are failures."""
    s = _clean(text)
    if s is None:
        return None
    neg = s.startswith("-")
    body = s[1:] if s.startswith(("+", "-")) else s
    d = _decimal(body, s)
    if isinstance(d, ParseFailure) or "," in body:
        return ParseFailure("int_garbage", s)
    if d != d.to_integral_value():
        return ParseFailure("int_fractional", s)
    v = int(d)
    if neg and v != 0:
        if not allow_negative:
            return ParseFailure("int_negative", s)
        v = -v
    return v


def parse_decimal(text: Any, *, allow_negative: bool = False) -> Decimal | ParseFailure | None:
    """Plain non-negative decimal number (``"6.4"``, ``"7"``)."""
    s = _clean(text)
    if s is None:
        return None
    neg = s.startswith("-")
    body = s[1:] if s.startswith(("+", "-")) else s
    d = _decimal(body, s)
    if isinstance(d, ParseFailure):
        return ParseFailure("decimal_garbage", s)
    if neg and d != 0:
        if not allow_negative:
            return ParseFailure("decimal_negative", s)
        d = -d
    return d


# ------------------------------------------------------------------------------- rates and factors

RATE_PERCENT = "percent"
RATE_FRACTION = "fraction"


def parse_rate(
    text: Any, *, unit: str, max_fraction: Decimal = Decimal("1")
) -> Decimal | ParseFailure | None:
    """Percent vs fraction -> fraction ``Decimal``.

    A trailing ``%`` always means percent (``"9.9%"`` -> 0.099). A bare number is read in ``unit``
    (``"percent"``: ``10.1`` -> 0.101; ``"fraction"``: ``0.109`` -> 0.109) as declared by the column's
    mapping, because a bare ``1`` is ambiguous (1% or 100%) and must not be guessed. Results outside
    ``[0, max_fraction]`` fail with ``rate_out_of_range``.
    """
    s = _clean(text)
    if s is None:
        return None
    pct = s.endswith("%")
    body = s[:-1].strip() if pct else s
    d = parse_decimal(body)
    if d is None or isinstance(d, ParseFailure):
        return ParseFailure("rate_garbage", s)
    if pct or unit == RATE_PERCENT:
        d = d / 100
    elif unit != RATE_FRACTION:
        raise ValueError(f"unknown rate unit {unit!r}")
    if not (0 <= d <= max_fraction):
        return ParseFailure("rate_out_of_range", s)
    return d.normalize() if d != 0 else Decimal(0)


FACTOR_MIN, FACTOR_MAX = Decimal("1.0"), Decimal("2.0")


def parse_factor(text: Any) -> Decimal | ParseFailure | None:
    """``"1.35x"`` / ``"1.35 X"`` / ``"1.35"`` -> ``Decimal('1.35')``; must lie in [1.0, 2.0]."""
    s = _clean(text)
    if s is None:
        return None
    body = re.sub(r"\s*[xX]$", "", s)
    d = parse_decimal(body)
    if d is None or isinstance(d, ParseFailure):
        return ParseFailure("factor_garbage", s)
    if not (FACTOR_MIN <= d <= FACTOR_MAX):
        return ParseFailure("factor_out_of_range", s)
    return d


# ------------------------------------------------------------------------------------------- dates

DATE_FMT_ISO = "YYYY-MM-DD"
DATE_FMT_MDY4 = "MM/DD/YYYY"
DATE_FMT_MDY2 = "MM/DD/YY"
DATE_FMT_SERIAL = "excel_serial"

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]00:00(?::00(?:\.0+)?)?)?$")
_ISO_TIME = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$")
_YMD_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_MDY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|\d{4})$")
_SERIAL = re.compile(r"^(\d{5})(?:\.0+)?$")


def _mk(y: int, m: int, d: int, raw: str) -> date | ParseFailure:
    try:
        return date(y, m, d)
    except ValueError:
        return ParseFailure("date_invalid", raw)


def parse_date_fmt(text: Any) -> tuple[date, str] | ParseFailure | None:
    """Like :func:`parse_date` but also returns which format matched (for lineage and the L2 trap)."""
    s = _clean(text)
    if s is None:
        return None
    if m := _ISO.match(s):
        r = _mk(int(m[1]), int(m[2]), int(m[3]), s)
        return r if isinstance(r, ParseFailure) else (r, DATE_FMT_ISO)
    if _ISO_TIME.match(s):
        # a timestamp with a non-midnight time: a date column should not carry one; don't drop it silently
        return ParseFailure("date_has_time", s)
    if m := _YMD_SLASH.match(s):
        r = _mk(int(m[1]), int(m[2]), int(m[3]), s)
        return r if isinstance(r, ParseFailure) else (r, DATE_FMT_ISO)
    if m := _MDY.match(s):
        mo, dd, yy = int(m[1]), int(m[2]), m[3]
        if len(yy) == 2:
            y, fmt = (2000 + int(yy) if int(yy) < 70 else 1900 + int(yy)), DATE_FMT_MDY2
        else:
            y, fmt = int(yy), DATE_FMT_MDY4
        # US month-first only. A day-first value (13/01/2024) is invalid here, not silently swapped.
        r = _mk(y, mo, dd, s)
        return r if isinstance(r, ParseFailure) else (r, fmt)
    if m := _SERIAL.match(s):
        n = int(m[1])
        if not (SERIAL_MIN <= n <= SERIAL_MAX):
            return ParseFailure("date_serial_out_of_range", s)
        return EXCEL_EPOCH + timedelta(days=n), DATE_FMT_SERIAL
    return ParseFailure("date_unrecognized", s)


def parse_date(text: Any) -> date | ParseFailure | None:
    """``MM/DD/YY``, ``MM/DD/YYYY`` (``M/D`` too), ISO ``YYYY-MM-DD`` (optionally ``T00:00:00``),
    ``YYYY/MM/DD`` and Excel serials (1990-2100) -> ``datetime.date``. Two-digit years pivot at 70
    (``00-69`` -> 20xx). Month-first is assumed for slashed dates; impossible dates fail."""
    r = parse_date_fmt(text)
    if r is None or isinstance(r, ParseFailure):
        return r
    return r[0]


# ------------------------------------------------------------------------- frequency, term, payment

_FREQ = {
    "d": FREQ_DAILY,
    "daily": FREQ_DAILY,
    "day": FREQ_DAILY,
    "dly": FREQ_DAILY,
    "/day": FREQ_DAILY,
    "w": FREQ_WEEKLY,
    "weekly": FREQ_WEEKLY,
    "week": FREQ_WEEKLY,
    "wk": FREQ_WEEKLY,
    "wkly": FREQ_WEEKLY,
    "/wk": FREQ_WEEKLY,
}


def parse_frequency(text: Any) -> str | ParseFailure | None:
    """``D`` / ``Daily`` / ``W`` / ``Weekly`` / ``wkly`` ... -> ``"daily"`` | ``"weekly"``."""
    s = _clean(text)
    if s is None:
        return None
    f = _FREQ.get(s.casefold().rstrip("."))
    return f if f else ParseFailure("frequency_unrecognized", s)


@dataclass(frozen=True)
class Term:
    bdays: int
    frequency_hint: str | None  # "weekly" when stated in weeks, else None (days say nothing)


_TERM = re.compile(
    r"^(?P<n>\d+(?:\.0+)?)\s*(?P<unit>wks?|weeks?|w|days?|d|bdays?|business\s+days?)?\.?$", re.IGNORECASE
)


def parse_term(text: Any) -> Term | ParseFailure | None:
    """``"120"`` / ``"120 days"`` -> 120 business days; ``"24 wks"`` -> 120 (5 business days per week).

    Term unit: business days (see module docstring). Must be a positive whole number.
    """
    s = _clean(text)
    if s is None:
        return None
    m = _TERM.match(s)
    if not m:
        return ParseFailure("term_unrecognized", s)
    n = int(Decimal(m["n"]))
    if n <= 0:
        return ParseFailure("term_not_positive", s)
    unit = (m["unit"] or "").casefold()
    if unit.startswith("w"):
        return Term(n * BDAYS_PER_WEEK, FREQ_WEEKLY)
    return Term(n, None)


@dataclass(frozen=True)
class Payment:
    cents: int
    frequency_hint: str | None  # from a suffix such as "wkly" or "/day"; None when not stated
    has_cents: bool  # False for whole-dollar text such as "$411" (cents may have been rounded away)


_PAY_SUFFIX = re.compile(
    r"^(?P<amt>.*?)\s*(?:(?P<sep>/|per\s+)?\s*(?P<f>wkly|weekly|wk|week|w|daily|day|dly|d))\.?$",
    re.IGNORECASE,
)


def parse_payment(text: Any) -> Payment | ParseFailure | None:
    """``"$410.67"`` -> 41067 cents; ``"$2,812.14 wkly"`` -> 281214 cents + weekly hint.

    The amount follows :func:`parse_money` (no negatives). A frequency suffix (``wkly``, ``weekly``,
    ``/wk``, ``per week``, ``daily``, ``/day``) becomes ``frequency_hint``.
    """
    s = _clean(text)
    if s is None:
        return None
    hint = None
    body = s
    m = _PAY_SUFFIX.match(s)
    if m and m["amt"] and re.search(r"\d", m["amt"]):
        body = m["amt"]
        hint = _FREQ.get(m["f"].casefold())
    v = parse_money(body)
    if v is None:
        return ParseFailure("payment_garbage", s)
    if isinstance(v, ParseFailure):
        return ParseFailure("payment_" + v.reason.removeprefix("money_"), s)
    return Payment(v, hint, money_has_cents(body))


def half_up_div(num: int, den: int) -> int:
    """Round ``num / den`` half away from zero for non-negative integers (exact integer arithmetic)."""
    if den <= 0 or num < 0:
        raise ValueError("half_up_div expects num >= 0 and den > 0")
    return (2 * num + den) // (2 * den)


def installment_cents(rtr_cents: int, term_bdays: int, frequency: str) -> int:
    """Contractual installment implied by RTR and term: RTR / term per business day, x5 for weekly,
    rounded half-up to the cent (this is how the trackers' payment column is computed)."""
    k = BDAYS_PER_WEEK if frequency == FREQ_WEEKLY else 1
    return half_up_div(rtr_cents * k, term_bdays)


# ------------------------------------------------------------------------------ identifiers, text

_EIN = re.compile(r"^(\d{2})-?(\d{7})$")


def parse_ein(text: Any) -> str | ParseFailure | None:
    """``"20-8752044"`` / ``"208752044"`` -> ``"20-8752044"``."""
    s = _clean(text)
    if s is None:
        return None
    m = _EIN.match(s)
    return f"{m[1]}-{m[2]}" if m else ParseFailure("ein_invalid", s)


def parse_text(text: Any) -> str | None:
    """Verbatim text (no trimming, no case change) so later parts can match on the raw form.
    Whitespace-only text is treated as missing."""
    if text is None:
        return None
    s = str(text)
    return s if s.strip() else None


def parse_upper_code(text: Any) -> str | None:
    """Short code columns (ACH ``Type``, state): trimmed and upper-cased."""
    s = _clean(text)
    return s.upper() if s else None


_STATE = re.compile(r"^[A-Z]{2}$")


def parse_state(text: Any) -> str | ParseFailure | None:
    s = parse_upper_code(text)
    if s is None:
        return None
    return s if _STATE.match(s) else ParseFailure("state_invalid", str(text))


def norm_lookup_key(text: str) -> str:
    """Key used for lookup tables (industry, status, events): casefold + collapse whitespace."""
    return " ".join(text.casefold().split())


def parse_lookup(text: Any, table: dict[str, str], *, reason: str) -> str | ParseFailure | None:
    """Free text -> canonical value through a lookup table keyed by :func:`norm_lookup_key`."""
    s = _clean(text)
    if s is None:
        return None
    v = table.get(norm_lookup_key(s))
    return v if v is not None else ParseFailure(reason, s)
