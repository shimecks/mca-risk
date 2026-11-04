"""Stage 4b ``metrics``: deal, relationship and portfolio metrics. ``python -m mcarisk.metrics``.

Reads ``deals_resolved``, ``ledger``, ``deal_status_events`` (Parts 3-4a), computes the pure-Python
metrics (``metrics/deal.py``, ``relationship.py``, ``portfolio.py``) as of a date, and writes:

  deal_metrics          one row per deal funded on or before the as-of date (D1-D19, outcome)
  relationship_metrics  one row per resolved merchant (R1-R10)
  portfolio_metrics     long table of P1-P9 (P4 by vintage, P7 curves)

It also installs the SQL mirrors from ``sql/metrics/*.sql`` (table macros ``deal_metrics_sql``,
``relationship_metrics_sql``, ``portfolio_metrics_sql``) and views ``v_deal_metrics``,
``v_relationship_metrics``, ``v_portfolio_metrics`` at the same as-of date and D17 basis; updates the
``data_quality`` Q6 row (tracker status vs derived outcome); regenerates ``build/dq_report.md``; and
records the manifest stage ``metrics``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from mcarisk import paths
from mcarisk.manifest import RunManifest, StageRecorder, open_run
from mcarisk.metrics.deal import (
    AS_OF_DEFAULT,
    D17_BASES,
    D17_DEFAULT_BASIS,
    Deal,
    Event,
    StatusEvent,
    all_deal_metrics,
    d17_basis_from_env,
)
from mcarisk.metrics.portfolio import portfolio_metrics
from mcarisk.metrics.relationship import all_relationship_metrics
from mcarisk.resolve.stage import write_table

SQL_DIR = paths.ROOT / "sql" / "metrics"

DEAL_DDL = [
    ("deal_id", "BIGINT NOT NULL"),
    ("merchant_key", "VARCHAR"),
    ("as_of", "DATE NOT NULL"),
    ("funding_date", "DATE NOT NULL"),
    ("vintage", "VARCHAR"),
    ("frequency", "VARCHAR"),
    ("tracker_missing", "BOOLEAN"),
    ("commission_known", "BOOLEAN"),
    ("rtr_source", "VARCHAR"),
    ("d1_advance_cents", "BIGINT"),
    ("d2_rtr_cents", "BIGINT"),
    ("d3_fee_cents", "BIGINT"),
    ("d4_net_cash_to_merchant_cents", "BIGINT"),
    ("d5_commission_cents", "BIGINT"),
    ("d6_funder_cash_out_cents", "BIGINT"),
    ("d7_cash_from_merchant_cents", "BIGINT"),
    ("d8_internal_payoff_out_cents", "BIGINT"),
    ("d9_reported_collected_cents", "BIGINT"),
    ("d10_reported_collection_rate", "DOUBLE"),
    ("d11_adjusted_collection_rate", "DOUBLE"),
    ("d12_expected_collected_cents", "BIGINT"),
    ("d13_pace", "DOUBLE"),
    ("d14_return_rate_last20", "DOUBLE"),
    ("d14_returns", "BIGINT"),
    ("d14_attempts", "BIGINT"),
    ("d15_bdays_since_good_payment", "BIGINT"),
    ("last_good_payment_date", "DATE"),
    ("d16_default", "BOOLEAN"),
    ("d16_default_date", "DATE"),
    ("d16_recognition_date", "DATE"),
    ("d16_trigger", "VARCHAR"),
    ("renewal_date", "DATE"),
    ("successor_deal_id", "BIGINT"),
    ("d17_distressed", "BOOLEAN"),
    ("d17_d14_trigger", "BOOLEAN"),
    ("d17_d13_trigger", "BOOLEAN"),
    ("d17_mod_trigger", "BOOLEAN"),
    ("d17_d14_value", "DOUBLE"),
    ("d17_d14_returns", "BIGINT"),
    ("d17_d14_attempts", "BIGINT"),
    ("d17_d13_value", "DOUBLE"),
    ("d17_payment_ratio", "DOUBLE"),
    ("outcome", "VARCHAR NOT NULL"),
    ("outcome_date", "DATE"),
    ("is_closed", "BOOLEAN"),
    ("d18_bad_naive", "BOOLEAN"),
    ("d19_bad_economic", "BOOLEAN"),
    ("settlement_agreed_cents", "BIGINT"),
    ("settlement_received_cents", "BIGINT"),
    ("written_off_cents", "BIGINT"),
    ("remaining_rtr_cents", "BIGINT"),
    ("has_modification", "BOOLEAN"),
]
REL_DDL = [
    ("merchant_key", "VARCHAR NOT NULL"),
    ("as_of", "DATE NOT NULL"),
    ("first_funding_date", "DATE"),
    ("first_funding_quarter", "VARCHAR"),
    ("last_deal_id", "BIGINT"),
    ("last_outcome", "VARCHAR"),
    ("r1_funder_cash_out_cents", "BIGINT"),
    ("r2_cash_from_merchant_cents", "BIGINT"),
    ("r3_cash_multiple", "DOUBLE"),
    ("r4_net_cash_position_cents", "BIGINT"),
    ("r5_status", "VARCHAR"),
    ("r6_economic_exposure_cents", "BIGINT"),
    ("r7_contractual_balance_cents", "BIGINT"),
    ("r8_realized_loss_cents", "BIGINT"),
    ("r9_relationship_bad", "BOOLEAN"),
    ("r10_n_deals", "BIGINT"),
    ("r10_n_distressed_refi", "BIGINT"),
    ("r10_days_to_resolution", "BIGINT"),
    ("n_commission_unknown", "BIGINT"),
    ("reported_collected_cents", "BIGINT"),
]
PORT_DDL = [
    ("metric_id", "VARCHAR NOT NULL"),
    ("metric", "VARCHAR NOT NULL"),
    ("segment", "VARCHAR NOT NULL"),
    ("k", "BIGINT"),
    ("value", "DOUBLE"),
    ("numerator", "BIGINT"),
    ("denominator", "BIGINT"),
    ("unit", "VARCHAR"),
    ("as_of", "DATE"),
]

# Q6: tracker status (status_norm, as typed) -> derived outcomes it agrees with
STATUS_AGREES = {
    "renewed": {"renewed_healthy", "refinanced_distressed"},
    "paid_off": {"paid_in_full", "paid_in_full_early"},
    "settled": {"settled"},
    "defaulted": {"defaulted"},
    "written_off": {"written_off"},
    "active": {"active", "active_modified"},
}


# ------------------------------------------------------------------------------------------ loading
def load_model(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    deals = [
        Deal(
            deal_id=r[0],
            merchant_key=r[1],
            funding_date=r[2],
            advance_cents=r[3],
            rtr_cents=r[4],
            fee_cents=r[5],
            commission_rate=r[6],
            payment_cents=r[7] if r[7] is not None else r[9],
            frequency=r[8] if r[8] is not None else r[10],
            tracker_missing=bool(r[11]),
        )
        for r in con.execute(
            "SELECT deal_id, merchant_key, funding_date, advance_cents, rtr_cents, fee_cents, commission_rate, "
            "payment_cents, frequency, payment_cents_ach, frequency_ach, tracker_missing FROM deals_resolved"
        ).fetchall()
    ]
    events = [
        Event(*r)
        for r in con.execute(
            "SELECT event_id, deal_id, event_date, kind, amount_cents, matched_event_id, new_payment_cents, "
            "successor_deal_id FROM ledger"
        ).fetchall()
    ]
    status = [
        StatusEvent(*r)
        for r in con.execute(
            "SELECT status_id, deal_id, event_date, event_type, agreed_amount_cents FROM deal_status_events"
        ).fetchall()
    ]
    cash = con.execute(
        "SELECT merchant_key, event_date, kind, amount_cents FROM ledger WHERE is_cash"
    ).fetchall()
    return {"deals": deals, "events": events, "status": status, "cash": cash}


def compute(
    con: duckdb.DuckDBPyConnection, *, as_of: date = AS_OF_DEFAULT, d17_basis: str = D17_DEFAULT_BASIS
) -> tuple[list[dict], list[dict], list[dict]]:
    m = load_model(con)
    dm = all_deal_metrics(m["deals"], m["events"], m["status"], as_of=as_of, d17_basis=d17_basis)
    rm = all_relationship_metrics(dm)
    pm = portfolio_metrics(dm, rm, m["cash"], as_of=as_of)
    return dm, rm, pm


# ------------------------------------------------------------------------------------------ SQL
def install_sql(
    con: duckdb.DuckDBPyConnection, *, as_of: date = AS_OF_DEFAULT, d17_basis: str = D17_DEFAULT_BASIS
) -> list[str]:
    """Create the table macros from ``sql/metrics/*.sql`` and the ``v_*`` views at ``as_of``."""
    files = sorted(SQL_DIR.glob("*.sql"))
    for f in files:
        con.execute(f.read_text(encoding="utf-8"))
    flag = "true" if d17_basis == "attempt" else "false"
    lit = f"DATE '{as_of.isoformat()}'"
    for v, mac in (
        ("v_deal_metrics", "deal_metrics_sql"),
        ("v_relationship_metrics", "relationship_metrics_sql"),
        ("v_portfolio_metrics", "portfolio_metrics_sql"),
    ):
        con.execute(f"CREATE OR REPLACE VIEW {v} AS SELECT * FROM {mac}({lit}, {flag})")
    return [f.name for f in files]


# ------------------------------------------------------------------------------------------ Q6 + report
def stale_status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Q6: tracker status (as typed) vs the §2.1 outcome. Disagreements are counted, never trusted."""
    rows = con.execute(
        "SELECT d.deal_id, d.status_norm, d.status_raw, m.outcome, d.src_file, d.src_sheet, d.src_row "
        "FROM deals_resolved d JOIN deal_metrics m USING (deal_id) WHERE d.status_norm IS NOT NULL"
    ).fetchall()
    pairs: Counter = Counter()
    hits, samples = 0, []
    for did, sn, sr, oc, f, sh, rr in rows:
        ok = oc in STATUS_AGREES.get(sn, set())
        if not ok:
            hits += 1
            pairs[(sn, oc)] += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "src_file": f,
                        "src_sheet": sh,
                        "src_row": rr,
                        "deal_id": did,
                        "status_raw": sr,
                        "outcome": oc,
                    }
                )
    renewed_typed_paid = sum(
        n for (sn, oc), n in pairs.items() if sn == "paid_off" and oc in STATUS_AGREES["renewed"]
    )
    n_renewed = int(
        con.execute(
            "SELECT COUNT(*) FROM deal_metrics m JOIN deals_resolved d USING (deal_id) "
            "WHERE m.outcome IN ('renewed_healthy', 'refinanced_distressed') AND d.status_norm IS NOT NULL"
        ).fetchone()[0]
    )
    top = ", ".join(f"{sn}->{oc}: {n}" for (sn, oc), n in pairs.most_common(8))
    return {
        "status": "evaluated",
        "hits": hits,
        "denominator": len(rows),
        "sample_rows": json.dumps(samples, default=str),
        "detail": (
            f"{hits} of {len(rows)} tracker statuses disagree with the §2.1 outcome (as of the metrics as-of "
            f"date); {renewed_typed_paid} of {n_renewed} renewed deals are typed paid off. Top pairs: {top}"
        ),
        "pairs": {f"{sn}->{oc}": n for (sn, oc), n in sorted(pairs.items())},
    }


def dq_report(con: duckdb.DuckDBPyConnection) -> str:
    from mcarisk.standardize import dq_report_md

    def rows(sql: str) -> list[dict]:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    dq = rows("SELECT * FROM data_quality")
    order = {r: i for i, r in enumerate(["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "L1", "L2", "L3"])}
    dq.sort(key=lambda r: (order.get(r["rule_id"], 99), r["rule_id"]))
    acc = rows("SELECT * FROM std_accounting")
    reasons: Counter = Counter()
    for t, f, a, rsn, n in con.execute(
        "SELECT target_table, field, action, reason, COUNT(*) FROM std_log GROUP BY ALL"
    ).fetchall():
        reasons[(t, f, a, rsn)] = n
    md = dq_report_md(dq, acc, reasons).replace(
        "# Data-quality report (Part 2: standardize)",
        "# Data-quality report (Parts 2-4: standardize, resolve, ledger + metrics)",
    )
    lines = ["", "## Resolution, ledger and metrics tables (Parts 3-4)", "", "| Table | Rows |", "|---|---:|"]
    for t in (
        "deals_resolved", "merchants", "merchant_aliases", "merchant_merges", "deal_links", "payoff_classes",
        "payments_resolved", "misposting_repairs", "scorecard_links", "collections_links", "notes_extractions",
        "collections_terms", "exceptions", "ledger", "deal_status_events", "ledger_rejects", "deal_metrics",
        "relationship_metrics", "portfolio_metrics",
    ):  # fmt: skip
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        lines.append(f"| `{t}` | {n:,} |")
    lines += ["", "## Ledger accounting (Part 4)", "", "| Source | Rows | Ledger | Status events | Rejected |",
              "|---|---:|---:|---:|---:|"]  # fmt: skip
    for a in rows("SELECT * FROM ledger_accounting"):
        lines.append(
            f"| `{a['source_table']}` | {a['n_source']:,} | {a['n_ledger']:,} | {a['n_status_events']:,} | "
            f"{a['n_rejected']:,} |"
        )
    lines += ["", "## Derived outcomes (§2.1)", "", "| Outcome | Deals |", "|---|---:|"]
    for oc, n in con.execute(
        "SELECT outcome, COUNT(*) FROM deal_metrics GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        lines.append(f"| {oc} | {n:,} |")
    ex = con.execute(
        "SELECT rule_id, reason, COUNT(*) FROM exceptions GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    lines += ["", "## Review queue (`exceptions`)", "", "| Rule | Reason | Rows |", "|---|---|---:|"]
    lines += [f"| {r} | {rsn} | {n:,} |" for r, rsn, n in ex]
    used = con.execute("SELECT extractor, COUNT(*) FROM collections_terms GROUP BY 1 ORDER BY 1").fetchall()
    lines += [
        "",
        "Notes extraction (collections_terms by extractor): " + ", ".join(f"{e}: {n}" for e, n in used),
        "",
    ]
    return md + "\n".join(lines)


# ------------------------------------------------------------------------------------------ stage
@dataclass
class MetricsResult:
    counts: dict[str, int]
    outcomes: dict[str, int]
    headline: dict[str, Any]
    q6: dict[str, Any]
    d17_basis: str
    sql_files: list[str]
    seconds: float


HEADLINE = [
    ("P1", "reported_collection_rate", "all"),
    ("P1", "adjusted_collection_rate", "all"),
    ("P2", "renewal_masking_gap_pp", "all"),
    ("P3", "internal_payoff_share", "all"),
    ("P4", "bad_rate_naive", "all"),
    ("P4", "bad_rate_economic", "all"),
    ("P5", "resolved_bad_share", "all"),
    ("P5", "resolved_cash_multiple", "all"),
    ("P6", "economic_exposure_cents", "all"),
    ("P6", "contractual_balance_cents", "all"),
    ("P8", "distressed_share_of_renewals", "all"),
    ("P8", "bad_rate_chains_with_distressed", "all"),
]


def run_metrics(
    db_path: Path = paths.DB_PATH,
    *,
    as_of: date = AS_OF_DEFAULT,
    d17_basis: str | None = None,
    dq_report_path: Path | None = paths.DQ_REPORT_PATH,
    run: RunManifest | None = None,
) -> MetricsResult:
    t0 = time.perf_counter()
    basis = d17_basis or d17_basis_from_env()
    if basis not in D17_BASES:
        raise ValueError(f"d17 basis must be one of {D17_BASES}")
    con = duckdb.connect(str(db_path))
    try:
        dm, rm, pm = compute(con, as_of=as_of, d17_basis=basis)
        con.execute("BEGIN TRANSACTION")
        counts = {
            "deal_metrics": write_table(con, "deal_metrics", dm, DEAL_DDL),
            "relationship_metrics": write_table(con, "relationship_metrics", rm, REL_DDL),
            "portfolio_metrics": write_table(con, "portfolio_metrics", pm, PORT_DDL),
        }
        sql_files = install_sql(con, as_of=as_of, d17_basis=basis)
        q6 = stale_status(con)
        con.execute(
            "UPDATE data_quality SET status = ?, hits = ?, denominator = ?, sample_rows = ?, detail = ?, "
            "owner_part = 'Part 4' WHERE rule_id = 'Q6'",
            [q6["status"], q6["hits"], q6["denominator"], q6["sample_rows"], q6["detail"]],
        )
        con.execute("COMMIT")
        if dq_report_path is not None:
            Path(dq_report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(dq_report_path).write_text(dq_report(con), encoding="utf-8")
    finally:
        con.close()
    outcomes = dict(Counter(d["outcome"] for d in dm).most_common())
    idx = {(r["metric_id"], r["metric"], r["segment"]): r["value"] for r in pm if r["k"] is None}
    headline = {f"{a}.{b}": idx.get((a, b, c)) for a, b, c in HEADLINE}
    out = MetricsResult(counts, outcomes, headline, q6, basis, sql_files, round(time.perf_counter() - t0, 3))
    if run is not None:
        st = StageRecorder(run.data["stages"]["metrics"])
        for k, v in counts.items():
            st.table(k, accepted=v)
        st.extra("as_of", as_of.isoformat())
        st.extra("d17_basis", basis)
        st.extra("outcomes", outcomes)
        st.extra("headline", headline)
        st.extra(
            "q6_stale_status", {"hits": q6["hits"], "denominator": q6["denominator"], "pairs": q6["pairs"]}
        )
        st.extra("sql_files", sql_files)
        st.extra(
            "tracker_missing",
            {
                "deals": sum(d["tracker_missing"] for d in dm),
                "commission_unknown": sum(not d["commission_known"] for d in dm),
                "rtr_source": dict(Counter(str(d["rtr_source"]) for d in dm if d["tracker_missing"])),
            },
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deal / relationship / portfolio metrics (Part 4).")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    ap.add_argument("--dq-report", type=Path, default=paths.DQ_REPORT_PATH)
    ap.add_argument("--as-of", type=date.fromisoformat, default=AS_OF_DEFAULT)
    ap.add_argument("--d17-basis", choices=list(D17_BASES), default=None)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[metrics] {args.db} not found; run `make ledger` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("metrics"):
        res = run_metrics(
            args.db, as_of=args.as_of, d17_basis=args.d17_basis, dq_report_path=args.dq_report, run=run
        )
    print(
        f"[metrics] done in {res.seconds:.2f}s (as of {args.as_of}, D17 basis {res.d17_basis}) -> {args.db}"
    )
    for k, v in res.counts.items():
        print(f"  {k:<22} {v:>7}")
    print(f"  outcomes: {res.outcomes}")
    for k, v in res.headline.items():
        print(f"  {k:<45} {v}")
    print(f"  Q6: {res.q6['hits']} of {res.q6['denominator']} tracker statuses disagree")
    return 0
