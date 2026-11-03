"""Ingest of the real raw/ files: counts, lineage, typing, src_row definition."""

from __future__ import annotations

import csv

import duckdb
import openpyxl
import pytest

from mcarisk import paths
from mcarisk.ingest import SOURCES

RAW_TABLES = [s.table for s in SOURCES]


@pytest.fixture(scope="module")
def con(raw_ingest):
    c = duckdb.connect(str(raw_ingest["db"]), read_only=True)
    yield c
    c.close()


def _expected_counts():
    """Count data rows per file straight from the files (header, blank and title rows excluded)."""
    out = {}
    for p in sorted((paths.RAW_DIR / "ach_exports").glob("*.csv")):
        with open(p, newline="", encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
        out[f"raw/ach_exports/{p.name}"] = n - 1
    for p in sorted(paths.RAW_DIR.glob("*.xlsx")):
        wb = openpyxl.load_workbook(p, read_only=True)
        nonblank = [r for r in wb.worksheets[0].iter_rows(values_only=True) if any(v is not None for v in r)]
        wb.close()
        # minus the header row, minus one-cell title rows (the 2023 tracker has one)
        title_rows = sum(1 for r in nonblank if sum(v is not None for v in r) == 1)
        out[f"raw/{p.name}"] = len(nonblank) - 1 - title_rows
    return out


def test_every_raw_file_is_ingested(con):
    files = {r[0] for r in con.sql("SELECT DISTINCT src_file FROM raw_sources").fetchall()}
    on_disk = {p.relative_to(paths.ROOT).as_posix() for p in paths.RAW_DIR.rglob("*") if p.is_file()}
    assert files == on_disk
    assert len([f for f in files if f.startswith("raw/ach_exports/")]) == 46


def test_row_counts_per_file_equal_file_contents(con):
    sql = " UNION ALL ".join(f"SELECT src_file, COUNT(*) FROM {t} GROUP BY src_file" for t in RAW_TABLES)
    assert dict(con.sql(sql).fetchall()) == _expected_counts()


def test_reconciliation_identity(con):
    bad = con.sql(
        "SELECT * FROM raw_sources WHERE n_physical_rows <> (header_row IS NOT NULL)::INT + n_loaded + n_rejected"
    ).fetchall()
    assert bad == []


def test_lineage_populated(con):
    for t in RAW_TABLES:
        n_null = con.sql(
            f"SELECT COUNT(*) FROM {t} WHERE src_file IS NULL OR src_sheet IS NULL OR src_row IS NULL "
            f"OR src_file = '' OR src_sheet = '' OR src_row < 1"
        ).fetchone()[0]
        assert n_null == 0, t
        dupes = con.sql(
            f"SELECT COUNT(*) - COUNT(DISTINCT (src_file, src_sheet, src_row)) FROM {t}"
        ).fetchone()[0]
        assert dupes == 0, t


def test_all_data_columns_are_varchar(con):
    in_list = ", ".join(f"'{t}'" for t in RAW_TABLES)
    rows = con.sql(
        f"SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_name IN ({in_list})"
    ).fetchall()
    assert len({r[0] for r in rows}) == len(RAW_TABLES)
    for t, c, typ in rows:
        expected = "INTEGER" if c == "src_row" else "VARCHAR"
        assert typ == expected, (t, c, typ)


def test_src_row_definition(con):
    # xlsx: Excel row number. The 2023 tracker header is on row 3 -> first data row is 4.
    assert con.sql("SELECT MIN(src_row) FROM raw_tracker_2023").fetchone()[0] == 4
    hdr = con.sql(
        "SELECT header_row FROM raw_sources WHERE src_file='raw/deal_tracker_2023.xlsx'"
    ).fetchone()[0]
    assert hdr == 3
    # CSV: 1-based record number with the header as record 1.
    first = con.sql(
        'SELECT "Trans Date", Deal, Amount FROM raw_ach '
        "WHERE src_file='raw/ach_exports/ach_2023_01.csv' AND src_row=2"
    ).fetchone()
    with open(paths.RAW_DIR / "ach_exports" / "ach_2023_01.csv", newline="", encoding="utf-8") as fh:
        rec = list(csv.reader(fh))[1]
    assert first == (rec[0], rec[1], rec[3])


def test_values_kept_as_text(con):
    row = con.sql(
        'SELECT "Advance Amount", "Commission %", "Fund Date" FROM raw_tracker_2023 WHERE src_row = 4'
    ).fetchone()
    assert row[0].startswith("$") and row[1].endswith("%") and row[2].count("/") == 2
    assert con.sql('SELECT "Factor Rate" FROM raw_tracker_2024 LIMIT 1').fetchone()[0].endswith("x")
    # the ACH date-format switch is preserved, not normalized
    june = con.sql("SELECT \"Trans Date\" FROM raw_ach WHERE src_file LIKE '%2024_06%' LIMIT 1").fetchone()[0]
    july = con.sql("SELECT \"Trans Date\" FROM raw_ach WHERE src_file LIKE '%2024_07%' LIMIT 1").fetchone()[0]
    assert june[2] == "/" and july[4] == "-"


def test_real_excel_dates_are_marked(con):
    n = con.sql(
        "SELECT COUNT(*) FROM raw_cell_types WHERE table_name='raw_tracker_2024' AND column_name='Funded' "
        "AND native_type='datetime'"
    ).fetchone()[0]
    assert n == con.sql("SELECT COUNT(*) FROM raw_tracker_2024").fetchone()[0]
    # 2025+ trackers store dates as text: no datetime cells there
    n25 = con.sql(
        "SELECT COUNT(*) FROM raw_cell_types WHERE table_name='raw_tracker_2025' AND native_type='datetime'"
    ).fetchone()[0]
    assert n25 == 0


def test_rejects_have_reasons(con):
    rows = con.sql("SELECT src_file, src_row, reason FROM raw_rejects ORDER BY src_file, src_row").fetchall()
    assert rows == [
        ("raw/deal_tracker_2023.xlsx", 1, "preamble_above_header"),
        ("raw/deal_tracker_2023.xlsx", 2, "blank_row"),
    ]


def test_bookkeeping_tables_exist(con):
    tables = {r[0] for r in con.sql("SELECT table_name FROM information_schema.tables").fetchall()}
    assert {"raw_rejects", "raw_sources", "raw_columns", "raw_cell_types", *RAW_TABLES} <= tables
