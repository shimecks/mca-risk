"""Stage 1, ingest: load every file in ``raw/`` into DuckDB ``raw_*`` tables **as text**, with lineage.

Nothing is parsed or coerced here; that is Part 2 (standardize). This stage only guarantees that every
physical row of every sheet/file is either *loaded*, *rejected with a reason*, or is the sheet's
*header row*, and that the reconciliation holds (``n_physical_rows = header + loaded + rejected``).

Conventions (relied on by later parts and by evals; also documented in PROGRESS.md)
-------------------------------------------------------------------------------------
Lineage columns, first in every ``raw_*`` data table, all NOT NULL:
  * ``src_file``  POSIX path relative to the repo root, e.g. ``raw/ach_exports/ach_2024_07.csv``
                  (relative to the raw dir's parent when ingesting another directory).
  * ``src_sheet`` worksheet name for xlsx (e.g. ``Funded``); the literal ``(csv)`` for CSV files.
  * ``src_row``   INTEGER, the **1-based physical row number**: for xlsx the Excel row number you see
                  in Excel (title/blank/header rows included in the count); for CSV the 1-based record
                  number with the header record counted as 1, so the first data record is ``src_row = 2``
                  (this is the row number Excel shows when the CSV is opened there). The raw CSVs
                  contain no multi-line quoted fields, so record number == line number.

Cell text (all source data columns are VARCHAR):
  * empty cell / zero-length CSV field  -> NULL. Whitespace-only text is kept verbatim.
  * text cell                           -> the exact string (no strip, no case change).
  * xlsx int                            -> ``str(int)``, e.g. ``1001``.
  * xlsx float                          -> ``repr(float)``: shortest round-trip text, e.g. ``2929.69``.
  * xlsx real date/datetime             -> ISO 8601: ``2024-01-02`` at midnight, else ``2024-01-02T13:45:00``.
  * xlsx bool                           -> ``TRUE`` / ``FALSE``; error cells keep their text (``#N/A``).
  * formulas                            -> cached value (workbooks are opened with ``data_only=True``).
  Because a real Excel date and a text cell holding an ISO date render identically, every
  non-text, non-empty xlsx cell is also recorded in ``raw_cell_types`` (native type + number format).
  A non-NULL cell that has no ``raw_cell_types`` row was stored as text in the workbook.

Rejects (``raw_rejects``): one row per rejected physical row, with ``reason`` in
  * ``blank_row``             every cell empty or whitespace-only (anywhere in the sheet)
  * ``preamble_above_header`` non-blank row above the detected header (e.g. a title row)
  * ``field_count_mismatch``  CSV record with a different number of fields than the header
  * ``unmapped_sheet``        row of a non-empty worksheet that no source family claims
  ``raw_values`` keeps the row's rendered cells as a JSON array so nothing is lost.

Bookkeeping tables: ``raw_sources`` (one row per file x sheet, with counts and status),
``raw_columns`` (table column <- source header text and column position), ``raw_cell_types``.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

import duckdb
import openpyxl
import pyarrow as pa
from openpyxl.utils import get_column_letter

from mcarisk import paths
from mcarisk.manifest import RunManifest, sha256_file, start_run

CSV_SHEET = "(csv)"
LINEAGE_COLS = ("src_file", "src_sheet", "src_row")
HEADER_SCAN_ROWS = 25

REASON_BLANK = "blank_row"
REASON_PREAMBLE = "preamble_above_header"
REASON_FIELDS = "field_count_mismatch"
REASON_UNMAPPED_SHEET = "unmapped_sheet"


class IngestError(RuntimeError):
    """Raised when raw/ cannot be ingested without guessing (unknown file, schema drift, no header)."""


@dataclass(frozen=True)
class Source:
    """One source family -> one ``raw_*`` table. ``patterns`` are globs relative to the raw dir."""

    table: str
    patterns: tuple[str, ...]
    description: str
    sheet: str | None = None  # xlsx: required sheet name; None = the workbook's single non-empty sheet


SOURCES: tuple[Source, ...] = (
    Source("raw_tracker_2023", ("deal_tracker_2023.xlsx",), "2023 deal tracker (title row above header)"),
    Source("raw_tracker_2024", ("deal_tracker_2024.xlsx",), "2024 deal tracker (renamed columns)"),
    Source(
        "raw_tracker_2025",
        ("deal_tracker_2025.xlsx", "deal_tracker_2026_YTD.xlsx"),
        "2025+ deal trackers (snake_case schema; 2025 and 2026_YTD share it)",
    ),
    Source("raw_scorecards", ("underwriting_scorecards_*.xlsx",), "Underwriting scorecards 2023-2024"),
    Source("raw_collections", ("collections_log.xlsx",), "Collections & servicing log"),
    Source("raw_ach", ("ach_exports/ach_*.csv",), "Monthly ACH processor exports (unioned)"),
)

# ---------------------------------------------------------------------------------------- cell text

_NUMBERISH = re.compile(r"^\s*[-+(]?\s*\$?\s*[\d,]*\.?\d+\s*%?\s*\)?\s*(x|wks?|wkly)?\s*$", re.IGNORECASE)
_DATEISH = re.compile(r"^\s*\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}([ T].*)?$")


def cell_text(v: Any) -> str | None:
    """Render a native cell value as text following the module-level conventions."""
    if v is None:
        return None
    if isinstance(v, str):
        return v if v != "" else None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, datetime):
        if v.time() == dtime(0, 0):
            return v.date().isoformat()
        return v.isoformat()
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    return str(v)


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def is_blank_row(values: list[Any]) -> bool:
    return all(_is_blank(v) for v in values)


def _looks_like_data(v: Any) -> bool:
    if isinstance(v, (int, float, datetime, date)) and not isinstance(v, bool):
        return True
    return isinstance(v, str) and bool(_NUMBERISH.match(v) or _DATEISH.match(v))


def detect_header(rows: list[list[Any]], max_scan: int = HEADER_SCAN_ROWS) -> int:
    """Return the 0-based index (into ``rows``) of the header row.

    Generic rule, no file-specific knowledge: among the first ``max_scan`` rows, the header is the
    first row that (a) has >= 2 non-blank cells, (b) whose non-blank cells are all text that does not
    look like a number, money, factor or date, (c) whose labels are unique (case-insensitive), and
    (d) is "wide": at least 80% as many non-blank cells as the widest row in the scan window.
    Title rows fail (a)/(d); blank rows fail (a); data rows fail (b).
    """
    window = rows[:max_scan]
    widths = [sum(not _is_blank(v) for v in r) for r in window]
    widest = max(widths, default=0)
    for i, r in enumerate(window):
        cells = [v for v in r if not _is_blank(v)]
        if len(cells) < 2 or len(cells) < 0.8 * widest:
            continue
        if not all(isinstance(v, str) for v in cells) or any(_looks_like_data(v) for v in cells):
            continue
        labels = [v.strip().casefold() for v in cells]
        if len(set(labels)) != len(labels):
            continue
        return i
    raise IngestError(f"no header row found in the first {max_scan} rows")


def column_names(header: list[Any], width: int, *, csv_mode: bool) -> list[str]:
    """DuckDB column names from header text: stripped; blank -> ``col_<A|n>``; dupes -> ``name__2``."""
    names: list[str] = []
    seen: Counter[str] = Counter()
    for j in range(width):
        h = header[j] if j < len(header) else None
        base = (
            h.strip()
            if isinstance(h, str) and h.strip()
            else (f"col_{j + 1}" if csv_mode else f"col_{get_column_letter(j + 1)}")
        )
        if base.casefold() in LINEAGE_COLS:
            base = f"{base}__raw"
        seen[base.casefold()] += 1
        names.append(base if seen[base.casefold()] == 1 else f"{base}__{seen[base.casefold()]}")
    return names


# ---------------------------------------------------------------------------------------- readers


@dataclass
class SheetData:
    src_file: str
    src_sheet: str
    kind: str  # "xlsx" | "csv"
    sheet_index: int
    sheet_state: str
    rows: list[tuple[int, list[Any]]]  # (physical row number, native values)
    formats: dict[tuple[int, int], str] = field(default_factory=dict)  # (row, col) -> number_format


def read_xlsx(path: Path, src_file: str) -> list[SheetData]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    out: list[SheetData] = []
    try:
        for idx, ws in enumerate(wb.worksheets):
            rows: list[tuple[int, list[Any]]] = []
            formats: dict[tuple[int, int], str] = {}
            if ws.max_row and ws.max_column:
                for r in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
                    vals = []
                    for c in r:
                        vals.append(c.value)
                        if c.value is not None and not isinstance(c.value, str):
                            formats[(c.row, c.column)] = c.number_format
                    # trim trailing empty cells so width reflects real content
                    while vals and vals[-1] is None:
                        vals.pop()
                    rows.append((r[0].row, vals))
            # a sheet with no cells at all reports max_row=1 with one empty cell
            if len(rows) == 1 and not rows[0][1]:
                rows = []
            out.append(SheetData(src_file, ws.title, "xlsx", idx, ws.sheet_state, rows, formats))
    finally:
        wb.close()
    return out


def read_csv(path: Path, src_file: str) -> SheetData:
    rows: list[tuple[int, list[Any]]] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for n, rec in enumerate(csv.reader(fh), start=1):
            rows.append((n, [v if v != "" else None for v in rec]))
    return SheetData(src_file, CSV_SHEET, "csv", 0, "visible", rows)


# ---------------------------------------------------------------------------------------- ingest core


@dataclass
class TableBuffer:
    source: Source
    header_text: list[str] | None = None
    names: list[str] | None = None
    first_file: str | None = None
    lineage: dict[str, list[Any]] = field(default_factory=lambda: {k: [] for k in LINEAGE_COLS})
    cols: list[list[str | None]] = field(default_factory=list)
    columns_meta: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.lineage["src_row"])


@dataclass
class IngestResult:
    db_path: Path
    tables: dict[str, int]
    rejects: Counter[tuple[str, str]]
    sources: list[dict[str, Any]]
    seconds: float


def _rel(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def discover(raw_dir: Path, sources: tuple[Source, ...]) -> dict[str, list[Path]]:
    """Assign every file under ``raw_dir`` to exactly one source. Unknown or doubly-claimed files fail."""
    files = sorted(p for p in raw_dir.rglob("*") if p.is_file() and not p.name.startswith((".", "~$")))
    assigned: dict[str, list[Path]] = {s.table: [] for s in sources}
    unknown: list[str] = []
    for p in files:
        rel = _rel(p, raw_dir)
        hits = [s for s in sources if any(fnmatch.fnmatchcase(rel, pat) for pat in s.patterns)]
        if len(hits) == 1:
            assigned[hits[0].table].append(p)
        elif not hits:
            unknown.append(rel)
        else:
            raise IngestError(f"{rel} matches several sources: {[s.table for s in hits]}")
    if unknown:
        raise IngestError(f"unrecognized files in raw/ (add a Source for them): {unknown}")
    empty = [t for t, fs in assigned.items() if not fs]
    if empty:
        raise IngestError(f"no files found for sources: {empty}")
    return assigned


def _ingest_sheet(
    sheet: SheetData,
    buf: TableBuffer,
    rejects: list[dict[str, Any]],
    cell_types: list[dict[str, Any]],
) -> dict[str, Any]:
    """Split one sheet into header / loaded / rejected rows and append loaded rows to ``buf``."""
    rows = sheet.rows
    n_rej0 = len(rejects)

    def reject(rownum: int, values: list[Any], reason: str, detail: str = "") -> None:
        rejects.append(
            {
                "src_file": sheet.src_file,
                "src_sheet": sheet.src_sheet,
                "src_row": rownum,
                "table_name": buf.source.table,
                "reason": reason,
                "detail": detail,
                "raw_values": json.dumps([cell_text(v) for v in values], ensure_ascii=False),
            }
        )

    nonblank = [(n, v) for n, v in rows if not is_blank_row(v)]
    if not nonblank:
        for n, v in rows:
            reject(n, v, REASON_BLANK)
        return {
            "status": "empty",
            "status_reason": "sheet has no non-blank cells",
            "header_row": None,
            "n_loaded": 0,
            "n_rejected": len(rejects) - n_rej0,
            "n_columns": 0,
        }

    h_idx = detect_header([v for _, v in rows])
    header_row, header = rows[h_idx]
    width = max(len(header), max((len(v) for _, v in rows[h_idx + 1 :]), default=0))
    if sheet.kind == "csv":
        width = len(header)
    names = column_names(header, width, csv_mode=sheet.kind == "csv")
    header_text = [cell_text(header[j]) if j < len(header) else None for j in range(width)]

    if buf.names is None:
        buf.names, buf.header_text, buf.first_file = names, header_text, sheet.src_file
        buf.cols = [[] for _ in names]
    elif [x.casefold() for x in names] != [x.casefold() for x in buf.names]:
        raise IngestError(
            f"schema drift inside {buf.source.table}: {sheet.src_file}!{sheet.src_sheet} has columns {names}, "
            f"but {buf.first_file} has {buf.names}. Split the source family or add a mapping."
        )
    for j, nm in enumerate(names):
        buf.columns_meta.append(
            {
                "table_name": buf.source.table,
                "column_name": buf.names[j],
                "ordinal": j + 1,
                "src_file": sheet.src_file,
                "src_sheet": sheet.src_sheet,
                "src_col": j + 1,
                "src_col_letter": get_column_letter(j + 1) if sheet.kind == "xlsx" else None,
                "header_text": header_text[j],
                "header_row": header_row,
                "_name_in_file": nm,
            }
        )

    n_loaded = 0
    for i, (n, vals) in enumerate(rows):
        if i == h_idx:
            continue
        if is_blank_row(vals):
            reject(n, vals, REASON_BLANK)
            continue
        if i < h_idx:
            reject(n, vals, REASON_PREAMBLE, f"above header at row {header_row}")
            continue
        if sheet.kind == "csv" and len(vals) != width:
            reject(n, vals, REASON_FIELDS, f"{len(vals)} fields, header has {width}")
            continue
        buf.lineage["src_file"].append(sheet.src_file)
        buf.lineage["src_sheet"].append(sheet.src_sheet)
        buf.lineage["src_row"].append(n)
        for j in range(width):
            v = vals[j] if j < len(vals) else None
            buf.cols[j].append(cell_text(v))
            if v is not None and not isinstance(v, str):
                cell_types.append(
                    {
                        "table_name": buf.source.table,
                        "src_file": sheet.src_file,
                        "src_sheet": sheet.src_sheet,
                        "src_row": n,
                        "column_name": buf.names[j],
                        "native_type": "bool" if isinstance(v, bool) else type(v).__name__,
                        "number_format": sheet.formats.get((n, j + 1)),
                    }
                )
        n_loaded += 1
    return {
        "status": "ingested",
        "status_reason": None,
        "header_row": header_row,
        "n_loaded": n_loaded,
        "n_rejected": len(rejects) - n_rej0,
        "n_columns": width,
    }


def _reject_whole_sheet(
    sheet: SheetData, table: str, reason: str, detail: str, rejects: list[dict[str, Any]]
) -> dict[str, Any]:
    n0 = len(rejects)
    for n, vals in sheet.rows:
        rejects.append(
            {
                "src_file": sheet.src_file,
                "src_sheet": sheet.src_sheet,
                "src_row": n,
                "table_name": table,
                "reason": REASON_BLANK if is_blank_row(vals) else reason,
                "detail": detail,
                "raw_values": json.dumps([cell_text(v) for v in vals], ensure_ascii=False),
            }
        )
    return {
        "status": "rejected",
        "status_reason": f"{reason}: {detail}",
        "header_row": None,
        "n_loaded": 0,
        "n_rejected": len(rejects) - n0,
        "n_columns": 0,
    }


def _arrow(columns: dict[str, list[Any]], types: dict[str, pa.DataType]) -> pa.Table:
    return pa.table({k: pa.array(v, type=types.get(k, pa.string())) for k, v in columns.items()})


def _write_table(
    con: duckdb.DuckDBPyConnection, name: str, ddl_cols: list[tuple[str, str]], data: pa.Table
) -> None:
    ddl = ", ".join(f'"{c}" {t}' for c, t in ddl_cols)
    con.execute(f'CREATE OR REPLACE TABLE "{name}" ({ddl})')
    con.register("_ingest_tmp", data)
    try:
        con.execute(f'INSERT INTO "{name}" SELECT * FROM _ingest_tmp')
    finally:
        con.unregister("_ingest_tmp")


def ingest(
    raw_dir: Path = paths.RAW_DIR,
    db_path: Path = paths.DB_PATH,
    *,
    sources: tuple[Source, ...] = SOURCES,
    run: RunManifest | None = None,
) -> IngestResult:
    """Load ``raw_dir`` into ``db_path``. Pure function of its inputs; see module docstring."""
    t0 = time.perf_counter()
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    lineage_base = raw_dir.resolve().parent
    assigned = discover(raw_dir, sources)

    rejects: list[dict[str, Any]] = []
    cell_types: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    buffers: dict[str, TableBuffer] = {}

    for src in sources:
        buf = buffers[src.table] = TableBuffer(src)
        for path in assigned[src.table]:
            src_file = _rel(path, lineage_base)
            digest = sha256_file(path)
            if path.suffix.lower() == ".csv":
                sheets = [read_csv(path, src_file)]
            elif path.suffix.lower() in (".xlsx", ".xlsm"):
                sheets = read_xlsx(path, src_file)
            else:
                raise IngestError(f"unsupported file type: {src_file}")

            # Decide which sheet(s) of a workbook feed the table; every other sheet is accounted for too.
            if sheets[0].kind == "xlsx":
                if src.sheet is not None:
                    wanted = [s for s in sheets if s.src_sheet == src.sheet]
                    if not wanted:
                        raise IngestError(
                            f"{src_file}: sheet {src.sheet!r} not found (has {[s.src_sheet for s in sheets]})"
                        )
                else:
                    wanted = [s for s in sheets if any(not is_blank_row(v) for _, v in s.rows)]
                    if len(wanted) != 1:
                        raise IngestError(
                            f"{src_file}: expected exactly one non-empty sheet, found {[s.src_sheet for s in wanted]}; "
                            "set Source.sheet"
                        )
            else:
                wanted = sheets

            for sheet in sheets:
                if any(sheet is w for w in wanted):
                    info = _ingest_sheet(sheet, buf, rejects, cell_types)
                else:
                    info = _reject_whole_sheet(
                        sheet,
                        src.table,
                        REASON_UNMAPPED_SHEET,
                        f"sheet {sheet.src_sheet!r} is not mapped to {src.table}",
                        rejects,
                    )
                    if all(is_blank_row(v) for _, v in sheet.rows):
                        info["status"], info["status_reason"] = "empty", "sheet has no non-blank cells"
                n_phys = len(sheet.rows)
                accounted = (1 if info["header_row"] else 0) + info["n_loaded"] + info["n_rejected"]
                if accounted != n_phys:
                    raise IngestError(
                        f"{src_file}!{sheet.src_sheet}: {n_phys} rows but {accounted} accounted for"
                    )
                source_rows.append(
                    {
                        "src_file": src_file,
                        "src_sheet": sheet.src_sheet,
                        "file_kind": sheet.kind,
                        "table_name": src.table,
                        "sheet_index": sheet.sheet_index,
                        "sheet_state": sheet.sheet_state,
                        "status": info["status"],
                        "status_reason": info["status_reason"],
                        "header_row": info["header_row"],
                        "n_physical_rows": n_phys,
                        "n_loaded": info["n_loaded"],
                        "n_rejected": info["n_rejected"],
                        "n_columns": info["n_columns"],
                        "file_sha256": digest,
                        "file_bytes": path.stat().st_size,
                    }
                )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        for (tname,) in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_name LIKE 'raw\\_%' ESCAPE '\\'"
        ).fetchall():
            con.execute(f'DROP TABLE "{tname}"')
        tables: dict[str, int] = {}
        for tname, buf in buffers.items():
            names = buf.names or []
            cols: dict[str, list[Any]] = dict(buf.lineage)
            for j, nm in enumerate(names):
                cols[nm] = buf.cols[j]
            ddl = [
                ("src_file", "VARCHAR NOT NULL"),
                ("src_sheet", "VARCHAR NOT NULL"),
                ("src_row", "INTEGER NOT NULL"),
            ]
            ddl += [(nm, "VARCHAR") for nm in names]
            _write_table(con, tname, ddl, _arrow(cols, {"src_row": pa.int32()}))
            tables[tname] = buf.n

        rej_cols = ["src_file", "src_sheet", "src_row", "table_name", "reason", "detail", "raw_values"]
        _write_table(
            con,
            "raw_rejects",
            [
                ("src_file", "VARCHAR NOT NULL"),
                ("src_sheet", "VARCHAR NOT NULL"),
                ("src_row", "INTEGER NOT NULL"),
                ("table_name", "VARCHAR NOT NULL"),
                ("reason", "VARCHAR NOT NULL"),
                ("detail", "VARCHAR"),
                ("raw_values", "VARCHAR"),
            ],
            _arrow({c: [r[c] for r in rejects] for c in rej_cols}, {"src_row": pa.int32()}),
        )
        src_cols = list(source_rows[0].keys()) if source_rows else []
        int_cols = {
            "sheet_index",
            "header_row",
            "n_physical_rows",
            "n_loaded",
            "n_rejected",
            "n_columns",
            "file_bytes",
        }
        _write_table(
            con,
            "raw_sources",
            [(c, "BIGINT" if c in int_cols else "VARCHAR") for c in src_cols],
            _arrow({c: [r[c] for r in source_rows] for c in src_cols}, {c: pa.int64() for c in int_cols}),
        )
        meta = [m for b in buffers.values() for m in b.columns_meta]
        meta_cols = [
            "table_name",
            "column_name",
            "ordinal",
            "src_file",
            "src_sheet",
            "src_col",
            "src_col_letter",
            "header_text",
            "header_row",
        ]
        meta_int = {"ordinal", "src_col", "header_row"}
        _write_table(
            con,
            "raw_columns",
            [(c, "BIGINT" if c in meta_int else "VARCHAR") for c in meta_cols],
            _arrow({c: [m[c] for m in meta] for c in meta_cols}, {c: pa.int64() for c in meta_int}),
        )
        ct_cols = [
            "table_name",
            "src_file",
            "src_sheet",
            "src_row",
            "column_name",
            "native_type",
            "number_format",
        ]
        _write_table(
            con,
            "raw_cell_types",
            [(c, "INTEGER" if c == "src_row" else "VARCHAR") for c in ct_cols],
            _arrow({c: [r[c] for r in cell_types] for c in ct_cols}, {"src_row": pa.int32()}),
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    rej_counter: Counter[tuple[str, str]] = Counter((r["table_name"], r["reason"]) for r in rejects)
    result = IngestResult(db_path, tables, rej_counter, source_rows, round(time.perf_counter() - t0, 3))
    if run is not None:
        _record(run, result)
    return result


def _record(run: RunManifest, res: IngestResult) -> None:
    """Fill the already-open ``ingest`` stage record (see :func:`main`)."""
    rec = run.data["stages"]["ingest"]
    by_table: dict[str, dict[str, int]] = defaultdict(dict)
    for (t, reason), n in res.rejects.items():
        by_table[t][reason] = n
    from mcarisk.manifest import StageRecorder

    st = StageRecorder(rec)
    for t, n in res.tables.items():
        st.table(
            t, accepted=n, rejected=sum(by_table[t].values()), repaired=0, reasons={"rejected": by_table[t]}
        )
    st.extra("files", sorted({s["src_file"] for s in res.sources}))
    st.extra(
        "sheets",
        [
            {
                k: s[k]
                for k in (
                    "src_file",
                    "src_sheet",
                    "table_name",
                    "status",
                    "header_row",
                    "n_physical_rows",
                    "n_loaded",
                    "n_rejected",
                )
            }
            for s in res.sources
        ],
    )
    st.extra("db_path", res.db_path.as_posix())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest raw/ into DuckDB raw_* tables (as text, with lineage).")
    ap.add_argument("--raw", type=Path, default=paths.RAW_DIR)
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    args = ap.parse_args(argv)

    run = start_run(args.manifest, inputs_dir=args.raw)
    with run.stage("ingest"):
        res = ingest(args.raw, args.db, run=run)
    total_rej = sum(res.rejects.values())
    print(f"[ingest] {len(res.sources)} file/sheet(s) -> {args.db} in {res.seconds:.2f}s")
    for t, n in res.tables.items():
        rej = {r: c for (tt, r), c in res.rejects.items() if tt == t}
        print(f"  {t:<20} loaded={n:>7}  rejected={sum(rej.values())} {rej or ''}")
    print(f"  raw_rejects total={total_rej}; manifest -> {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
