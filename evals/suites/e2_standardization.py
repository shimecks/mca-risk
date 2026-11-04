"""E2 Standardization (plan.md §4): parsed advance / RTR / payment / dates / factor / term equal the truth
for each deal.  Gate: field_accuracy >= 0.995.

Population: every ``deals_std`` row (one per tracker row), joined to ``ground_truth/deals.csv`` on the
deal number. Deals that have no tracker row at all (Q5, reconstructed from ACH in Part 3) are not
standardization output and are reported as ``coverage`` only. A ``deals_std`` deal number that is not
in the truth counts as wrong on every field.

Gated fields and how they are compared (truth units -> pipeline units):
  advance        round(advance * 100)          == advance_cents
  rtr            round(rtr * 100)              == rtr_cents
  payment        round(payment * 100)          == payment_cents   (installment as stated: weekly amount
                                                                   for weekly deals)
  funding_date   funding_date (ISO)            == funding_date
  factor         factor                        == factor          (|diff| < 1e-9)
  term           term_bdays                    == term_bdays      (business days)
A NULL pipeline value is a mismatch. field_accuracy = correct field values / compared field values,
pooled over the 6 fields.

Report-only: per-field and per-source-file accuracy, the same check for fee / commission / frequency /
explicit prior deal / inline underwriting features (2025+ trackers), and the list of mismatches.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput

SUITE_ID = "E2"
MAX_MISMATCHES = 200


def _cents(x: Any) -> int | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return round(float(x) * 100)


def _int(x: Any) -> int | None:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x is pd.NA:
        return None
    return int(x)


def _same_float(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return False
    return abs(float(a) - float(b)) < tol


# field -> (truth -> comparable, pipeline column, pipeline -> comparable, comparator)
GATED: dict[str, tuple[Any, str, Any]] = {
    "advance": (lambda t: _cents(t["advance"]), "advance_cents", _int),
    "rtr": (lambda t: _cents(t["rtr"]), "rtr_cents", _int),
    "payment": (lambda t: _cents(t["payment"]), "payment_cents", _int),
    "funding_date": (lambda t: date.fromisoformat(str(t["funding_date"])), "funding_date", lambda v: v),
    "factor": (lambda t: float(t["factor"]), "factor", lambda v: None if pd.isna(v) else float(v)),
    "term": (lambda t: int(t["term_bdays"]), "term_bdays", _int),
}
EXTRA: dict[str, tuple[Any, str, Any]] = {
    "fee": (lambda t: _cents(t["fee"]), "fee_cents", _int),
    "commission_rate": (
        lambda t: round(float(t["commission_pct"]), 6),
        "commission_rate",
        lambda v: None if pd.isna(v) else round(float(v), 6),
    ),
    "frequency": (lambda t: t["frequency"], "frequency", lambda v: v),
}
# Only compared where the source has the field and it is populated (inline UW, explicit links).
WHERE_GIVEN: dict[str, tuple[Any, str, Any]] = {
    "prior_deal_id": (lambda t: _int(t["prior_deal_id"]), "prior_deal_id", _int),
    "payoff_amt": (lambda t: _cents(t["internal_payoff_of_prior"]), "payoff_amt_cents", _int),
    "fico": (lambda t: int(t["fico"]), "fico", _int),
    "avg_monthly_rev": (lambda t: _cents(round(float(t["monthly_rev"]), -2)), "avg_monthly_rev_cents", _int),
    "tib_months": (lambda t: int(t["tib_months"]), "tib_months", _int),
    "positions": (lambda t: int(t["positions_at_funding"]), "positions", _int),
    "nsf_90d": (lambda t: int(t["nsf_90d"]), "nsf_90d", _int),
    "industry": (lambda t: t["industry"], "industry", lambda v: v),
    "state": (lambda t: t["state"], "state", lambda v: v),
}


def _eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, float) or isinstance(b, float):
        return _same_float(a, b)
    return a == b


def run(ctx: EvalContext) -> SuiteOutput:
    truth = pd.read_csv(ctx.ground_truth_dir / "deals.csv")
    tmap = {int(r["deal_id"]): r for r in truth.to_dict("records")}
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        std = con.execute("SELECT * FROM deals_std ORDER BY deal_id").df()
    finally:
        con.close()
    std["funding_date"] = [None if pd.isna(v) else pd.Timestamp(v).date() for v in std["funding_date"]]

    correct: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    by_file_ok: dict[str, int] = defaultdict(int)
    by_file_n: dict[str, int] = defaultdict(int)
    extra_ok: dict[str, int] = defaultdict(int)
    extra_n: dict[str, int] = defaultdict(int)
    mismatches: list[dict[str, Any]] = []
    unknown_deals = 0

    for r in std.to_dict("records"):
        t = tmap.get(int(r["deal_id"]))
        if t is None:
            unknown_deals += 1
        for fname, (tf, col, pf) in GATED.items():
            want = tf(t) if t is not None else None
            got = pf(r[col])
            ok = t is not None and _eq(want, got)
            total[fname] += 1
            correct[fname] += ok
            by_file_n[r["src_file"]] += 1
            by_file_ok[r["src_file"]] += ok
            if not ok and len(mismatches) < MAX_MISMATCHES:
                mismatches.append(
                    {
                        "deal_id": int(r["deal_id"]),
                        "field": fname,
                        "truth": None if want is None else str(want),
                        "parsed": None if got is None else str(got),
                        "src_file": r["src_file"],
                        "src_row": int(r["src_row"]),
                    }
                )
        if t is None:
            continue
        for fname, (tf, col, pf) in EXTRA.items():
            extra_n[fname] += 1
            extra_ok[fname] += _eq(tf(t), pf(r[col]))
        for fname, (tf, col, pf) in WHERE_GIVEN.items():
            got = pf(r[col])
            if got is None:
                continue
            raw_t = tf(t)
            extra_n[fname] += 1
            extra_ok[fname] += _eq(raw_t, got)

    n_correct, n_total = sum(correct.values()), sum(total.values())
    per_field = {f: round(correct[f] / total[f], 6) if total[f] else None for f in GATED}
    per_file = {f: round(by_file_ok[f] / by_file_n[f], 6) for f in sorted(by_file_n)}
    extras = {f: {"accuracy": round(extra_ok[f] / extra_n[f], 6), "n": extra_n[f]} for f in extra_n}
    coverage = round(len(std) / len(truth), 6)
    metrics = {
        "field_accuracy": round(n_correct / n_total, 6) if n_total else None,
        "field_accuracy_by_field": per_field,
        "field_accuracy_by_file": per_file,
        "extra_field_accuracy": extras,
        "deals_std_rows": len(std),
        "truth_deals": len(truth),
        "coverage": coverage,
        "unknown_deal_ids": unknown_deals,
        "mismatch_count": n_total - n_correct,
    }
    notes = [
        f"{n_correct:,}/{n_total:,} gated field values correct over {len(std):,} tracker deals "
        f"({len(truth) - len(std)} truth deals have no tracker row: Q5, Part 3)",
        "per field: " + ", ".join(f"{k}={v}" for k, v in per_field.items()),
        "per file: " + ", ".join(f"{k.removeprefix('raw/')}={v}" for k, v in per_file.items()),
        "extra (report-only): " + ", ".join(f"{k}={v['accuracy']} (n={v['n']})" for k, v in extras.items()),
    ]
    for m in mismatches[:20]:
        notes.append(
            f"mismatch deal {m['deal_id']} {m['field']}: truth={m['truth']} parsed={m['parsed']} "
            f"({m['src_file']} row {m['src_row']})"
        )
    return SuiteOutput(metrics=metrics, details={"mismatches": mismatches}, notes=notes)
