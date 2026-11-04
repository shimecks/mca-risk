"""Stage 2, standardize: ``raw_*`` text tables -> typed canonical ``*_std`` tables + data-quality report.

``python -m mcarisk.standardize`` / ``make standardize``. Reads ``build/portfolio.duckdb`` (written by
ingest) and the column mappings in ``mappings/``; writes, in one transaction:

=================  ============================================================================
``deals_std``      one row per tracker row (2023, 2024, 2025 + 2026_YTD trackers), canonical terms
``payments_std``   one row per distinct ACH event after METRICS.md L2 dedupe (kept copy's lineage)
``payments_dupes`` one row per dropped overlap duplicate, with its own lineage and the kept row's
``scorecards_std`` one row per underwriting scorecard row
``collections_std`` one row per collections-log row
``std_rejects``    raw rows that could not be standardized, with a reason and the raw cells (JSON)
``std_log``        every nulled / repaired / derived field (one row per field event, with lineage)
``std_accounting`` per source table: raw rows = loaded + rejected (+ L2 duplicates)  (METRICS.md Q7)
``data_quality``   every rule Q1-Q7, L1-L3 (+ S* standardization checks): status, hits, sample rows
=================  ============================================================================

and ``build/dq_report.md``. Nothing here links entities, renewals or payoffs (Part 3): the deal on an
ACH row is kept as posted (``deal_id_posted``) and all free text a later part needs is kept verbatim.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from mcarisk import parse as P
from mcarisk import paths, schemas
from mcarisk.manifest import RunManifest, open_run
from mcarisk.mapping import (
    ACTION_REJECTED,
    ACTION_REPAIRED,
    Mapping,
    apply_row,
    load_lookups,
    load_mapping,
)

LINEAGE = ["src_file", "src_sheet", "src_row"]
TRACKER_MAPPINGS = ("tracker_2023", "tracker_2024", "tracker_2025")
REASON_DUPLICATE_DEAL = "duplicate_deal_id"
REASON_L2_DUPLICATE = "l2_duplicate"
N_SAMPLES = 5


class StandardizeError(RuntimeError):
    """Raised when standardization cannot proceed without guessing (missing raw column, broken identity)."""


@dataclass
class Mapped:
    """Output of mapping one source table."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    rejects: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    n_raw: int = 0


# ------------------------------------------------------------------------------------------ reading


def read_raw(con: duckdb.DuckDBPyConnection, mapping: Mapping) -> list[dict[str, Any]]:
    """All rows of the mapping's raw table (every column, as text) ordered by lineage."""
    have = [r[0] for r in con.execute(f'DESCRIBE "{mapping.source_table}"').fetchall()]
    missing = [c for c in mapping.columns if c not in have]
    if missing:
        raise StandardizeError(
            f"mappings/{mapping.name}.yaml reads {missing} but {mapping.source_table} has no such column"
        )
    cur = con.execute(f'SELECT * FROM "{mapping.source_table}" ORDER BY src_file, src_row')
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _lineage(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in LINEAGE}


def _raw_values(raw: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in raw.items() if k not in LINEAGE}, ensure_ascii=False)


def map_table(mapping: Mapping, raw_rows: list[dict[str, Any]], lookups: dict[str, dict[str, str]]) -> Mapped:
    """Apply a mapping to every raw row: each row ends up in ``rows`` or ``rejects`` (never dropped)."""
    out = Mapped(n_raw=len(raw_rows))
    cache: dict[tuple[str, Any], Any] = {}
    for raw in raw_rows:
        res = apply_row(mapping, raw, lookups, cache)
        lin = _lineage(raw)
        for e in res.logs:
            out.logs.append(
                {
                    **lin,
                    "target_table": mapping.target,
                    "field": e.field,
                    "action": e.action,
                    "reason": e.reason,
                    "raw_value": None if e.raw_value is None else str(e.raw_value),
                    "value": e.value,
                }
            )
        if res.reject is not None:
            out.rejects.append(
                {
                    **lin,
                    "source_table": mapping.source_table,
                    "target_table": mapping.target,
                    "reason": f"{res.reject.field}:{res.reject.reason}",
                    "detail": f"raw value {res.reject.raw_value!r}",
                    "raw_values": _raw_values(raw),
                }
            )
            continue
        flags = sorted({e.reason for e in res.logs if e.action != ACTION_REJECTED})
        row = {**res.values, **lin, "std_flags": ";".join(flags) or None}
        if mapping.source_family:
            row["source_family"] = mapping.source_family
        out.rows.append(row)
    return out


# --------------------------------------------------------------------------------------- dataframes


def _to_float(v: Any, nd: int) -> float | None:
    if v is None:
        return None
    return round(float(v), nd) if isinstance(v, (Decimal, float, int)) else v


def frame(name: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a DataFrame with exactly the schema's columns and canonical dtypes (column-wise, so a
    NULL integer never round-trips through a float NaN)."""
    data: dict[str, Any] = {}
    for c, col in schemas.SCHEMAS[name].columns.items():
        vals = [r.get(c) for r in rows]
        dt = str(col.dtype)
        if dt == "Int64":
            data[c] = pd.array([None if v is None else int(v) for v in vals], dtype="Int64")
        elif dt.startswith("float"):
            nd = 6 if c == "commission_rate" else 4
            data[c] = pd.Series([_to_float(v, nd) for v in vals], dtype="float64")
        else:
            data[c] = pd.Series(vals, dtype=object)
    return pd.DataFrame(data, columns=list(schemas.SCHEMAS[name].columns))


# -------------------------------------------------------------------------------------------- deals


def build_deals(con: duckdb.DuckDBPyConnection, mappings_dir: Path, lookups: dict) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    per_source: dict[str, dict[str, int]] = {}
    checks: dict[str, dict[str, Any]] = {}
    seen: dict[int, dict[str, Any]] = {}
    for name in TRACKER_MAPPINGS:
        m = load_mapping(name, mappings_dir)
        raw_rows = read_raw(con, m)
        mapped = map_table(m, raw_rows, lookups)
        raw_by_key = {(r["src_file"], r["src_row"]): r for r in raw_rows}
        n_dup = 0
        for r in mapped.rows:
            prev = seen.get(r["deal_id"])
            if prev is not None:  # same deal number twice: keep the first, reject the repeat with a reason
                n_dup += 1
                rejects.append(
                    {
                        **_lineage(r),
                        "source_table": m.source_table,
                        "target_table": "deals_std",
                        "reason": REASON_DUPLICATE_DEAL,
                        "detail": f"deal {r['deal_id']} already loaded from {prev['src_file']} row {prev['src_row']}",
                        "raw_values": _raw_values(raw_by_key[(r["src_file"], r["src_row"])]),
                    }
                )
                continue
            seen[r["deal_id"]] = r
            r["_source_table"] = m.source_table
            rows.append(r)
            _year_check(m, r, checks)
        rejects.extend(mapped.rejects)
        logs.extend(mapped.logs)
        per_source[m.source_table] = {
            "n_raw": mapped.n_raw,
            "n_loaded": len(mapped.rows) - n_dup,
            "n_rejected": len(mapped.rejects) + n_dup,
            "n_repaired": sum(1 for r in rows if r["_source_table"] == m.source_table and r.get("std_flags")),
        }
    _deal_consistency(rows, checks)
    return {"rows": rows, "rejects": rejects, "logs": logs, "per_source": per_source, "checks": checks}


def _hit(checks: dict[str, dict[str, Any]], key: str, row: dict[str, Any], detail: str) -> None:
    c = checks.setdefault(key, {"hits": 0, "samples": []})
    c["hits"] += 1
    if len(c["samples"]) < N_SAMPLES:
        c["samples"].append({**_lineage(row), "detail": detail})


def _year_check(m: Mapping, r: dict[str, Any], checks: dict[str, dict[str, Any]]) -> None:
    want = m.checks.get("funding_year") or (m.checks.get("funding_year_by_file") or {}).get(r["src_file"])
    fd = r.get("funding_date")
    if want and fd and fd.year != int(want):
        _hit(checks, "S3", r, f"deal {r['deal_id']} funded {fd} in the {want} tracker")


def _deal_consistency(rows: list[dict[str, Any]], checks: dict[str, dict[str, Any]]) -> None:
    """Cross-field checks. They only count (data_quality S*); they never change a value."""
    ids = {r["deal_id"]: r for r in rows}
    for r in rows:
        adv, fac, rtr = r.get("advance_cents"), r.get("factor"), r.get("rtr_cents")
        if adv is not None and fac is not None and rtr is not None:
            implied = int((Decimal(adv) * Decimal(fac)).quantize(Decimal(1)))
            if abs(implied - rtr) > 1:
                _hit(checks, "S1", r, f"deal {r['deal_id']}: RTR {rtr} vs advance x factor {implied} (cents)")
        term, pay, freq = r.get("term_bdays"), r.get("payment_cents"), r.get("frequency")
        if rtr and term and pay is not None and freq:
            implied = P.installment_cents(rtr, term, freq)
            if implied != pay:
                _hit(checks, "S2", r, f"deal {r['deal_id']}: payment {pay} vs RTR/term {implied} (cents)")
        prior = r.get("prior_deal_id")
        if prior is not None:
            p = ids.get(prior)
            if p is None:
                _hit(checks, "S4", r, f"deal {r['deal_id']}: prior deal {prior} has no tracker row")
            elif p["funding_date"] and r["funding_date"] and p["funding_date"] >= r["funding_date"]:
                _hit(checks, "S4", r, f"deal {r['deal_id']}: prior deal {prior} funded on/after it")
        missing = [
            f
            for f in ("advance_cents", "factor", "rtr_cents", "payment_cents", "frequency", "term_bdays")
            if r.get(f) is None
        ]
        if missing:
            _hit(checks, "S5", r, f"deal {r['deal_id']}: missing {missing}")


# ------------------------------------------------------------------------------------- ACH payments

_HOME = re.compile(r"ach_(\d{4})_(\d{2})\.csv$")


def _home_rank(row: dict[str, Any]) -> tuple[int, str, int]:
    """Which copy of an overlap duplicate to keep: the export for the transaction's own month first
    (0), then any other file; ties broken by (src_file, src_row) so the choice is deterministic."""
    m = _HOME.search(row["src_file"])
    d: date = row["txn_date"]
    home = 0 if (m and (int(m[1]), int(m[2])) == (d.year, d.month)) else 1
    return home, row["src_file"], row["src_row"]


def dedupe_payments(
    rows: list[dict[str, Any]], key: list[str], raw_dates: dict[tuple[str, int], str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """METRICS.md L2. Group rows on ``key`` (built from PARSED values: the date is a ``datetime.date``,
    so ``06/28/2024`` and ``2024-06-28`` collide), keep one row per group, return (kept, dupes).

    Kept rows get ``payment_id`` (1..n in date/deal/type/batch/amount order) and ``n_copies``; each
    dropped row keeps its own lineage plus the kept row's ``payment_id`` and lineage.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in key)].append(r)
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for members in groups.values():
        members = sorted(members, key=_home_rank)
        keep = {**members[0], "n_copies": len(members)}
        kept.append(keep)
        dropped.extend((d, keep) for d in members[1:])
    kept.sort(
        key=lambda r: (r["txn_date"], r["deal_id_posted"], r["txn_type"], r["batch_id"], r["amount_cents"])
    )
    for i, r in enumerate(kept, 1):
        r["payment_id"] = i
    raw_dates = raw_dates or {}
    dupes = [
        {
            **{k: d[k] for k in ("txn_date", "deal_id_posted", "amount_cents", "txn_type", "batch_id")},
            **_lineage(d),
            "payment_id": k["payment_id"],
            "date_text_raw": raw_dates.get((d["src_file"], d["src_row"]), d.get("date_format_raw")),
            "date_format_raw": d["date_format_raw"],
            "kept_src_file": k["src_file"],
            "kept_src_row": k["src_row"],
        }
        for d, k in dropped
    ]
    dupes.sort(key=lambda r: (r["src_file"], r["src_row"]))
    return kept, dupes


def build_payments(con: duckdb.DuckDBPyConnection, mappings_dir: Path, lookups: dict) -> dict[str, Any]:
    m = load_mapping("ach", mappings_dir)
    raw_rows = read_raw(con, m)
    mapped = map_table(m, raw_rows, lookups)
    date_col = m.fields["txn_date"]["column"]
    raw_dates = {(r["src_file"], r["src_row"]): r[date_col] for r in raw_rows}
    key = list(m.extra["dedupe_key"])
    kept, dupes = dedupe_payments(mapped.rows, key, raw_dates)

    checks: dict[str, dict[str, Any]] = {}
    signs = m.extra.get("sign_by_type") or {}
    for r in kept:
        want = signs.get(r["txn_type"])
        a = r["amount_cents"]
        if (want == "negative" and a >= 0) or (want == "positive" and a <= 0):
            _hit(checks, "S6", r, f"{r['txn_type']} amount {a} cents; expected {want}")
    # What dedupe on the raw date text would have missed (the July 2024 format-switch trap).
    raw_key = {
        (raw_dates[(r["src_file"], r["src_row"])], *(r[k] for k in key if k != "txn_date"))
        for r in mapped.rows
    }
    missed_without_normalization = len(raw_key) - len(kept)
    cross = [
        d
        for d in dupes
        if d["date_format_raw"] != _fmt_of(raw_dates[(d["kept_src_file"], d["kept_src_row"])])
    ]
    return {
        "rows": kept,
        "dupes": dupes,
        "rejects": mapped.rejects,
        "logs": mapped.logs,
        "n_raw": mapped.n_raw,
        "checks": checks,
        "missed_without_normalization": missed_without_normalization,
        "cross_format_dupes": cross,
    }


def _fmt_of(text: str) -> str | None:
    r = P.parse_date_fmt(text)
    return r[1] if isinstance(r, tuple) else None


# ------------------------------------------------------------------------------------ data quality

PENDING = "pending"
EVALUATED = "evaluated"
PARTIAL = "partial"

RULES: list[tuple[str, str, str]] = [
    ("Q1", "Merchant key (entity resolution)", "Part 3"),
    ("Q2", "Renewal link", "Part 3"),
    ("Q3", "Scorecard <-> deal", "Part 3"),
    ("Q4", "Collections <-> deal", "Part 3"),
    ("Q5", "Completeness: every deal with ACH activity has a tracker row", "Part 3"),
    ("Q6", "Stale tracker status vs derived outcome", "Part 4"),
    (
        "Q7",
        "Accounting: every raw row loaded, rejected with a reason, or repaired with a log entry",
        "Part 2",
    ),
    ("L1", "Internal vs cash payoff", "Part 3"),
    ("L2", "ACH dedupe after date normalization", "Part 2"),
    ("L3", "Misposted payments", "Part 3"),
    ("S1", "deals_std: RTR != advance x factor (+-1 cent)", "Part 2"),
    ("S2", "deals_std: payment != RTR / term installment", "Part 2"),
    ("S3", "deals_std: funding date outside the tracker's year", "Part 2"),
    ("S4", "deals_std: explicit prior deal unknown or not earlier", "Part 2"),
    ("S5", "deals_std: missing contract term field", "Part 2"),
    ("S6", "payments_std: amount sign unexpected for its type", "Part 2"),
]


def _samples(rows: list[dict[str, Any]], extra: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [{**_lineage(r), **{k: _jsonable(r.get(k)) for k in extra}} for r in rows[:N_SAMPLES]]


def _jsonable(v: Any) -> Any:
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def build_data_quality(
    deals: dict[str, Any],
    pays: dict[str, Any],
    score: dict[str, Any],
    coll: dict[str, Any],
    accounting: list[dict[str, Any]],
    all_logs: list[dict[str, Any]],
    all_rejects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    d_rows, p_rows = deals["rows"], pays["rows"]
    deal_ids = {r["deal_id"] for r in d_rows}
    checks = {**deals["checks"], **pays["checks"]}
    out: dict[str, dict[str, Any]] = {}

    def put(rule: str, status: str, hits: int | None, denom: int | None, samples: Any, detail: str) -> None:
        name, part = next((n, p) for r, n, p in RULES if r == rule)
        out[rule] = {
            "rule_id": rule,
            "rule": name,
            "owner_part": part,
            "status": status if status != PENDING else f"pending ({part})",
            "hits": hits,
            "denominator": denom,
            "sample_rows": json.dumps(samples, ensure_ascii=False),
            "detail": detail,
        }

    names = {r["merchant_name_raw"] for r in d_rows if r.get("merchant_name_raw")}
    names |= {r["merchant_name_raw"] for r in p_rows if r.get("merchant_name_raw")}
    put(
        "Q1",
        PENDING,
        None,
        None,
        [],
        f"{len(names)} distinct raw merchant-name strings in trackers + ACH to resolve",
    )

    explicit = [r for r in d_rows if r.get("prior_deal_id") is not None]
    put(
        "Q2",
        PARTIAL,
        len(explicit),
        len(d_rows),
        _samples(explicit, ("deal_id", "prior_deal_id")),
        "explicit links read from Prior Deal / renewal_of (method=explicit); notes regex and L1 inference are Part 3",
    )
    put(
        "Q3",
        PENDING,
        None,
        len(score["rows"]),
        [],
        f"{len(score['rows'])} scorecards keyed by name + app date only",
    )

    no_deal = [r for r in coll["rows"] if r.get("deal_id_raw") is None]
    put(
        "Q4",
        PARTIAL,
        len(no_deal),
        len(coll["rows"]),
        _samples(no_deal, ("log_date", "merchant_name_raw", "event_type")),
        "collections rows without a deal number (need merchant + open-deal matching in Part 3)",
    )

    fund_deals: dict[int, dict[str, Any]] = {}
    for r in p_rows:
        if r["deal_id_posted"] not in deal_ids:
            fund_deals.setdefault(r["deal_id_posted"], r)
    miss = sorted(fund_deals.values(), key=lambda r: r["deal_id_posted"])
    put(
        "Q5",
        PARTIAL,
        len(miss),
        len({r["deal_id_posted"] for r in p_rows}),
        _samples(miss, ("deal_id_posted", "txn_date", "txn_type")),
        "deal numbers with ACH activity but no tracker row (detected here; reconstruction from ACH is Part 3)",
    )
    st = Counter(r.get("status_norm") or "(unmapped)" for r in d_rows)
    put(
        "Q6",
        PENDING,
        None,
        len(d_rows),
        [],
        "typed status (status_norm): " + json.dumps(dict(sorted(st.items()))),
    )

    n_rej = sum(a["n_rejected"] for a in accounting)
    n_rep = sum(a["n_repaired"] for a in accounting)
    q7_samples = [{**_lineage(x), "reason": x["reason"]} for x in all_rejects[:N_SAMPLES]] + [
        {**_lineage(x), "field": x["field"], "action": x["action"], "reason": x["reason"]}
        for x in [lg for lg in all_logs if lg["action"] == ACTION_REPAIRED][:N_SAMPLES]
    ]
    put(
        "Q7",
        EVALUATED,
        n_rej + n_rep,
        sum(a["n_raw"] for a in accounting),
        q7_samples,
        "; ".join(
            f"{a['source_table']}: {a['n_raw']} raw = {a['n_loaded']} loaded + {a['n_rejected']} rejected"
            + (f" + {a['n_l2_duplicates']} L2 duplicates" if a["n_l2_duplicates"] else "")
            + f" ({a['n_repaired']} loaded rows repaired/derived/nulled)"
            for a in accounting
        ),
    )
    payoffs = [r for r in p_rows if r["txn_type"] == "PAYOFF"]
    desc = Counter(r.get("description") for r in payoffs)
    put(
        "L1",
        PENDING,
        None,
        len(payoffs),
        [],
        f"{len(payoffs)} PAYOFF rows to classify; descriptions {json.dumps(dict(sorted(desc.items())))} are hints only",
    )
    put(
        "L2",
        EVALUATED,
        len(pays["dupes"]),
        pays["n_raw"],
        [
            {
                **_lineage(x),
                "kept_src_file": x["kept_src_file"],
                "kept_src_row": x["kept_src_row"],
                "date_text_raw": x["date_text_raw"],
            }
            for x in (pays["cross_format_dupes"] + pays["dupes"])[:N_SAMPLES]
        ],
        f"{len(pays['dupes'])} overlap duplicates dropped; {len(pays['cross_format_dupes'])} of them pair an "
        f"MM/DD/YYYY row with a YYYY-MM-DD row (July 2024 export switch) and would survive a dedupe on the raw "
        f"date text ({pays['missed_without_normalization']} rows would be double counted)",
    )
    put("L3", PENDING, None, None, [], "deal kept as posted (deal_id_posted); reassignment is Part 3")
    for rule, _, _ in RULES:
        if rule.startswith("S"):
            c = checks.get(rule, {"hits": 0, "samples": []})
            denom = len(p_rows) if rule == "S6" else len(d_rows)
            put(
                rule,
                EVALUATED,
                c["hits"],
                denom,
                c["samples"],
                "consistency check: counted, values unchanged",
            )
    return [out[r] for r, _, _ in RULES]


def dq_report_md(dq: list[dict[str, Any]], accounting: list[dict[str, Any]], reasons: Counter) -> str:
    lines = [
        "# Data-quality report (Part 2: standardize)",
        "",
        "Rules from METRICS.md §1 (L1-L3) and §6 (Q1-Q7), plus S* cross-field checks added by standardization.",
        "Rules owned by later parts are listed as pending. Source: DuckDB table `data_quality`.",
        "",
        "| Rule | What | Status | Hits | Of | Detail |",
        "|---|---|---|---:|---:|---|",
    ]
    for r in dq:
        hits = "" if r["hits"] is None else f"{r['hits']:,}"
        den = "" if r["denominator"] is None else f"{r['denominator']:,}"
        lines.append(f"| {r['rule_id']} | {r['rule']} | {r['status']} | {hits} | {den} | {r['detail']} |")
    lines += [
        "",
        "## Row accounting (Q7)",
        "",
        "| Source | Target | Raw rows | Loaded | Rejected | L2 duplicates | Loaded rows with a log entry |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for a in accounting:
        lines.append(
            f"| `{a['source_table']}` | `{a['target_table']}` | {a['n_raw']:,} | {a['n_loaded']:,} | "
            f"{a['n_rejected']:,} | {a['n_l2_duplicates']:,} | {a['n_repaired']:,} |"
        )
    lines += [
        "",
        "## Field events (`std_log`)",
        "",
        "| Table | Field | Action | Reason | Rows |",
        "|---|---|---|---|---:|",
    ]
    for (t, f, a, rsn), n in sorted(reasons.items()):
        lines.append(f"| `{t}` | {f} | {a} | {rsn} | {n:,} |")
    lines += ["", "## Sample rows", ""]
    for r in dq:
        s = json.loads(r["sample_rows"])
        if s:
            lines.append(f"- **{r['rule_id']}**: " + "; ".join(json.dumps(x, ensure_ascii=False) for x in s))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------ writing

_DDL_TYPES = {"Int64": "BIGINT", "float64": "DOUBLE", "str": "VARCHAR"}


def _ddl(name: str) -> list[tuple[str, str]]:
    out = []
    for c, col in schemas.SCHEMAS[name].columns.items():
        if c.endswith("_date"):
            t = "DATE"
        elif c == "src_row" or c == "kept_src_row":
            t = "INTEGER"
        else:
            t = _DDL_TYPES.get(str(col.dtype), "VARCHAR")
        nn = " NOT NULL" if not col.nullable else ""
        out.append((c, t + nn))
    return out


def write_df(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame, ddl: list[tuple[str, str]]) -> None:
    con.execute(f'CREATE OR REPLACE TABLE "{name}" (' + ", ".join(f'"{c}" {t}' for c, t in ddl) + ")")
    if df.empty:
        return
    con.register("_std_tmp", df)
    try:
        sel = ", ".join(f'CAST("{c}" AS {t.split()[0]})' for c, t in ddl)
        con.execute(f'INSERT INTO "{name}" SELECT {sel} FROM _std_tmp')
    finally:
        con.unregister("_std_tmp")


def _plain_df(rows: list[dict[str, Any]], cols: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame([{c: r.get(c) for c, _ in cols} for r in rows], columns=[c for c, _ in cols])


REJECT_DDL = [
    ("src_file", "VARCHAR NOT NULL"),
    ("src_sheet", "VARCHAR NOT NULL"),
    ("src_row", "INTEGER NOT NULL"),
    ("source_table", "VARCHAR NOT NULL"),
    ("target_table", "VARCHAR NOT NULL"),
    ("reason", "VARCHAR NOT NULL"),
    ("detail", "VARCHAR"),
    ("raw_values", "VARCHAR"),
]
LOG_DDL = [
    ("src_file", "VARCHAR NOT NULL"),
    ("src_sheet", "VARCHAR NOT NULL"),
    ("src_row", "INTEGER NOT NULL"),
    ("target_table", "VARCHAR NOT NULL"),
    ("field", "VARCHAR NOT NULL"),
    ("action", "VARCHAR NOT NULL"),
    ("reason", "VARCHAR NOT NULL"),
    ("raw_value", "VARCHAR"),
    ("value", "VARCHAR"),
]
ACCOUNTING_DDL = [
    ("source_table", "VARCHAR NOT NULL"),
    ("target_table", "VARCHAR NOT NULL"),
    ("n_raw", "BIGINT NOT NULL"),
    ("n_loaded", "BIGINT NOT NULL"),
    ("n_rejected", "BIGINT NOT NULL"),
    ("n_l2_duplicates", "BIGINT NOT NULL"),
    ("n_repaired", "BIGINT NOT NULL"),
]
DQ_DDL = [
    ("rule_id", "VARCHAR NOT NULL"),
    ("rule", "VARCHAR NOT NULL"),
    ("owner_part", "VARCHAR NOT NULL"),
    ("status", "VARCHAR NOT NULL"),
    ("hits", "BIGINT"),
    ("denominator", "BIGINT"),
    ("sample_rows", "VARCHAR"),
    ("detail", "VARCHAR"),
]


@dataclass
class StdResult:
    tables: dict[str, int]
    accounting: list[dict[str, Any]]
    data_quality: list[dict[str, Any]]
    manifest_tables: dict[str, dict[str, Any]]
    seconds: float


def _repaired_reasons(rows: list[dict[str, Any]]) -> Counter:
    """One reason per loaded row that has any log entry (row-level reason = its sorted flags)."""
    return Counter(r["std_flags"] for r in rows if r.get("std_flags"))


def standardize(
    db_path: Path = paths.DB_PATH,
    *,
    mappings_dir: Path = paths.MAPPINGS_DIR,
    dq_report_path: Path | None = paths.DQ_REPORT_PATH,
    run: RunManifest | None = None,
) -> StdResult:
    t0 = time.perf_counter()
    lookups = load_lookups(mappings_dir)
    con = duckdb.connect(str(db_path))
    try:
        deals = build_deals(con, mappings_dir, lookups)
        pays = build_payments(con, mappings_dir, lookups)
        sc_map = load_mapping("scorecards", mappings_dir)
        score = map_table(sc_map, read_raw(con, sc_map), lookups)
        co_map = load_mapping("collections", mappings_dir)
        coll = map_table(co_map, read_raw(con, co_map), lookups)
        for i, r in enumerate(score.rows, 1):
            r["scorecard_id"] = i
        for i, r in enumerate(coll.rows, 1):
            r["collection_id"] = i
        # payments_std carries no std_flags column: ACH rows are never repaired, only rejected or deduped
        for r in pays["rows"]:
            r.pop("std_flags", None)

        accounting: list[dict[str, Any]] = []
        for src, a in deals["per_source"].items():
            accounting.append({"source_table": src, "target_table": "deals_std", "n_l2_duplicates": 0, **a})
        accounting.append(
            {
                "source_table": "raw_ach",
                "target_table": "payments_std",
                "n_raw": pays["n_raw"],
                "n_loaded": len(pays["rows"]),
                "n_rejected": len(pays["rejects"]),
                "n_l2_duplicates": len(pays["dupes"]),
                "n_repaired": 0,
            }
        )
        for src, tgt, mp in (
            ("raw_scorecards", "scorecards_std", score),
            ("raw_collections", "collections_std", coll),
        ):
            accounting.append(
                {
                    "source_table": src,
                    "target_table": tgt,
                    "n_raw": mp.n_raw,
                    "n_loaded": len(mp.rows),
                    "n_rejected": len(mp.rejects),
                    "n_l2_duplicates": 0,
                    "n_repaired": sum(1 for r in mp.rows if r.get("std_flags")),
                }
            )
        # Q7 identity, checked against the raw tables themselves (not just our own counters)
        for a in accounting:
            (n_db,) = con.execute(f'SELECT COUNT(*) FROM "{a["source_table"]}"').fetchone()
            if not (n_db == a["n_raw"] == a["n_loaded"] + a["n_rejected"] + a["n_l2_duplicates"]):
                raise StandardizeError(
                    f"row accounting broken for {a['source_table']}: {a} (table has {n_db})"
                )

        frames = {
            "deals_std": schemas.validate("deals_std", frame("deals_std", deals["rows"])),
            "payments_std": schemas.validate("payments_std", frame("payments_std", pays["rows"])),
            "payments_dupes": schemas.validate("payments_dupes", frame("payments_dupes", pays["dupes"])),
            "scorecards_std": schemas.validate("scorecards_std", frame("scorecards_std", score.rows)),
            "collections_std": schemas.validate("collections_std", frame("collections_std", coll.rows)),
        }
        all_rejects = deals["rejects"] + pays["rejects"] + score.rejects + coll.rejects
        all_logs = deals["logs"] + pays["logs"] + score.logs + coll.logs
        dq = build_data_quality(
            deals, pays, {"rows": score.rows}, {"rows": coll.rows}, accounting, all_logs, all_rejects
        )

        con.execute("BEGIN TRANSACTION")
        try:
            for name, df in frames.items():
                write_df(con, name, df, _ddl(name))
            write_df(con, "std_rejects", _plain_df(all_rejects, REJECT_DDL), REJECT_DDL)
            write_df(con, "std_log", _plain_df(all_logs, LOG_DDL), LOG_DDL)
            write_df(con, "std_accounting", _plain_df(accounting, ACCOUNTING_DDL), ACCOUNTING_DDL)
            write_df(con, "data_quality", _plain_df(dq, DQ_DDL), DQ_DDL)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    log_reasons = Counter((x["target_table"], x["field"], x["action"], x["reason"]) for x in all_logs)
    if dq_report_path is not None:
        dq_report_path.parent.mkdir(parents=True, exist_ok=True)
        dq_report_path.write_text(dq_report_md(dq, accounting, log_reasons), encoding="utf-8")

    # manifest: one entry per target table; rejected/repaired explained by reasons
    def rej_reasons(rejs: list[dict[str, Any]]) -> dict[str, int]:
        return dict(Counter(x["reason"] for x in rejs))

    deals_rows = deals["rows"]
    manifest_tables = {
        "deals_std": {
            "accepted": len(deals_rows),
            "rejected": len(deals["rejects"]),
            "repaired": sum(1 for r in deals_rows if r.get("std_flags")),
            "reasons": {
                "rejected": rej_reasons(deals["rejects"]),
                "repaired": dict(_repaired_reasons(deals_rows)),
            },
        },
        "payments_std": {
            "accepted": len(pays["rows"]),
            "rejected": len(pays["rejects"]) + len(pays["dupes"]),
            "repaired": 0,
            "reasons": {
                "rejected": {**rej_reasons(pays["rejects"]), REASON_L2_DUPLICATE: len(pays["dupes"])}
            },
        },
        "scorecards_std": {
            "accepted": len(score.rows),
            "rejected": len(score.rejects),
            "repaired": sum(1 for r in score.rows if r.get("std_flags")),
            "reasons": {
                "rejected": rej_reasons(score.rejects),
                "repaired": dict(_repaired_reasons(score.rows)),
            },
        },
        "collections_std": {
            "accepted": len(coll.rows),
            "rejected": len(coll.rejects),
            "repaired": sum(1 for r in coll.rows if r.get("std_flags")),
            "reasons": {
                "rejected": rej_reasons(coll.rejects),
                "repaired": dict(_repaired_reasons(coll.rows)),
            },
        },
    }
    res = StdResult(
        tables={k: len(v) for k, v in frames.items()},
        accounting=accounting,
        data_quality=dq,
        manifest_tables=manifest_tables,
        seconds=round(time.perf_counter() - t0, 3),
    )
    if run is not None:
        _record(run, res, log_reasons)
    return res


def _record(run: RunManifest, res: StdResult, log_reasons: Counter) -> None:
    from mcarisk.manifest import StageRecorder

    st = StageRecorder(run.data["stages"]["standardize"])
    for name, t in res.manifest_tables.items():
        st.table(
            name, accepted=t["accepted"], rejected=t["rejected"], repaired=t["repaired"], reasons=t["reasons"]
        )
    st.extra("accounting", res.accounting)
    st.extra(
        "field_events",
        [
            {"table": t, "field": f, "action": a, "reason": r, "rows": n}
            for (t, f, a, r), n in sorted(log_reasons.items())
        ],
    )
    st.extra(
        "data_quality", {r["rule_id"]: {"status": r["status"], "hits": r["hits"]} for r in res.data_quality}
    )
    st.note("payments_std rejected = unparseable rows + L2 overlap duplicates (details in payments_dupes)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Standardize raw_* tables into typed *_std tables + DQ report.")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--mappings", type=Path, default=paths.MAPPINGS_DIR)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    ap.add_argument("--dq-report", type=Path, default=paths.DQ_REPORT_PATH)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[standardize] {args.db} not found; run `make ingest` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("standardize"):
        res = standardize(args.db, mappings_dir=args.mappings, dq_report_path=args.dq_report, run=run)
    print(f"[standardize] done in {res.seconds:.2f}s -> {args.db}")
    for a in res.accounting:
        print(
            f"  {a['source_table']:<17} -> {a['target_table']:<16} raw={a['n_raw']:>7} loaded={a['n_loaded']:>7} "
            f"rejected={a['n_rejected']} l2_dupes={a['n_l2_duplicates']} logged={a['n_repaired']}"
        )
    print(f"  dq report -> {args.dq_report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
