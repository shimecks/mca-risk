"""Ingest behaviour on small synthetic fixtures (no dependency on raw/)."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from mcarisk.ingest import (
    IngestError,
    Source,
    cell_text,
    column_names,
    detect_header,
    ingest,
)
from tests.conftest import make_xlsx

TRACKER = Source("raw_t", ("tracker.xlsx",), "test tracker")
CSVS = Source("raw_c", ("exports/*.csv",), "test csvs")


def test_detect_header_skips_title_and_blank_rows():
    rows = [
        ["FUNDED DEALS 2023 - internal, do not forward", None, None, None],
        [],
        ["Deal #", "Merchant", "Fund Date", "Advance Amount"],
        [1001, "Lantern Hauling LLC", "01/19/23", "$38,500.00"],
    ]
    assert detect_header(rows) == 2


def test_detect_header_first_row_when_clean():
    assert detect_header([["a", "b", "c"], ["1", "x", "2024-01-01"]]) == 0


def test_detect_header_ignores_all_text_data_rows_that_look_numeric():
    # a CSV-like sheet where everything is text: the numeric-looking first row is not a header
    rows = [
        ["01/03/2023", "1001", "-36000.0"],
        ["Trans Date", "Deal", "Amount"],
        ["01/04/2023", "1001", "410.67"],
    ]
    assert detect_header(rows) == 1


def test_detect_header_narrow_title_row_is_not_header():
    rows = [["Report", "Q3"], ["deal_id", "legal_name", "advance", "factor", "rtr"], [1, "x", 1, 1.2, 1.2]]
    assert detect_header(rows) == 1


def test_detect_header_fails_loudly():
    with pytest.raises(IngestError):
        detect_header([[1, 2, 3], [4, 5, 6]])


def test_cell_text_rendering():
    assert cell_text(None) is None
    assert cell_text("") is None
    assert cell_text("  $1,200.00 ") == "  $1,200.00 "
    assert cell_text(1001) == "1001"
    assert cell_text(2929.69) == "2929.69"
    assert cell_text(1.3) == "1.3"
    assert cell_text(True) == "TRUE"
    assert cell_text(datetime(2024, 1, 2)) == "2024-01-02"
    assert cell_text(datetime(2024, 1, 2, 13, 45)) == "2024-01-02T13:45:00"


def test_column_names_blank_and_duplicates():
    got = column_names(["A", None, "a", " src_row "], 5, csv_mode=False)
    assert got == ["A", "col_B", "a__2", "src_row__raw", "col_E"]


def _db(tmp_path):
    return tmp_path / "t.duckdb"


def test_title_row_fixture_end_to_end(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    make_xlsx(
        raw / "tracker.xlsx",
        {
            "Funded": [
                ["MY TITLE ROW"],
                [None],
                ["Deal #", "Merchant", "Fund Date", "Advance"],
                [1001, "A LLC", "01/19/23", "$1,000.00"],
                [None, None, None, None],
                [1002, "B Inc", datetime(2023, 2, 1), 2500.5],
            ]
        },
    )
    res = ingest(raw, _db(tmp_path), sources=(TRACKER,))
    assert res.tables == {"raw_t": 2}
    con = duckdb.connect(str(_db(tmp_path)), read_only=True)
    rows = con.sql(
        'SELECT src_file, src_sheet, src_row, "Deal #", "Fund Date", "Advance" FROM raw_t ORDER BY src_row'
    ).fetchall()
    assert rows == [
        ("raw/tracker.xlsx", "Funded", 4, "1001", "01/19/23", "$1,000.00"),
        ("raw/tracker.xlsx", "Funded", 6, "1002", "2023-02-01", "2500.5"),
    ]
    rej = con.sql("SELECT src_row, reason FROM raw_rejects ORDER BY src_row").fetchall()
    assert rej == [(1, "preamble_above_header"), (2, "blank_row"), (5, "blank_row")]
    src = con.sql("SELECT header_row, n_physical_rows, n_loaded, n_rejected FROM raw_sources").fetchone()
    assert src == (3, 6, 2, 3)
    # the real Excel date is distinguishable from text via raw_cell_types
    types = dict(con.sql("SELECT column_name, native_type FROM raw_cell_types WHERE src_row = 6").fetchall())
    assert types == {"Deal #": "int", "Fund Date": "datetime", "Advance": "float"}
    n_text_date = con.sql(
        "SELECT count(*) FROM raw_cell_types WHERE src_row = 4 AND column_name = 'Fund Date'"
    ).fetchone()[0]
    assert n_text_date == 0
    con.close()


def test_extra_sheet_is_rejected_with_reason(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    make_xlsx(
        raw / "tracker.xlsx", {"Funded": [["a", "b"], [1, 2]], "Notes": [["scratch"], ["x"]], "Empty": []}
    )
    res = ingest(raw, _db(tmp_path), sources=(Source("raw_t", ("tracker.xlsx",), "t", sheet="Funded"),))
    assert res.tables == {"raw_t": 1}
    con = duckdb.connect(str(_db(tmp_path)), read_only=True)
    assert con.sql("SELECT src_sheet, src_row, reason FROM raw_rejects ORDER BY src_row").fetchall() == [
        ("Notes", 1, "unmapped_sheet"),
        ("Notes", 2, "unmapped_sheet"),
    ]
    status = dict(con.sql("SELECT src_sheet, status FROM raw_sources").fetchall())
    assert status == {"Funded": "ingested", "Notes": "rejected", "Empty": "empty"}
    con.close()


def test_ambiguous_multi_sheet_workbook_fails(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    make_xlsx(raw / "tracker.xlsx", {"S1": [["a", "b"], [1, 2]], "S2": [["c", "d"], [3, 4]]})
    with pytest.raises(IngestError, match="exactly one non-empty sheet"):
        ingest(raw, _db(tmp_path), sources=(TRACKER,))


def test_csv_union_lineage_and_bad_rows(tmp_path):
    raw = tmp_path / "raw"
    (raw / "exports").mkdir(parents=True)
    (raw / "exports" / "a.csv").write_text(
        "Date,Deal,Amount\n01/03/2023,1001,410.67\n,,\n01/04/2023,1001,-5\n", encoding="utf-8"
    )
    (raw / "exports" / "b.csv").write_text(
        "Date,Deal,Amount\n2024-07-01,1002, 7 \n2024-07-02,1002\n", encoding="utf-8"
    )
    res = ingest(raw, _db(tmp_path), sources=(CSVS,))
    assert res.tables == {"raw_c": 3}
    con = duckdb.connect(str(_db(tmp_path)), read_only=True)
    got = con.sql(
        "SELECT src_file, src_sheet, src_row, Amount FROM raw_c ORDER BY src_file, src_row"
    ).fetchall()
    assert got == [
        ("raw/exports/a.csv", "(csv)", 2, "410.67"),
        ("raw/exports/a.csv", "(csv)", 4, "-5"),
        ("raw/exports/b.csv", "(csv)", 2, " 7 "),  # whitespace preserved verbatim
    ]
    assert con.sql(
        "SELECT src_file, src_row, reason FROM raw_rejects ORDER BY src_file, src_row"
    ).fetchall() == [
        ("raw/exports/a.csv", 3, "blank_row"),
        ("raw/exports/b.csv", 3, "field_count_mismatch"),
    ]
    con.close()


def test_schema_drift_within_family_fails(tmp_path):
    raw = tmp_path / "raw"
    (raw / "exports").mkdir(parents=True)
    (raw / "exports" / "a.csv").write_text("Date,Deal\nx,y\n", encoding="utf-8")
    (raw / "exports" / "b.csv").write_text("Date,DealID\nx,y\n", encoding="utf-8")
    with pytest.raises(IngestError, match="schema drift"):
        ingest(raw, _db(tmp_path), sources=(CSVS,))


def test_unknown_file_fails(tmp_path):
    raw = tmp_path / "raw"
    (raw / "exports").mkdir(parents=True)
    (raw / "exports" / "a.csv").write_text("Date,Deal\nx,y\n", encoding="utf-8")
    (raw / "surprise.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    with pytest.raises(IngestError, match="unrecognized"):
        ingest(raw, _db(tmp_path), sources=(CSVS,))
