"""pandera schemas for the Part 2 canonical tables (the stage boundary contract).

Every ``*_std`` DataFrame is validated with ``lazy=True`` before it is written; a violation raises
``pandera.errors.SchemaErrors`` and fails the stage. Conventions checked here (CLAUDE.md):
money = integer cents (nullable ``Int64``), dates = ``datetime.date`` (not ``datetime``), lineage
columns non-null, identifiers unique.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import pandera.pandas as pa

FREQUENCIES = ["daily", "weekly"]
TXN_TYPES = ["FUNDING", "DEBIT", "RETURN", "PAYOFF", "SETTLEMENT"]
STATUS_NORM = ["active", "modified", "paid_off", "renewed", "defaulted", "settled", "written_off"]
EVENT_TYPES = ["default", "bankruptcy", "modification", "settlement", "write_off"]


def _is_date(v: Any) -> bool:
    return isinstance(v, date) and not isinstance(v, datetime)


def _date_col(nullable: bool = False) -> pa.Column:
    return pa.Column(
        object,
        pa.Check(lambda s: s.map(lambda v: _is_date(v) or (v is None or v is pd.NaT)), element_wise=False),
        nullable=nullable,
    )


def _cents(nullable: bool = True, *, non_negative: bool = True) -> pa.Column:
    checks = [pa.Check.ge(0)] if non_negative else []
    return pa.Column("Int64", checks, nullable=nullable)


def _int(nullable: bool = True, lo: int | None = 0, hi: int | None = None, unique: bool = False) -> pa.Column:
    checks = []
    if lo is not None:
        checks.append(pa.Check.ge(lo))
    if hi is not None:
        checks.append(pa.Check.le(hi))
    return pa.Column("Int64", checks, nullable=nullable, unique=unique)


def _text(nullable: bool = True, isin: list[str] | None = None) -> pa.Column:
    return pa.Column(object, [pa.Check.isin(isin)] if isin else [], nullable=nullable)


LINEAGE = {
    "src_file": pa.Column(str, pa.Check.str_startswith("raw/")),
    "src_sheet": pa.Column(str),
    "src_row": pa.Column("Int64", pa.Check.ge(1)),
}

DEALS_STD = pa.DataFrameSchema(
    {
        "deal_id": _int(nullable=False, lo=1, unique=True),
        "source_family": _text(nullable=False, isin=["tracker_2023", "tracker_2024", "tracker_2025"]),
        "merchant_name_raw": _text(),
        "dba_raw": _text(),
        "ein": pa.Column(object, pa.Check.str_matches(r"^\d{2}-\d{7}$"), nullable=True),
        "funding_date": _date_col(),
        "advance_cents": _cents(),
        "factor": pa.Column(float, pa.Check.in_range(1.0, 2.0), nullable=True),
        "rtr_cents": _cents(),
        "payment_cents": _cents(),
        "frequency": _text(isin=FREQUENCIES),
        "term_bdays": _int(lo=1),
        "fee_cents": _cents(),
        "commission_rate": pa.Column(float, pa.Check.in_range(0.0, 0.5), nullable=True),
        "iso_name": _text(),
        "prior_deal_id": _int(lo=1),
        "payoff_amt_cents": _cents(),
        "status_raw": _text(),
        "status_norm": _text(isin=STATUS_NORM),
        "notes_raw": _text(),
        "fico": _int(lo=300, hi=900),
        "avg_monthly_rev_cents": _cents(),
        "tib_months": _int(),
        "industry_raw": _text(),
        "industry": _text(),
        "state": _text(),
        "positions": _int(),
        "nsf_90d": _int(),
        "std_flags": _text(),
        **LINEAGE,
    },
    strict=True,
    name="deals_std",
)

PAYMENTS_STD = pa.DataFrameSchema(
    {
        "payment_id": _int(nullable=False, lo=1, unique=True),
        "txn_date": _date_col(),
        "deal_id_posted": _int(nullable=False, lo=1),
        "merchant_name_raw": _text(),
        "amount_cents": pa.Column("Int64", pa.Check.ne(0)),
        "txn_type": _text(nullable=False, isin=TXN_TYPES),
        "return_code": _text(),
        "batch_id": _text(nullable=False),
        "description": _text(),
        "date_format_raw": _text(nullable=False),
        "n_copies": _int(nullable=False, lo=1),
        **LINEAGE,
    },
    strict=True,
    # METRICS.md L2: after dedupe the key is unique by construction; the schema re-asserts it.
    unique=["txn_date", "deal_id_posted", "amount_cents", "txn_type", "batch_id"],
    name="payments_std",
)

PAYMENTS_DUPES = pa.DataFrameSchema(
    {
        "payment_id": _int(nullable=False, lo=1),
        "txn_date": _date_col(),
        "deal_id_posted": _int(nullable=False, lo=1),
        "amount_cents": pa.Column("Int64"),
        "txn_type": _text(nullable=False, isin=TXN_TYPES),
        "batch_id": _text(nullable=False),
        "date_text_raw": _text(nullable=False),
        "date_format_raw": _text(nullable=False),
        "kept_src_file": pa.Column(str),
        "kept_src_row": pa.Column("Int64"),
        **LINEAGE,
    },
    strict=True,
    name="payments_dupes",
)

SCORECARDS_STD = pa.DataFrameSchema(
    {
        "scorecard_id": _int(nullable=False, lo=1, unique=True),
        "business_name_raw": _text(nullable=False),
        "app_date": _date_col(),
        "requested_cents": _cents(),
        "fico": _int(lo=300, hi=900),
        "avg_monthly_rev_cents": _cents(),
        "nsf_90d": _int(),
        "positions": _int(),
        "tib_years": pa.Column(float, pa.Check.ge(0), nullable=True),
        "industry_raw": _text(),
        "industry": _text(),
        "state": _text(),
        "uw_notes_raw": _text(),
        "std_flags": _text(),
        **LINEAGE,
    },
    strict=True,
    name="scorecards_std",
)

COLLECTIONS_STD = pa.DataFrameSchema(
    {
        "collection_id": _int(nullable=False, lo=1, unique=True),
        "log_date": _date_col(),
        "merchant_name_raw": _text(),
        "deal_id_raw": _int(lo=1),
        "event_raw": _text(nullable=False),
        "event_type": _text(nullable=False, isin=EVENT_TYPES),
        "balance_cents": _cents(),
        "amount_cents": _cents(),
        "notes_raw": _text(),
        "std_flags": _text(),
        **LINEAGE,
    },
    strict=True,
    name="collections_std",
)

SCHEMAS: dict[str, pa.DataFrameSchema] = {
    "deals_std": DEALS_STD,
    "payments_std": PAYMENTS_STD,
    "payments_dupes": PAYMENTS_DUPES,
    "scorecards_std": SCORECARDS_STD,
    "collections_std": COLLECTIONS_STD,
}


def validate(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the named schema (lazy: all violations reported); raises SchemaErrors."""
    return SCHEMAS[name].validate(df, lazy=True)
