"""Leakage guard check (METRICS.md §5 feature rules): perturb everything a feature may not use, rebuild the
features, and compare. Used by ``tests/test_model_build.py`` and eval E8 (on a *copy* of the database).

``perturb_post_funding(con)`` changes, in place:

* **ledger rows dated after the funding date of the deal whose features read them.** A deal's own ledger
  is never a feature input; a prior deal P's events feed its renewal N, so every event on P dated after
  N's funding date (or every event, if P has no renewal) has its amount and kind scrambled; and a fake
  *return* is added one day after N's funding date for each of P's debits on or before it (the
  look-ahead trap: under D17's ``attempt`` basis those returns would count), plus a fake modification;
* tracker ``status_raw`` / ``status_norm`` / notes and the resolved payoff fields;
* collections-derived servicing tables (``deal_status_events``, ``collections_*``), deleted;
* every outcome, metric and target table, dropped.
"""

from __future__ import annotations

from typing import Any

import duckdb

DROP_TABLES = (
    "deal_metrics",
    "deal_targets",
    "relationship_metrics",
    "portfolio_metrics",
    "target_metrics",
    "deal_attributes",
)
CLEAR_TABLES = ("deal_status_events", "collections_std", "collections_terms", "collections_links")

# The date after which each deal's ledger events are post-funding *for the deal that reads them*.
CUTOFF_SQL = """
CREATE OR REPLACE TEMP TABLE _cutoff AS
SELECT p.deal_id, MIN(n.funding_date) AS cutoff
FROM deals_resolved p LEFT JOIN deals_resolved n ON n.prior_deal_id = p.deal_id
GROUP BY p.deal_id
"""


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def perturb_post_funding(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Scramble every post-funding input (module doc). Returns counts of what was changed."""
    con.execute(CUTOFF_SQL)
    post = "event_date > COALESCE((SELECT cutoff FROM _cutoff c WHERE c.deal_id = ledger.deal_id), DATE '1900-01-01')"
    n_post = con.execute(f"SELECT COUNT(*) FROM ledger WHERE {post}").fetchone()[0]
    con.execute(
        f"UPDATE ledger SET amount_cents = amount_cents * 7 + 13, "
        f"kind = CASE kind WHEN 'debit' THEN 'return' WHEN 'return' THEN 'debit' ELSE kind END WHERE {post}"
    )
    mx = con.execute("SELECT MAX(event_id) FROM ledger").fetchone()[0]
    src = """
        FROM ledger l JOIN _cutoff c ON c.deal_id = l.deal_id
        WHERE c.cutoff IS NOT NULL AND l.kind = 'debit' AND l.event_date <= c.cutoff
    """
    n_fake = con.execute(f"SELECT COUNT(*) {src}").fetchone()[0]
    # a return of every pre-cutoff debit, posted the day after the cutoff (the look-ahead trap) ...
    con.execute(
        f"""INSERT INTO ledger SELECT l.* REPLACE (
            {mx} + ROW_NUMBER() OVER (ORDER BY l.event_id) AS event_id, c.cutoff + 1 AS event_date,
            'return' AS kind, -l.amount_cents AS amount_cents, l.event_id AS matched_event_id) {src}"""
    )
    # ... and a modification to a 1-cent payment on the same day
    con.execute(
        f"""INSERT INTO ledger SELECT l.* REPLACE (
            {mx + n_fake} + ROW_NUMBER() OVER (ORDER BY l.event_id) AS event_id, c.cutoff + 1 AS event_date,
            'modification' AS kind, 0 AS amount_cents, NULL AS matched_event_id, 1 AS new_payment_cents) {src}"""
    )
    rows = range(2 * n_fake)
    dcols = {r[0] for r in con.execute("DESCRIBE deals_resolved").fetchall()}
    sets = [
        f"{c} = {v}"
        for c, v in (
            ("status_raw", "'CHARGED OFF'"),
            ("status_norm", "'written_off'"),
            ("notes_raw", "'perturbed'"),
            ("closed_by_payoff", "NULL"),
            ("payoff_date", "NULL"),
            ("next_deal_id", "NULL"),
        )
        if c in dcols
    ]
    con.execute("UPDATE deals_resolved SET " + ", ".join(sets))
    have = _tables(con)
    for t in CLEAR_TABLES:
        if t in have:
            con.execute(f'DELETE FROM "{t}"')
    for t in DROP_TABLES:
        if t in have:
            con.execute(f'DROP TABLE IF EXISTS "{t}"')
    return {"post_funding_ledger_rows": int(n_post), "fake_events": len(rows)}


def feature_diff(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[tuple[int, str, Any, Any]]:
    """Every (deal_id, column, before, after) that differs (NaN == NaN)."""
    import math

    out = []
    bb = {r["deal_id"]: r for r in b}
    for r in a:
        s = bb.get(r["deal_id"])
        if s is None:
            out.append((r["deal_id"], "<row>", "present", "missing"))
            continue
        for k, v in r.items():
            w = s.get(k)
            same = v == w or (
                isinstance(v, float) and isinstance(w, float) and math.isnan(v) and math.isnan(w)
            )
            if not same:
                out.append((r["deal_id"], k, v, w))
    if len(b) != len(a):
        out.append((-1, "<n_rows>", len(a), len(b)))
    return out
