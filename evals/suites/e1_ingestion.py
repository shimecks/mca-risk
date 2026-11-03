"""E1 Ingestion (plan.md §4): every raw row is loaded or rejected with a reason; per-file counts reconcile.

The source side is counted **independently of the ingest code path**: xlsx files are read straight from
their XML parts with ``zipfile`` + ``ElementTree`` (no openpyxl), CSV files are split into lines from
raw bytes (row accounting) and parsed with pandas (cell values), never with ``mcarisk.ingest``.

Metrics
  rows_accounted_rate   share of non-blank source rows that appear exactly once as header, loaded row or
                        rejected row (with a non-empty reason) at the right (src_file, src_sheet, src_row).
                        Also requires that no loaded/rejected row points at a row that does not exist and
                        that rows rejected as blank are blank in the source.  [gate: 100%]
  files_reconciled_rate share of raw files whose per-sheet counts reconcile:
                        source non-blank rows == header + loaded + non-blank rejects, and raw_sources'
                        n_loaded equals COUNT(*) of the raw table for that file.     [gate: 100%]
  value_fidelity_rate   share of loaded cells whose text equals the independently read source cell.
  lineage_nonnull_rate  share of raw_* rows whose src_file/src_sheet/src_row are all non-null.
"""

from __future__ import annotations

import re
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput

SUITE_ID = "E1"
CSV_SHEET = "(csv)"
_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


# ------------------------------------------------------------------------------- independent readers


def xlsx_cells(path: Path) -> tuple[dict[str, dict[int, dict[int, tuple[str, str]]]], bool]:
    """{sheet_name: {row: {col: (type, text)}}} straight from the XML, plus the workbook's date1904 flag."""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        pr = wb.find("m:workbookPr", _NS)
        date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", _NS)}
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        out: dict[str, dict[int, dict[int, tuple[str, str]]]] = {}
        for sh in wb.find("m:sheets", _NS).findall("m:sheet", _NS):
            tgt = target[sh.get(f"{{{_NS['r']}}}id")].lstrip("/")
            part = tgt if tgt.startswith("xl/") else f"xl/{tgt}"
            rows: dict[int, dict[int, tuple[str, str]]] = {}
            for row in ET.fromstring(z.read(part)).iter(f"{{{_NS['m']}}}row"):
                rnum = int(row.get("r"))
                cells: dict[int, tuple[str, str]] = {}
                for c in row.findall("m:c", _NS):
                    col = _col_index(_REF.match(c.get("r")).group(1))
                    t = c.get("t", "n")
                    if t == "inlineStr":
                        is_ = c.find("m:is", _NS)
                        text = (
                            "".join(x.text or "" for x in is_.iter(f"{{{_NS['m']}}}t"))
                            if is_ is not None
                            else ""
                        )
                    else:
                        v = c.find("m:v", _NS)
                        if v is None or v.text is None:
                            continue
                        text = shared[int(v.text)] if t == "s" else v.text
                    cells[col] = (t, text)
                rows[rnum] = cells
            out[sh.get("name")] = rows
    return out, date1904


def _xml_row_blank(cells: dict[int, tuple[str, str]]) -> bool:
    return all(text.strip() == "" for _, text in cells.values())


def csv_lines(path: Path) -> list[bytes]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()  # trailing newline terminates the last record, it does not start a new one
    return [ln.rstrip(b"\r") for ln in lines]


def _csv_line_blank(line: bytes) -> bool:
    return line.strip(b' \t,"') == b""


# ------------------------------------------------------------------------------- suite


def run(ctx: EvalContext) -> SuiteOutput:
    if not ctx.db_path.exists():
        raise FileNotFoundError(f"{ctx.db_path} missing; run `make ingest` first")
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        return _run(ctx, con)
    finally:
        con.close()


def _run(ctx: EvalContext, con: duckdb.DuckDBPyConnection) -> SuiteOutput:
    lineage_base = ctx.raw_dir.resolve().parent
    files = sorted(p for p in ctx.raw_dir.rglob("*") if p.is_file() and not p.name.startswith((".", "~$")))
    rel = {p: p.resolve().relative_to(lineage_base).as_posix() for p in files}

    sources = con.sql("SELECT * FROM raw_sources").df()
    raw_tables = sorted(set(sources["table_name"]))
    loaded = pd.concat(
        [
            con.sql(f"SELECT '{t}' AS table_name, src_file, src_sheet, src_row FROM \"{t}\"").df()
            for t in raw_tables
        ],
        ignore_index=True,
    )
    rejects = con.sql("SELECT src_file, src_sheet, src_row, reason, table_name FROM raw_rejects").df()

    loaded_by = defaultdict(list)  # (file, sheet) -> rows
    for f, s, r in loaded[["src_file", "src_sheet", "src_row"]].itertuples(index=False):
        loaded_by[(f, s)].append(int(r))
    rej_by = defaultdict(list)
    for f, s, r, reason in rejects[["src_file", "src_sheet", "src_row", "reason"]].itertuples(index=False):
        rej_by[(f, s)].append((int(r), reason))
    header_by = {
        (r.src_file, r.src_sheet): (None if pd.isna(r.header_row) else int(r.header_row))
        for r in sources.itertuples()
    }
    nloaded_by = {(r.src_file, r.src_sheet): int(r.n_loaded) for r in sources.itertuples()}

    problems: list[str] = []
    per_file: dict[str, dict[str, Any]] = {}
    total_nonblank = accounted_ok = 0
    sheets_seen = 0
    xlsx_cache: dict[str, tuple[dict, bool]] = {}

    for p in files:
        f = rel[p]
        file_ok = True
        if p.suffix.lower() == ".csv":
            lines = csv_lines(p)
            sheet_rows = {CSV_SHEET: {i + 1: (not _csv_line_blank(ln)) for i, ln in enumerate(lines)}}
        elif p.suffix.lower() in (".xlsx", ".xlsm"):
            cells, d1904 = xlsx_cells(p)
            xlsx_cache[f] = (cells, d1904)
            sheet_rows = {
                s: {r: (not _xml_row_blank(c)) for r, c in rows.items()} for s, rows in cells.items()
            }
        else:
            problems.append(f"{f}: unsupported file type, not checked")
            per_file[f] = {"reconciled": False}
            continue

        for sheet, rowmap in sheet_rows.items():
            sheets_seen += 1
            key = (f, sheet)
            if key not in header_by:
                problems.append(f"{f}!{sheet}: sheet missing from raw_sources")
                file_ok = False
                total_nonblank += sum(rowmap.values())
                continue
            nonblank = {r for r, nb in rowmap.items() if nb}
            hdr = header_by[key]
            seen: dict[int, int] = defaultdict(int)
            if hdr is not None:
                seen[hdr] += 1
            for r in loaded_by.get(key, []):
                seen[r] += 1
            rej_reason = {}
            for r, reason in rej_by.get(key, []):
                seen[r] += 1
                rej_reason[r] = reason
            # every non-blank source row exactly once, rejects carry a reason
            ok_rows = {
                r for r in nonblank if seen.get(r) == 1 and (r not in rej_reason or bool(rej_reason[r]))
            }
            total_nonblank += len(nonblank)
            accounted_ok += len(ok_rows)
            # nothing points at a blank / nonexistent row, except blank rows rejected as blank
            for r in seen:
                if r in nonblank:
                    continue
                if rej_reason.get(r) == "blank_row" and seen[r] == 1:
                    continue
                problems.append(f"{f}!{sheet} row {r}: referenced by ingest but blank/nonexistent in source")
                file_ok = False
            if hdr is not None and hdr not in nonblank:
                problems.append(f"{f}!{sheet}: header row {hdr} is blank in source")
                file_ok = False
            n_loaded = len(loaded_by.get(key, []))
            n_rej_nonblank = sum(1 for r, _ in rej_by.get(key, []) if r in nonblank)
            if len(nonblank) != (1 if hdr is not None else 0) + n_loaded + n_rej_nonblank:
                problems.append(
                    f"{f}!{sheet}: source non-blank={len(nonblank)} vs header+loaded+rejected="
                    f"{(1 if hdr is not None else 0) + n_loaded + n_rej_nonblank}"
                )
                file_ok = False
            if nloaded_by.get(key) != n_loaded:
                problems.append(
                    f"{f}!{sheet}: raw_sources.n_loaded={nloaded_by.get(key)} but table has {n_loaded}"
                )
                file_ok = False
            if len(ok_rows) != len(nonblank):
                file_ok = False
        extra_sheets = {s for (ff, s) in header_by if ff == f} - set(sheet_rows)
        if extra_sheets:
            problems.append(f"{f}: raw_sources lists sheets not in file: {sorted(extra_sheets)}")
            file_ok = False
        per_file[f] = {"reconciled": file_ok}

    ingested_files = set(sources["src_file"])
    orphan = ingested_files - set(rel.values())
    if orphan:
        problems.append(f"raw_sources lists files not present in raw/: {sorted(orphan)}")

    fidelity_ok, fidelity_total, fidelity_examples = _fidelity(ctx, con, files, rel, xlsx_cache, raw_tables)

    lineage_total = lineage_ok = 0
    for t in raw_tables:
        n, ok = con.sql(
            f"SELECT COUNT(*), COUNT(*) FILTER (WHERE src_file IS NOT NULL AND src_sheet IS NOT NULL "
            f'AND src_row IS NOT NULL) FROM "{t}"'
        ).fetchone()
        lineage_total += n
        lineage_ok += ok

    n_files = len(files)
    metrics = {
        "rows_accounted_rate": (accounted_ok / total_nonblank if total_nonblank else 0.0)
        if not orphan
        else 0.0,
        "files_reconciled_rate": sum(v["reconciled"] for v in per_file.values()) / n_files
        if n_files
        else 0.0,
        "files_seen": n_files,
        "sheets_seen": sheets_seen,
        "rows_source_nonblank": total_nonblank,
        "rows_loaded": len(loaded),
        "rows_rejected": len(rejects),
        "value_fidelity_rate": fidelity_ok / fidelity_total if fidelity_total else 0.0,
        "lineage_nonnull_rate": lineage_ok / lineage_total if lineage_total else 0.0,
    }
    reasons = rejects.groupby(["table_name", "reason"]).size().to_dict()
    notes = [
        f"{n_files} files / {sheets_seen} sheets; {total_nonblank} non-blank source rows; "
        f"{len(loaded)} loaded + {len(rejects)} rejected"
    ]
    notes += [f"rejected {n} row(s) in {t} as {r}" for (t, r), n in sorted(reasons.items())]
    notes += [f"PROBLEM: {p}" for p in problems[:50]]
    notes += [f"value mismatch: {e}" for e in fidelity_examples[:10]]
    return SuiteOutput(
        metrics=metrics,
        details={
            "per_file": per_file,
            "problems": problems,
            "reject_reasons": {f"{t}:{r}": n for (t, r), n in reasons.items()},
        },
        notes=notes,
    )


# ------------------------------------------------------------------------------- value fidelity


def _excel_serial_to_iso(text: str, date1904: bool) -> str:
    serial = float(text)
    base = date(1904, 1, 1) if date1904 else date(1899, 12, 30)
    days = int(serial)
    d = base + timedelta(days=days)
    frac = serial - days
    if abs(frac) < 1e-9:
        return d.isoformat()
    secs = round(frac * 86400)
    return f"{d.isoformat()}T{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def _same(expected: tuple[str, str] | None, got: str | None, native: str | None, d1904: bool) -> bool:
    if expected is None:
        return got is None
    t, text = expected
    if t in ("s", "inlineStr", "str", "e"):
        return got == (text if text != "" else None)
    if t == "b":
        return got == ("TRUE" if text in ("1", "true") else "FALSE")
    if got is None:
        return False
    if native == "datetime":
        return got == _excel_serial_to_iso(text, d1904)
    try:
        return float(got) == float(text)
    except ValueError:
        return False


def _fidelity(ctx, con, files, rel, xlsx_cache, raw_tables) -> tuple[int, int, list[str]]:
    cols = con.sql("SELECT table_name, column_name, src_file, src_sheet, src_col FROM raw_columns").df()
    types = con.sql("SELECT src_file, src_sheet, src_row, column_name, native_type FROM raw_cell_types").df()
    native = {
        (r.src_file, r.src_sheet, int(r.src_row), r.column_name): r.native_type for r in types.itertuples()
    }
    ok = total = 0
    examples: list[str] = []
    for p in files:
        f = rel[p]
        fcols = cols[cols.src_file == f]
        if fcols.empty:
            continue
        table = fcols.table_name.iloc[0]
        data = con.execute(f'SELECT * FROM "{table}" WHERE src_file = ?', [f]).df()
        if p.suffix.lower() == ".csv":
            src = pd.read_csv(p, dtype=str, keep_default_na=False, header=None, encoding="utf-8-sig")
            src.index = src.index + 1  # 1-based record number == src_row
            for c in fcols.itertuples():
                exp = src.loc[data.src_row.to_numpy(), int(c.src_col) - 1].to_numpy()
                got = data[c.column_name].fillna("").to_numpy()
                match = exp == got
                ok += int(match.sum())
                total += len(match)
                if not match.all():
                    i = int((~match).argmax())
                    examples.append(
                        f"{f} row {int(data.src_row.iloc[i])} {c.column_name}: {exp[i]!r} != {got[i]!r}"
                    )
        else:
            cells, d1904 = xlsx_cache[f]
            for sheet, sub in data.groupby("src_sheet"):
                rows = cells[sheet]
                for c in fcols[fcols.src_sheet == sheet].itertuples():
                    col = int(c.src_col)
                    for r, got in zip(sub.src_row, sub[c.column_name], strict=True):
                        r = int(r)
                        got = None if pd.isna(got) else got
                        exp = rows.get(r, {}).get(col)
                        nat = native.get((f, sheet, r, c.column_name))
                        total += 1
                        if _same(exp, got, nat, d1904):
                            ok += 1
                        elif len(examples) < 20:
                            examples.append(f"{f}!{sheet} row {r} {c.column_name}: {exp!r} != {got!r}")
    return ok, total, examples
