"""Stage 4a ``ledger``: the cash ledger of METRICS.md §1. ``python -m mcarisk.ledger`` / ``make ledger``.

Every ACH row (``payments_resolved``, after L2 dedupe and L3 repair) maps to exactly one ledger event;
the tracker adds one ``commission_out`` per deal; the collections log adds ``modification`` and
``write_off`` events. Tables written (one transaction):

  ledger              one row per event: deal_id (per-deal attribution after the L3 repairs), event_date,
                      kind (§1), amount_cents (funder's view: cash out < 0, cash in > 0), is_cash, source,
                      source_id, lineage. ``internal_payoff`` keeps the exported sign (positive: it reduces
                      the prior deal's receivable) and is non-cash. Non-cash collections events carry
                      ``write_off`` = the written-off balance (positive, from the log's Balance column) and
                      ``modification`` = 0 with ``new_payment_cents``.
                      Returns are matched to the debit they reverse (``matched_event_id`` on both rows).
  deal_status_events  collections rows that are not §1 ledger events but drive §2.1 / D16:
                      ``default``, ``bankruptcy``, ``settlement`` (agreed amount). One row per log row.
  ledger_rejects      collections rows with no deal (Q4 unmatched): not attributed, kept with a reason.
  ledger_accounting   source table -> ledger / deal_status_events / ledger_rejects counts (identity checked).
  cal_sched           the payment-schedule calendar (``mcarisk.metrics.schedule``), read by the SQL views.

Tracker-missing deals (Q5, 44 deals): ACH gives their funding, debits, returns and payoffs, so their
cash events are complete. The tracker's commission % and advance are unknown, so **no
``commission_out`` event is created** for them; ``deal_metrics.commission_known`` is false and D5 is
NULL (see ``metrics/deal.py``). This is recorded in the manifest and in ``data_quality``.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from mcarisk import paths
from mcarisk.manifest import RunManifest, StageRecorder, open_run
from mcarisk.metrics.schedule import cal_index, cal_sched_rows
from mcarisk.resolve.stage import write_table

LINEAGE = ["src_file", "src_sheet", "src_row"]

KINDS = (
    "funding_out",
    "commission_out",
    "debit",
    "return",
    "early_payoff_cash",
    "internal_payoff",
    "settlement",
    "modification",
    "write_off",
)
CASH_KINDS = {"funding_out", "commission_out", "debit", "return", "early_payoff_cash", "settlement"}
# order of kinds on the same day (funding first, then remittances, then non-cash markers)
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
STATUS_TYPES = ("default", "bankruptcy", "settlement")

REJECT_UNLINKED = "collections_row_unlinked"
LOG_RETURN_UNMATCHED = "return_unmatched"

DDL = {
    "ledger": [
        ("event_id", "BIGINT NOT NULL"),
        ("deal_id", "BIGINT NOT NULL"),
        ("merchant_key", "VARCHAR"),
        ("event_date", "DATE NOT NULL"),
        ("kind", "VARCHAR NOT NULL"),
        ("amount_cents", "BIGINT NOT NULL"),
        ("is_cash", "BOOLEAN NOT NULL"),
        ("source", "VARCHAR NOT NULL"),
        ("source_id", "BIGINT NOT NULL"),
        ("matched_event_id", "BIGINT"),
        ("successor_deal_id", "BIGINT"),
        ("new_payment_cents", "BIGINT"),
        ("return_code", "VARCHAR"),
        ("deal_id_posted", "BIGINT"),
        ("attribution", "VARCHAR NOT NULL"),
        ("attribution_confidence", "DOUBLE"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "deal_status_events": [
        ("status_id", "BIGINT NOT NULL"),
        ("deal_id", "BIGINT NOT NULL"),
        ("event_date", "DATE NOT NULL"),
        ("event_type", "VARCHAR NOT NULL"),
        ("agreed_amount_cents", "BIGINT"),
        ("installments", "BIGINT"),
        ("attribution", "VARCHAR NOT NULL"),
        ("attribution_confidence", "DOUBLE"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "ledger_rejects": [
        ("source_table", "VARCHAR NOT NULL"),
        ("source_id", "BIGINT NOT NULL"),
        ("reason", "VARCHAR NOT NULL"),
        ("detail", "VARCHAR"),
        ("src_file", "VARCHAR NOT NULL"),
        ("src_sheet", "VARCHAR NOT NULL"),
        ("src_row", "INTEGER NOT NULL"),
    ],
    "ledger_accounting": [
        ("source_table", "VARCHAR NOT NULL"),
        ("n_source", "BIGINT NOT NULL"),
        ("n_ledger", "BIGINT NOT NULL"),
        ("n_status_events", "BIGINT NOT NULL"),
        ("n_rejected", "BIGINT NOT NULL"),
    ],
    "cal_sched": [
        ("code", "VARCHAR NOT NULL"),
        ("d", "DATE NOT NULL"),
        ("is_sched", "BOOLEAN NOT NULL"),
        ("n", "INTEGER NOT NULL"),
    ],
}


def commission_cents(advance_cents: int | None, rate: float | None) -> int | None:
    """D5 = advance x ISO commission %, rounded half-up to the cent. None when either input is unknown.

    The SQL mirror uses the same expression: ``floor(advance_cents * rate + 0.5)``."""
    if advance_cents is None or rate is None:
        return None
    return math.floor(advance_cents * rate + 0.5)


@dataclass
class LedgerBuild:
    ledger: list[dict[str, Any]]
    status_events: list[dict[str, Any]]
    rejects: list[dict[str, Any]]
    accounting: list[dict[str, Any]]
    unmatched_returns: list[int]  # event_ids


def build_ledger(
    deals: list[dict[str, Any]],
    payments: list[dict[str, Any]],
    successors: dict[int, int | None],
    collections: list[dict[str, Any]],
) -> LedgerBuild:
    """Pure: build the §1 ledger from resolved deals, resolved ACH rows, L1 successors and collections rows.

    ``payments``: ``payments_resolved`` rows; ``successors``: payment_id -> successor deal (internal payoffs);
    ``collections``: ``collections_terms`` rows joined with ``collections_std.balance_cents`` and the Q4
    ``collections_links`` method/confidence.
    """
    mk = {d["deal_id"]: d["merchant_key"] for d in deals}
    rows: list[dict[str, Any]] = []
    for p in payments:
        rows.append(
            {
                "deal_id": p["deal_id"],
                "merchant_key": p["merchant_key"] or mk.get(p["deal_id"]),
                "event_date": p["txn_date"],
                "kind": p["ledger_kind"],
                "amount_cents": int(p["amount_cents"]),
                "is_cash": p["ledger_kind"] in CASH_KINDS,
                "source": "ach",
                "source_id": p["payment_id"],
                "successor_deal_id": successors.get(p["payment_id"])
                if p["ledger_kind"] == "internal_payoff"
                else None,
                "new_payment_cents": None,
                "return_code": p["return_code"],
                "deal_id_posted": p["deal_id_posted"],
                "attribution": "l3_repair" if p["repaired"] else "posted",
                "attribution_confidence": 1.0,
                **{k: p[k] for k in LINEAGE},
            }
        )
    for d in deals:
        c = commission_cents(d["advance_cents"], d["commission_rate"])
        if c:  # zero-commission (house) deals have no AP payment, hence no event
            rows.append(
                {
                    "deal_id": d["deal_id"],
                    "merchant_key": d["merchant_key"],
                    "event_date": d["funding_date"],
                    "kind": "commission_out",
                    "amount_cents": -c,
                    "is_cash": True,
                    "source": "tracker",
                    "source_id": d["deal_id"],
                    "successor_deal_id": None,
                    "new_payment_cents": None,
                    "return_code": None,
                    "deal_id_posted": d["deal_id"],
                    "attribution": "tracker",
                    "attribution_confidence": 1.0,
                    **{k: d[k] for k in LINEAGE},
                }
            )
    status, rejects = [], []
    n_col_ledger = 0
    for c in collections:
        lin = {k: c[k] for k in LINEAGE}
        if c["deal_id"] is None:
            rejects.append(
                {
                    "source_table": "collections_terms",
                    "source_id": c["collection_id"],
                    "reason": REJECT_UNLINKED,
                    "detail": f"{c['event_type']} on {c['log_date']}: Q4 found no deal (see exceptions)",
                    **lin,
                }
            )
            continue
        attr = f"q4_{c['link_method']}" if c.get("link_method") else "q4"
        conf = c.get("link_confidence")
        et = c["event_type"]
        if et in ("modification", "write_off"):
            n_col_ledger += 1
            rows.append(
                {
                    "deal_id": c["deal_id"],
                    "merchant_key": mk.get(c["deal_id"]),
                    "event_date": c["log_date"],
                    "kind": et,
                    "amount_cents": int(c["balance_cents"] or 0) if et == "write_off" else 0,
                    "is_cash": False,
                    "source": "collections",
                    "source_id": c["collection_id"],
                    "successor_deal_id": None,
                    "new_payment_cents": c["new_payment_cents"] if et == "modification" else None,
                    "return_code": None,
                    "deal_id_posted": c["deal_id"],
                    "attribution": attr,
                    "attribution_confidence": conf,
                    **lin,
                }
            )
        elif et in STATUS_TYPES:
            agreed = None
            if et == "settlement":
                agreed = c["amount_cents"] if c["amount_cents"] is not None else c["settlement_amount_cents"]
            status.append(
                {
                    "status_id": c["collection_id"],
                    "deal_id": c["deal_id"],
                    "event_date": c["log_date"],
                    "event_type": et,
                    "agreed_amount_cents": agreed,
                    "installments": c["settlement_installments"] if et == "settlement" else None,
                    "attribution": attr,
                    "attribution_confidence": conf,
                    **lin,
                }
            )
        else:
            raise ValueError(f"unknown collections event_type {et!r} (collection_id {c['collection_id']})")

    rows.sort(key=lambda r: (r["deal_id"], r["event_date"], KIND_ORDER[r["kind"]], r["source_id"]))
    for i, r in enumerate(rows, start=1):
        r["event_id"] = i
        r["matched_event_id"] = None
    unmatched = match_returns(rows)

    n_status = len(status)
    accounting = [
        {
            "source_table": "payments_resolved",
            "n_source": len(payments),
            "n_ledger": len(payments),
            "n_status_events": 0,
            "n_rejected": 0,
        },
        {
            "source_table": "deals_resolved (commission_out)",
            "n_source": len(deals),
            "n_ledger": sum(r["kind"] == "commission_out" for r in rows),
            "n_status_events": 0,
            "n_rejected": 0,
        },
        {
            "source_table": "collections_terms",
            "n_source": len(collections),
            "n_ledger": n_col_ledger,
            "n_status_events": n_status,
            "n_rejected": len(rejects),
        },
    ]
    if n_col_ledger + n_status + len(rejects) != len(collections):
        raise AssertionError("collections rows not fully accounted for")
    if sum(r["source"] == "ach" for r in rows) != len(payments):
        raise AssertionError("ACH rows not fully accounted for")
    return LedgerBuild(rows, status, rejects, accounting, unmatched)


def match_returns(rows: list[dict[str, Any]]) -> list[int]:
    """Pair each ``return`` with the debit it reverses (same deal, same absolute amount, earlier date).

    Preference: the debit exactly 2 business days earlier (the usual ACH return lag), else the latest
    earlier unmatched debit of that amount. Sets ``matched_event_id`` on both rows. Returns the event_ids
    of returns that found no debit (they still count as cash; they just mark no debit as returned).
    """
    ci = cal_index()
    debits: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "debit":
            debits[(r["deal_id"], r["amount_cents"])].append(r)
    unmatched = []
    for r in sorted(
        (r for r in rows if r["kind"] == "return"), key=lambda r: (r["event_date"], r["event_id"])
    ):
        cands = [
            d for d in debits.get((r["deal_id"], -r["amount_cents"]), []) if d["matched_event_id"] is None
        ]
        cands = [d for d in cands if d["event_date"] < r["event_date"]]
        if not cands:
            unmatched.append(r["event_id"])
            continue
        lag2 = [d for d in cands if ci.bdays_between(d["event_date"], r["event_date"]) == 2]
        pick = max(lag2 or cands, key=lambda d: (d["event_date"], d["event_id"]))
        pick["matched_event_id"] = r["event_id"]
        r["matched_event_id"] = pick["event_id"]
    return unmatched


# ------------------------------------------------------------------------------------------ stage
def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def load_inputs(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    deals = _rows(
        con,
        "SELECT deal_id, merchant_key, funding_date, advance_cents, commission_rate, "
        "src_file, src_sheet, src_row FROM deals_resolved ORDER BY deal_id",
    )
    pays = _rows(
        con,
        "SELECT payment_id, txn_date, deal_id, deal_id_posted, repaired, merchant_key, ledger_kind, "
        "amount_cents, return_code, src_file, src_sheet, src_row FROM payments_resolved ORDER BY payment_id",
    )
    succ = dict(
        con.execute(
            "SELECT payment_id, successor_deal_id FROM payoff_classes WHERE payoff_class = 'internal_payoff'"
        ).fetchall()
    )
    cols = _rows(
        con,
        "SELECT t.collection_id, t.deal_id, t.log_date, t.event_type, t.new_payment_cents, "
        "t.settlement_amount_cents, t.settlement_installments, t.amount_cents, s.balance_cents, "
        "l.method AS link_method, l.confidence AS link_confidence, t.src_file, t.src_sheet, t.src_row "
        "FROM collections_terms t JOIN collections_std s USING (collection_id) "
        "LEFT JOIN collections_links l USING (collection_id) ORDER BY t.collection_id",
    )
    return {"deals": deals, "payments": pays, "successors": succ, "collections": cols}


@dataclass
class LedgerResult:
    counts: dict[str, int]
    by_kind: dict[str, int]
    unmatched_returns: int
    tracker_missing_no_commission: int
    seconds: float


def run_ledger(db_path: Path = paths.DB_PATH, *, run: RunManifest | None = None) -> LedgerResult:
    t0 = time.perf_counter()
    con = duckdb.connect(str(db_path))
    try:
        inp = load_inputs(con)
        b = build_ledger(inp["deals"], inp["payments"], inp["successors"], inp["collections"])
        n_tm = int(con.execute("SELECT COUNT(*) FROM deals_resolved WHERE tracker_missing").fetchone()[0])
        con.execute("BEGIN TRANSACTION")
        counts = {
            "ledger": write_table(con, "ledger", b.ledger, DDL["ledger"]),
            "deal_status_events": write_table(
                con, "deal_status_events", b.status_events, DDL["deal_status_events"]
            ),
            "ledger_rejects": write_table(con, "ledger_rejects", b.rejects, DDL["ledger_rejects"]),
            "ledger_accounting": write_table(
                con, "ledger_accounting", b.accounting, DDL["ledger_accounting"]
            ),
            "cal_sched": write_table(con, "cal_sched", cal_sched_rows(), DDL["cal_sched"]),
        }
        con.execute("COMMIT")
    finally:
        con.close()
    by_kind = dict(sorted(Counter(r["kind"] for r in b.ledger).items()))
    out = LedgerResult(counts, by_kind, len(b.unmatched_returns), n_tm, round(time.perf_counter() - t0, 3))
    if run is not None:
        st = StageRecorder(run.data["stages"]["ledger"])
        st.table("ledger", accepted=counts["ledger"])
        st.table(
            "deal_status_events",
            accepted=counts["deal_status_events"],
        )
        st.table(
            "ledger_rejects",
            accepted=0,
            rejected=counts["ledger_rejects"],
            reasons={"rejected": {REJECT_UNLINKED: counts["ledger_rejects"]}}
            if counts["ledger_rejects"]
            else None,
        )
        st.table("cal_sched", accepted=counts["cal_sched"])
        st.extra("ledger_by_kind", by_kind)
        st.extra("accounting", b.accounting)
        st.extra("unmatched_returns", len(b.unmatched_returns))
        st.note(
            f"{n_tm} tracker-missing deals have no commission_out event (commission % unknown); "
            "deal_metrics.commission_known = false"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the METRICS.md §1 cash ledger (Part 4).")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[ledger] {args.db} not found; run `make resolve` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("ledger"):
        res = run_ledger(args.db, run=run)
    print(f"[ledger] done in {res.seconds:.2f}s -> {args.db}")
    for k, v in res.counts.items():
        print(f"  {k:<20} {v:>8}")
    print(f"  by kind: {res.by_kind}; unmatched returns: {res.unmatched_returns}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
