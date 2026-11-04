"""E6 Definitions (plan.md §4): §2.1 outcome accuracy, D17 precision / recall, D16 default date.

Gates (``evals/thresholds.json``): outcome accuracy >= 0.97; D17 precision >= 0.95, recall >= 0.85.

* **Outcome accuracy**: ``deal_metrics.outcome`` vs ``ground_truth/deals.csv`` ``outcome`` over every
  deal. The truth labels include ``active_stressed`` (a latent servicing state of the simulator), which
  is not a METRICS.md §2.1 outcome: under §2.1 such a deal is ``active``. The gated accuracy maps
  ``active_stressed`` -> ``active``; the strict, unmapped accuracy is reported alongside.
* **D17**: one record per prior deal P that was renewed (truth: a deal N has ``prior_deal_id`` = P;
  pipeline: P closed by an internal payoff). Truth positive = N's ``renewal_type`` is
  ``distressed_refi``; predicted positive = ``deal_metrics.d17_distressed`` on P.
* **Default date** (report-only, user decision 2026-11-03): over deals where both the pipeline (D16
  fired) and the truth have a default date, the share within +/-25 Fed business days, and the median /
  p90 absolute error in business days.
* Report-only, for the open D17 question: the same three numbers recomputed with the other D17 return
  basis (``MCARISK_D17_RETURNS``), using the pipeline's own metric code on the built tables.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput
from mcarisk.metrics.deal import OUTCOMES
from mcarisk.metrics.schedule import cal_index

SUITE_ID = "E6"
OUTCOME_MAP = {"active_stressed": "active"}


def _date(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    if isinstance(v, str):
        return date.fromisoformat(v[:10])
    return pd.Timestamp(v).date()


def score(dm: pd.DataFrame, truth: pd.DataFrame) -> dict:
    t = truth.set_index("deal_id")
    m = dm.set_index("deal_id")
    ids = sorted(set(m.index) & set(t.index))
    pred = m.loc[ids, "outcome"]
    true_raw = t.loc[ids, "outcome"]
    true = true_raw.replace(OUTCOME_MAP)
    acc = float((pred == true).mean())
    acc_strict = float((pred == true_raw).mean())
    # D17 over renewed priors
    true_pos: dict[int, bool] = {}
    for r in t.itertuples():
        if not pd.isna(r.prior_deal_id):
            true_pos[int(r.prior_deal_id)] = r.renewal_type == "distressed_refi"
    pred_pos = {int(i): bool(v) for i, v in m["d17_distressed"].items() if v is not None and not pd.isna(v)}
    keys = set(true_pos) | set(pred_pos)
    tp = sum(true_pos.get(k, False) and pred_pos.get(k, False) for k in keys)
    pp = sum(pred_pos.get(k, False) for k in keys)
    tt = sum(true_pos.get(k, False) for k in keys)
    return {
        "acc": acc,
        "acc_strict": acc_strict,
        "pred": pred,
        "true": true,
        "d17_p": tp / pp if pp else 0.0,
        "d17_r": tp / tt if tt else 0.0,
        "d17_counts": {"tp": tp, "predicted": pp, "true": tt, "renewed_priors": len(keys)},
        "ids": ids,
    }


def run(ctx: EvalContext) -> SuiteOutput:
    truth = pd.read_csv(ctx.ground_truth_dir / "deals.csv")
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        dm = con.execute("SELECT * FROM deal_metrics").df()
        try:
            from mcarisk.metrics.stage import compute

            alt = {}
            for b in ("asof", "attempt"):
                rows, _, _ = compute(con, d17_basis=b)
                alt[b] = pd.DataFrame(rows)
        except Exception as exc:  # report-only extra; never blocks the gated metrics
            alt = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()
    s = score(dm, truth)
    # which basis is the build? (the persisted table equals one of the two recomputations)
    build_basis = None
    if "error" not in alt:
        for b, df in alt.items():
            a = df.set_index("deal_id")["d17_distressed"].astype("object")
            c = dm.set_index("deal_id")["d17_distressed"].astype("object")
            if all((pd.isna(x) and pd.isna(y)) or x == y for x, y in zip(a.loc[c.index], c, strict=True)):
                build_basis = b
                break

    labels = list(OUTCOMES)
    cm = pd.crosstab(s["true"], s["pred"]).reindex(index=labels, columns=labels, fill_value=0)
    per_class = {
        o: {
            "true": int((s["true"] == o).sum()),
            "pred": int((s["pred"] == o).sum()),
            "correct": int(((s["true"] == o) & (s["pred"] == o)).sum()),
        }
        for o in labels
    }
    errors = Counter(f"{a}->{b}" for a, b in zip(s["true"], s["pred"], strict=True) if a != b)

    # default date within +/-25 business days
    ci = cal_index()
    t = truth.set_index("deal_id")
    diffs = []
    for r in dm.itertuples():
        if not r.d16_default or r.deal_id not in t.index:
            continue
        td = _date(t.at[r.deal_id, "default_date"])
        pd_ = _date(r.d16_default_date)
        if td is None or pd_ is None:
            continue
        diffs.append(abs(ci.bdays_between(td, pd_)))
    dd = pd.Series(diffs, dtype=float)
    within = float((dd <= 25).mean()) if len(dd) else None

    alt_scores = {}
    if "error" not in alt:
        for b, df in alt.items():
            x = score(df, truth)
            alt_scores[b] = {
                "outcome_accuracy": round(x["acc"], 6),
                "outcome_accuracy_strict": round(x["acc_strict"], 6),
                "d17_precision": round(x["d17_p"], 6),
                "d17_recall": round(x["d17_r"], 6),
            }
    else:
        alt_scores = alt

    metrics = {
        "outcome_accuracy": round(s["acc"], 6),
        "d17_precision": round(s["d17_p"], 6),
        "d17_recall": round(s["d17_r"], 6),
        "default_date_within_25bd_rate": None if within is None else round(within, 6),
        "outcome_accuracy_strict": round(s["acc_strict"], 6),
        "default_date_abs_err_bdays_median": None if dd.empty else float(dd.median()),
        "default_date_abs_err_bdays_p90": None if dd.empty else float(dd.quantile(0.9)),
        "default_date_n": len(dd),
        "d17_counts": s["d17_counts"],
        "d17_basis_of_build": build_basis,
        "by_d17_basis": alt_scores,
        "outcome_errors": dict(errors.most_common()),
    }
    notes = [
        f"outcome accuracy {s['acc']:.4f} over {len(s['ids'])} deals (truth active_stressed -> active; "
        f"strict {s['acc_strict']:.4f})",
        f"D17 on the build (basis {build_basis}): P {s['d17_p']:.4f} R {s['d17_r']:.4f} "
        f"({s['d17_counts']['tp']} TP of {s['d17_counts']['predicted']} predicted, {s['d17_counts']['true']} true)",
    ]
    for b, v in alt_scores.items():
        if isinstance(v, dict):
            notes.append(
                f"D17 basis {b}: outcome {v['outcome_accuracy']:.4f}, D17 P {v['d17_precision']:.4f} "
                f"R {v['d17_recall']:.4f}"
            )
    notes.append(
        "default date: "
        + (
            f"{within:.4f} within +/-25 bd (n={len(dd)}, median {dd.median():.0f}, p90 {dd.quantile(0.9):.0f})"
            if len(dd)
            else "n/a"
        )
    )
    notes.append("largest outcome confusions: " + ", ".join(f"{k} {v}" for k, v in errors.most_common(6)))
    return SuiteOutput(
        metrics=metrics,
        details={
            "confusion_matrix": {"rows_true": labels, "cols_pred": labels, "counts": cm.values.tolist()},
            "per_class": per_class,
        },
        notes=notes,
    )
