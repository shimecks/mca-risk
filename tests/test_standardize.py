"""Stage 2 (standardize): mappings, L2 dedupe (July 2024 format switch), pandera schemas, Q7 accounting."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import duckdb
import pandas as pd
import pandera.errors as pa_errors
import pytest

from mcarisk import paths, schemas
from mcarisk.mapping import (
    ACTION_DERIVED,
    ACTION_NULLED,
    ACTION_REPAIRED,
    apply_row,
    load_lookups,
    load_mapping,
)
from mcarisk.standardize import (
    RULES,
    StandardizeError,
    dedupe_payments,
    frame,
    map_table,
    read_raw,
)

LOOKUPS = load_lookups()


@pytest.fixture(scope="module")
def std_db(std_build):
    return std_build


@pytest.fixture(scope="module")
def con(std_db):
    c = duckdb.connect(str(std_db["db"]), read_only=True)
    yield c
    c.close()


def _ach(src_file: str, src_row: int, date_text: str, **kw) -> dict:
    row = {
        "src_file": src_file,
        "src_sheet": "(csv)",
        "src_row": src_row,
        "Trans Date": date_text,
        "Deal": "1150",
        "Merchant Name": "Harbor Bistro LLC",
        "Amount": "181.26",
        "Type": "DEBIT",
        "Return Code": None,
        "Batch ID": "B24062801D",
        "Description": "DAILY REMIT",
    }
    row.update(kw)
    return row


# ------------------------------------------------------------------------------------ mapping rows


def test_tracker_2023_weekly_row_and_whole_dollar_repair():
    m = load_mapping("tracker_2023")
    raw = {
        "Deal #": "1050",
        "Merchant": "Osei Shop Co",
        "Fund Date": "07/03/23",
        "Advance Amount": "$62,000",
        "Factor": "1.27",
        "RTR": "$78,740.00",
        "Daily Pmt": "$2,812 wkly",  # whole dollars: the true installment is $2,812.14
        "Term (days)": "28 wks",
        "Commission %": "7.5%",
        "Orig Fee": "$2,250.00",
        "Status": "Paid Off - Renewal",
    }
    res = apply_row(m, raw, LOOKUPS)
    v = res.values
    assert res.reject is None
    assert v["frequency"] == "weekly" and v["term_bdays"] == 140
    assert v["payment_cents"] == 281_214
    assert v["advance_cents"] == 6_200_000 and v["rtr_cents"] == 7_874_000
    assert v["commission_rate"] == Decimal("0.075") and v["funding_date"] == date(2023, 7, 3)
    assert v["status_norm"] == "renewed"
    (log,) = [e for e in res.logs if e.action == ACTION_REPAIRED]
    assert log.field == "payment_cents" and log.raw_value == "$2,812 wkly" and log.value == "281214"


def test_whole_dollar_payment_repair_rules():
    m = load_mapping("tracker_2023")
    raw = {
        "Deal #": "1002",
        "Fund Date": "02/16/23",
        "RTR": "$74,415",
        "Daily Pmt": "$744",
        "Term (days)": "100",
    }
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["payment_cents"] == 74_415  # $744 typed, $744.15 implied by RTR / term -> repaired
    raw["Daily Pmt"] = "$744.00"  # explicit cents are never overridden (S2 counts the mismatch instead)
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["payment_cents"] == 74_400 and not [e for e in res.logs if e.action == ACTION_REPAIRED]
    raw.update({"RTR": "$74,400", "Daily Pmt": "$744"})  # whole dollars that are exact: nothing to repair
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["payment_cents"] == 74_400 and not [e for e in res.logs if e.action == ACTION_REPAIRED]
    raw.update(
        {"RTR": "$80,000", "Daily Pmt": "$744"}
    )  # $56 away from RTR / term: not a rounding, left alone
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["payment_cents"] == 74_400


def test_conflicting_frequency_hints_null_the_field():
    m = load_mapping("tracker_2023")
    raw = {"Deal #": "1", "Fund Date": "01/03/23", "Daily Pmt": "$100.00 daily", "Term (days)": "24 wks"}
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["frequency"] is None
    assert any(
        e.field == "frequency" and e.action == ACTION_NULLED and e.reason == "conflicting_hints"
        for e in res.logs
    )


def test_tracker_2024_term_is_derived_exactly():
    m = load_mapping("tracker_2024")
    raw = {
        "Deal ID": "1123",
        "Funded": "2024-01-04",
        "Purchase Price": "62000",
        "Factor Rate": "1.27x",
        "Payback": "78740",
        "Pmt Amt": "2812.14",
        "Freq": "Weekly",
        "Comm": "0.075",
        "Prior Deal": None,
    }
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["term_bdays"] == 140 and res.values["commission_rate"] == Decimal("0.075")
    assert [e.action for e in res.logs] == [ACTION_DERIVED]
    raw["Pmt Amt"] = "2812.99"  # no term reproduces this installment -> NULL + log, never a guess
    res = apply_row(m, raw, LOOKUPS)
    assert res.values["term_bdays"] is None
    assert res.logs[0].reason == "term_underivable_no_exact_match"


def test_required_field_failure_rejects_the_row():
    m = load_mapping("tracker_2025")
    res = apply_row(m, {"deal_id": "1535", "funding_date": "13/45/2025"}, LOOKUPS)
    assert (
        res.reject is not None and res.reject.field == "funding_date" and res.reject.reason == "date_invalid"
    )
    mapped = map_table(
        m, [{"src_file": "raw/x.xlsx", "src_sheet": "S", "src_row": 2, "deal_id": "x"}], LOOKUPS
    )
    assert mapped.rows == [] and len(mapped.rejects) == 1
    assert mapped.rejects[0]["reason"] == "deal_id:int_garbage"
    assert json.loads(mapped.rejects[0]["raw_values"]) == {"deal_id": "x"}


def test_optional_field_failure_nulls_and_logs():
    m = load_mapping("tracker_2025")
    res = apply_row(
        m, {"deal_id": "1", "funding_date": "2025-01-02", "industry": "Bakery", "ein": "12"}, LOOKUPS
    )
    assert res.reject is None and res.values["industry"] is None and res.values["ein"] is None
    assert {(e.field, e.reason) for e in res.logs} == {
        ("industry", "industry_unmapped"),
        ("ein", "ein_invalid"),
    }


def test_mapping_reading_a_missing_column_fails_loudly(raw_ingest, tmp_path):
    db = tmp_path / "x.duckdb"
    c = duckdb.connect(str(db))
    c.execute(
        'CREATE TABLE raw_tracker_2024 (src_file VARCHAR, src_sheet VARCHAR, src_row INTEGER, "Deal ID" VARCHAR)'
    )
    with pytest.raises(StandardizeError, match="no such column"):
        read_raw(c, load_mapping("tracker_2024"))
    c.close()


def test_lookup_tables_are_unambiguous():
    assert LOOKUPS["industry"]["restaurant/bar"] == "Restaurant"
    assert LOOKUPS["industry"]["services"] == "Professional Services"
    assert LOOKUPS["status"]["paid off - renewal"] == "renewed"
    assert LOOKUPS["collection_events"]["charge off"] == "write_off"


# --------------------------------------------------------------------------------------- L2 dedupe


def _map_ach(rows: list[dict]) -> list[dict]:
    mapped = map_table(load_mapping("ach"), rows, LOOKUPS)
    assert not mapped.rejects
    return mapped.rows


KEY = ["txn_date", "deal_id_posted", "amount_cents", "txn_type", "batch_id"]


def test_dedupe_july_2024_format_switch():
    """The same ACH row appears as 06/28/2024 in the June export and 2024-06-28 in the July export."""
    june = _ach("raw/ach_exports/ach_2024_06.csv", 2330, "06/28/2024")
    july = _ach("raw/ach_exports/ach_2024_07.csv", 2, "2024-06-28")
    other_batch = _ach("raw/ach_exports/ach_2024_07.csv", 3, "2024-06-28", **{"Batch ID": "B24062802D"})
    kept, dupes = dedupe_payments(_map_ach([july, june, other_batch]), KEY, {})
    assert len(kept) == 2 and len(dupes) == 1
    k = next(r for r in kept if r["batch_id"] == "B24062801D")
    assert k["txn_date"] == date(2024, 6, 28) and k["n_copies"] == 2
    # the kept copy is the transaction's own month export (June), whatever the input order
    assert (k["src_file"], k["src_row"], k["date_format_raw"]) == (
        "raw/ach_exports/ach_2024_06.csv",
        2330,
        "MM/DD/YYYY",
    )
    (d,) = dupes
    assert (d["src_file"], d["src_row"], d["date_format_raw"]) == (
        "raw/ach_exports/ach_2024_07.csv",
        2,
        "YYYY-MM-DD",
    )
    assert (d["kept_src_file"], d["kept_src_row"], d["payment_id"]) == (
        k["src_file"],
        k["src_row"],
        k["payment_id"],
    )


def test_dedupe_on_raw_text_would_miss_the_switch():
    rows = [
        _ach("raw/ach_exports/ach_2024_06.csv", 9, "06/28/2024"),
        _ach("raw/ach_exports/ach_2024_07.csv", 2, "2024-06-28"),
    ]
    assert len({(r["Trans Date"], r["Deal"], r["Amount"], r["Type"], r["Batch ID"]) for r in rows}) == 2
    kept, _ = dedupe_payments(_map_ach(rows), KEY, {})
    assert len(kept) == 1


def test_dedupe_keeps_distinct_events():
    base = _ach("raw/ach_exports/ach_2024_06.csv", 2, "06/27/2024")
    variants = [
        base,
        {**base, "src_row": 3, "Amount": "181.27"},
        {**base, "src_row": 4, "Deal": "1151"},
        {**base, "src_row": 5, "Type": "RETURN", "Amount": "-181.26", "Return Code": "R01"},
        {**base, "src_row": 6, "Trans Date": "06/28/2024"},
    ]
    kept, dupes = dedupe_payments(_map_ach(variants), KEY, {})
    assert len(kept) == 5 and dupes == []
    assert [r["payment_id"] for r in kept] == [1, 2, 3, 4, 5]


def test_real_data_july_2024_overlap(con):
    """Rows in ach_2024_07.csv dated June 2024 (ISO text) are dropped in favour of the June export."""
    n = con.execute(
        """SELECT COUNT(*) FROM payments_dupes
           WHERE src_file = 'raw/ach_exports/ach_2024_07.csv' AND txn_date < DATE '2024-07-01'
             AND date_format_raw = 'YYYY-MM-DD' AND kept_src_file = 'raw/ach_exports/ach_2024_06.csv'"""
    ).fetchone()[0]
    assert n > 0
    kept_fmt = con.execute(
        """SELECT DISTINCT p.date_format_raw FROM payments_dupes d JOIN payments_std p USING (payment_id)
           WHERE d.src_file = 'raw/ach_exports/ach_2024_07.csv' AND d.txn_date < DATE '2024-07-01'"""
    ).fetchall()
    assert kept_fmt == [("MM/DD/YYYY",)]
    # no payment survives twice, and every kept row is in its own month's export
    assert (
        con.execute(
            "SELECT COUNT(*) FROM (SELECT txn_date, deal_id_posted, amount_cents, txn_type, batch_id "
            "FROM payments_std GROUP BY ALL HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT COUNT(*) FROM payments_std WHERE src_file <> 'raw/ach_exports/ach_' || strftime(txn_date, '%Y_%m') || '.csv'"
        ).fetchone()[0]
        == 0
    )


# ------------------------------------------------------------------------------------ pandera schemas


def _good_deal(**kw) -> dict:
    row = {
        "deal_id": 1001,
        "source_family": "tracker_2023",
        "funding_date": date(2023, 1, 19),
        "advance_cents": 3_850_000,
        "factor": 1.28,
        "rtr_cents": 4_928_000,
        "payment_cents": 41_067,
        "frequency": "daily",
        "term_bdays": 120,
        "src_file": "raw/deal_tracker_2023.xlsx",
        "src_sheet": "Funded",
        "src_row": 4,
    }
    row.update(kw)
    return row


def test_deals_schema_accepts_a_good_frame():
    schemas.validate("deals_std", frame("deals_std", [_good_deal(), _good_deal(deal_id=1002, src_row=5)]))


@pytest.mark.parametrize(
    "bad",
    [
        {"advance_cents": -1},  # negative money
        {"funding_date": datetime(2023, 1, 19)},  # a datetime is not a date
        {"funding_date": "2023-01-19"},  # nor is text
        {"frequency": "monthly"},
        {"factor": 3.5},
        {"term_bdays": 0},
        {"src_file": None},  # lineage is mandatory
        {"status_norm": "Paid Off"},  # not a canonical status
    ],
)
def test_deals_schema_failures_raise(bad):
    with pytest.raises(pa_errors.SchemaErrors):
        schemas.validate("deals_std", frame("deals_std", [_good_deal(**bad)]))


def test_deals_schema_rejects_duplicate_deal_ids():
    with pytest.raises(pa_errors.SchemaErrors):
        schemas.validate("deals_std", frame("deals_std", [_good_deal(), _good_deal(src_row=5)]))


def test_deals_schema_is_strict_about_columns():
    df = frame("deals_std", [_good_deal()])
    df["surprise"] = 1
    with pytest.raises(pa_errors.SchemaErrors):
        schemas.validate("deals_std", df)


def test_payments_schema_rejects_a_repeated_l2_key():
    rows = _map_ach([_ach("raw/ach_exports/ach_2024_06.csv", 2, "06/28/2024")])
    r = {**rows[0], "payment_id": 1, "n_copies": 1}
    schemas.validate("payments_std", frame("payments_std", [r]))
    with pytest.raises(pa_errors.SchemaErrors):
        schemas.validate("payments_std", frame("payments_std", [r, {**r, "payment_id": 2, "src_row": 3}]))
    with pytest.raises(pa_errors.SchemaErrors):
        schemas.validate("payments_std", frame("payments_std", [{**r, "amount_cents": 0}]))


# ------------------------------------------------------------------------------- end to end (raw/)


def test_every_raw_row_is_accounted_for_exactly_once(con):
    """Q7: each raw row lands in exactly one of {*_std, std_rejects, payments_dupes}, at its lineage."""
    pairs = {
        "raw_tracker_2023": "deals_std",
        "raw_tracker_2024": "deals_std",
        "raw_tracker_2025": "deals_std",
        "raw_ach": "payments_std",
        "raw_scorecards": "scorecards_std",
        "raw_collections": "collections_std",
    }
    for raw, std in pairs.items():
        extra = "UNION ALL SELECT src_file, src_row FROM payments_dupes" if std == "payments_std" else ""
        bad = con.execute(
            f"""WITH out AS (
                  SELECT src_file, src_row FROM {std} {extra}
                  UNION ALL SELECT src_file, src_row FROM std_rejects WHERE source_table = '{raw}')
                SELECT
                  (SELECT COUNT(*) FROM {raw} r WHERE (SELECT COUNT(*) FROM out o
                      WHERE o.src_file = r.src_file AND o.src_row = r.src_row) <> 1),
                  (SELECT COUNT(*) FROM out o WHERE o.src_file IN (SELECT src_file FROM {raw})
                      AND NOT EXISTS (SELECT 1 FROM {raw} r WHERE r.src_file = o.src_file AND r.src_row = o.src_row))"""
        ).fetchone()
        assert bad == (0, 0), raw
    acc = con.execute("SELECT * FROM std_accounting").df()
    assert (acc.n_raw == acc.n_loaded + acc.n_rejected + acc.n_l2_duplicates).all()


def test_real_counts(con):
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    assert q("SELECT COUNT(*) FROM deals_std") == 114 + 405 + 1448
    assert q("SELECT COUNT(*) FROM payments_std") + q("SELECT COUNT(*) FROM payments_dupes") == q(
        "SELECT COUNT(*) FROM raw_ach"
    )
    assert (
        q("SELECT COUNT(*) FROM scorecards_std") == 496 and q("SELECT COUNT(*) FROM collections_std") == 726
    )
    # every canonical lookup resolved on the real data; nothing rejected
    assert q("SELECT COUNT(*) FROM std_log WHERE action IN ('nulled', 'rejected')") == 0
    assert q("SELECT COUNT(*) FROM std_rejects") == 0
    assert (
        q("SELECT COUNT(*) FROM deals_std WHERE source_family = 'tracker_2024' AND term_bdays IS NULL") == 0
    )


def test_table_types(con):
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'deals_std'"
        ).fetchall()
    )
    assert (
        types["funding_date"] == "DATE"
        and types["advance_cents"] == "BIGINT"
        and types["src_row"] == "INTEGER"
    )
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'payments_std'"
        ).fetchall()
    )
    assert types["txn_date"] == "DATE" and types["amount_cents"] == "BIGINT"


def test_data_quality_lists_every_rule(con, std_db):
    dq = con.execute("SELECT * FROM data_quality").df().set_index("rule_id")
    for rule in ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "L1", "L2", "L3"]:
        assert rule in dq.index
    assert dq.loc["L2", "status"] == "evaluated" and dq.loc["L2", "hits"] == 15_640
    assert dq.loc["Q7", "status"] == "evaluated"
    assert dq.loc["Q6", "status"] == "pending (Part 4)" and dq.loc["L3", "status"] == "pending (Part 3)"
    assert json.loads(dq.loc["L2", "sample_rows"])[0]["src_file"] == "raw/ach_exports/ach_2024_07.csv"
    assert len(dq) == len(RULES)
    text = std_db["report"].read_text(encoding="utf-8")
    assert "| L2 |" in text and "Row accounting" in text


def test_manifest_records_the_stage(std_db):
    m = json.loads(std_db["manifest"].read_text(encoding="utf-8"))
    st = m["stages"]["standardize"]
    assert st["status"] == "ok" and "ingest" in m["stages"]
    t = st["tables"]
    assert t["payments_std"]["reasons"]["rejected"] == {"l2_duplicate": 15_640}
    assert t["deals_std"]["accepted"] == 1967 and t["deals_std"]["repaired"] == 405 + 7
    assert t["deals_std"]["reasons"]["repaired"] == {
        "term_bdays_derived_rtr_over_payment": 405,
        "payment_whole_dollar_from_rtr_term": 7,
    }


def test_lineage_values_point_at_raw_rows(con):
    # deal 1001 is on Excel row 4 of the 2023 tracker (title + blank + header above it)
    assert con.execute(
        "SELECT src_file, src_sheet, src_row FROM deals_std WHERE deal_id = 1001"
    ).fetchone() == (
        "raw/deal_tracker_2023.xlsx",
        "Funded",
        4,
    )


def test_standardize_never_touches_raw_tables(raw_ingest, std_db):
    a = duckdb.connect(str(raw_ingest["db"]), read_only=True)
    b = duckdb.connect(str(std_db["db"]), read_only=True)
    try:
        for t in ("raw_ach", "raw_tracker_2023"):
            assert (
                a.execute(f"SELECT COUNT(*), SUM(src_row) FROM {t}").fetchone()
                == b.execute(f"SELECT COUNT(*), SUM(src_row) FROM {t}").fetchone()
            )
    finally:
        a.close()
        b.close()


def test_frame_keeps_null_integers_null():
    df = frame("deals_std", [_good_deal(prior_deal_id=None), _good_deal(deal_id=2, prior_deal_id=1001)])
    assert str(df["prior_deal_id"].dtype) == "Int64"
    assert df["prior_deal_id"].isna().tolist() == [True, False]
    assert isinstance(df["funding_date"].iloc[0], date)
    assert pd.api.types.is_float_dtype(df["factor"])


def test_paths_point_at_repo_mappings():
    assert (paths.MAPPINGS_DIR / "tracker_2023.yaml").exists()
