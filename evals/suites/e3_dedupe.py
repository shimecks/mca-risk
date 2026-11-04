"""E3 Dedupe (plan.md §4): ``payments_std`` count and sum equal the true ledger (excluding commissions).
Gate: exact (count diff 0, sum diff 0 cents).

What is compared, precisely
---------------------------
* Pipeline side: every row of ``payments_std`` (ACH rows after METRICS.md L2 dedupe), ``amount_cents``
  as exported (funder perspective: FUNDING and RETURN negative; DEBIT, PAYOFF, SETTLEMENT positive).
* Truth side: every row of ``ground_truth/cashflows.csv`` whose ``kind`` is ACH-sourced, i.e. all kinds
  except ``commission_out`` (paid through accounts payable, never in the ACH exports; METRICS.md §1):
  ``funding_out`` <-> FUNDING, ``debit`` <-> DEBIT, ``return`` <-> RETURN, ``internal_payoff`` and
  ``early_payoff_cash`` <-> PAYOFF (the export does not distinguish them; L1 is Part 3),
  ``settlement`` <-> SETTLEMENT. Truth amounts are dollars with 2 decimals -> ``round(amount * 100)``.
  ``internal_payoff`` is positive in both (it is non-cash, but it is an ACH PAYOFF row).
* Deal attribution is NOT part of the gate: 250 debits are posted to the prior deal in the raw exports
  (L3, Part 3), so a per-deal comparison would differ by design.

Report-only: per-type count/sum diffs; multiset match on (date, type, amount) ignoring the deal (should
be exact too); multiset match including the posted deal (differs only by the mispostings); and the
counterfactual of deduplicating on the RAW date text (the July 2024 format-switch trap).
"""

from __future__ import annotations

from collections import Counter
from datetime import date

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput

SUITE_ID = "E3"
KIND_TO_TYPE = {
    "funding_out": "FUNDING",
    "debit": "DEBIT",
    "return": "RETURN",
    "internal_payoff": "PAYOFF",
    "early_payoff_cash": "PAYOFF",
    "settlement": "SETTLEMENT",
}


def run(ctx: EvalContext) -> SuiteOutput:
    cf = pd.read_csv(ctx.ground_truth_dir / "cashflows.csv")
    cf = cf[cf["kind"] != "commission_out"].copy()
    unknown = sorted(set(cf["kind"]) - set(KIND_TO_TYPE))
    if unknown:
        raise RuntimeError(f"cashflows.csv has kinds E3 does not know how to compare: {unknown}")
    cf["type"] = cf["kind"].map(KIND_TO_TYPE)
    cf["cents"] = (cf["amount"] * 100).round().astype("int64")
    cf["d"] = cf["date"].map(date.fromisoformat)

    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        ps = con.execute("SELECT txn_date, deal_id_posted, amount_cents, txn_type FROM payments_std").df()
        (n_raw,) = con.execute("SELECT COUNT(*) FROM raw_ach").fetchone()
        (n_dupes,) = con.execute("SELECT COUNT(*) FROM payments_dupes").fetchone()
        (n_raw_text_distinct,) = con.execute(
            'SELECT COUNT(*) FROM (SELECT DISTINCT "Trans Date", "Deal", "Amount", "Type", "Batch ID" FROM raw_ach)'
        ).fetchone()
    finally:
        con.close()
    ps["d"] = [pd.Timestamp(v).date() for v in ps["txn_date"]]

    count_diff = abs(len(ps) - len(cf))
    sum_diff = abs(int(ps["amount_cents"].sum()) - int(cf["cents"].sum()))

    by_type = {}
    for t in sorted(set(KIND_TO_TYPE.values())):
        a, b = ps[ps["txn_type"] == t], cf[cf["type"] == t]
        by_type[t] = {
            "count": len(a),
            "true_count": len(b),
            "sum_cents": int(a["amount_cents"].sum()),
            "true_sum_cents": int(b["cents"].sum()),
        }

    def multiset(df_keys: list[tuple]) -> Counter:
        return Counter(df_keys)

    got_nodeal = multiset(list(zip(ps["d"], ps["txn_type"], ps["amount_cents"], strict=True)))
    want_nodeal = multiset(list(zip(cf["d"], cf["type"], cf["cents"], strict=True)))
    got_deal = multiset(
        list(zip(ps["d"], ps["txn_type"], ps["amount_cents"], ps["deal_id_posted"], strict=True))
    )
    want_deal = multiset(list(zip(cf["d"], cf["type"], cf["cents"], cf["deal_id"], strict=True)))
    nodeal_unmatched = sum((got_nodeal - want_nodeal).values()) + sum((want_nodeal - got_nodeal).values())
    deal_unmatched_rows = sum((got_deal - want_deal).values())

    metrics = {
        "payments_count_abs_diff": count_diff,
        "payments_sum_abs_diff_cents": sum_diff,
        "payments_count": len(ps),
        "true_ledger_count": len(cf),
        "payments_sum_cents": int(ps["amount_cents"].sum()),
        "true_ledger_sum_cents": int(cf["cents"].sum()),
        "raw_ach_rows": int(n_raw),
        "l2_duplicates_dropped": int(n_dupes),
        "by_type": by_type,
        "multiset_unmatched_ignoring_deal": nodeal_unmatched,
        "rows_on_a_different_deal_than_truth": deal_unmatched_rows,
        "count_if_deduped_on_raw_date_text": int(n_raw_text_distinct),
    }
    notes = [
        f"payments_std {len(ps):,} rows / {int(ps['amount_cents'].sum()):,} cents vs true ACH-sourced ledger "
        f"{len(cf):,} rows / {int(cf['cents'].sum()):,} cents (commission_out excluded)",
        f"raw_ach {int(n_raw):,} rows -> {int(n_dupes):,} L2 duplicates dropped",
        f"(date, type, amount) multiset mismatches ignoring deal: {nodeal_unmatched}",
        f"rows posted to a different deal than the truth (mispostings, L3 / Part 3): {deal_unmatched_rows}",
        f"dedupe on the raw date text instead would keep {int(n_raw_text_distinct):,} rows "
        f"({int(n_raw_text_distinct) - len(cf):+,} vs truth): the July 2024 format-switch trap",
    ]
    return SuiteOutput(metrics=metrics, notes=notes)
