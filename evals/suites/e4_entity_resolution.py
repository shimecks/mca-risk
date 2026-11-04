"""E4 Entity resolution (plan.md §4): pairwise precision / recall / F1 of merchant clusters vs the true
``merchant_id``; deal -> merchant accuracy. Gate: pairwise F1 >= 0.98.

Population: every row of ``deals_resolved`` (tracker deals + Q5 reconstructions) joined to
``ground_truth/deals.csv`` on the deal number. A pair of deals is *predicted* together when they share a
``merchant_key`` and *truly* together when they share a ``merchant_id``.
  precision = |pred & true pairs| / |pred pairs|, recall = |pred & true| / |true pairs|.
Deal -> merchant accuracy: each predicted cluster is labelled with its majority true merchant; a deal is
correct when its cluster's label is its own true merchant (and no other cluster claims more of that
merchant's deals).

Report-only: name-only records. A scorecard / collections row is correct when the merchant the pipeline
assigned is the true merchant of the deal that row belongs to (scorecards: truth deal identified by its
underwriting features; collections: ``Deal #`` when present, otherwise the truth deal whose event date
matches). Also the merge counts by method.
"""

from __future__ import annotations

from collections import Counter
from math import comb

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput
from evals.truth import collections_truth_deal, scorecard_truth_deal

SUITE_ID = "E4"


def pairwise(pred: dict[int, str], true: dict[int, str]) -> tuple[float, float, float, int, int, int]:
    ids = [i for i in pred if i in true]
    pc = Counter(pred[i] for i in ids)
    tc = Counter(true[i] for i in ids)
    both = Counter((pred[i], true[i]) for i in ids)
    tp = sum(comb(n, 2) for n in both.values())
    pp = sum(comb(n, 2) for n in pc.values())
    tt = sum(comb(n, 2) for n in tc.values())
    p = tp / pp if pp else 1.0
    r = tp / tt if tt else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1, tp, pp, tt


def run(ctx: EvalContext) -> SuiteOutput:
    truth = pd.read_csv(ctx.ground_truth_dir / "deals.csv")
    true = dict(zip(truth.deal_id.astype(int), truth.merchant_id, strict=True))
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        dr = con.execute("SELECT deal_id, merchant_key FROM deals_resolved").df()
        merges = dict(con.execute("SELECT method, COUNT(*) FROM merchant_merges GROUP BY 1").fetchall())
        scl = con.execute(
            "SELECT l.scorecard_id, l.merchant_key, s.app_date, s.fico, s.avg_monthly_rev_cents, s.positions, "
            "s.state, s.tib_years FROM scorecard_links l JOIN scorecards_std s USING (scorecard_id)"
        ).df()
        cl = con.execute(
            "SELECT l.collection_id, l.merchant_key, c.log_date, c.merchant_name_raw, c.deal_id_raw, c.event_type, "
            "c.amount_cents FROM collections_links l JOIN collections_std c USING (collection_id)"
        ).df()
    finally:
        con.close()
    pred = dict(zip(dr.deal_id.astype(int), dr.merchant_key, strict=True))
    p, r, f1, tp, pp, tt = pairwise(pred, true)

    # deal -> merchant accuracy via majority labelling
    lab = Counter((pred[d], true[d]) for d in pred if d in true)
    label: dict[str, str] = {}
    for (k, m), _n in sorted(lab.items(), key=lambda x: -x[1]):
        label.setdefault(k, m)
    correct = sum(1 for d in pred if d in true and label.get(pred[d]) == true[d])
    acc = correct / len(true)
    unknown = sorted(d for d in pred if d not in true)
    missing = sorted(d for d in true if d not in pred)

    # name-only records
    sc_truth = scorecard_truth_deal(scl, truth)
    sc_ok = sc_n = 0
    for sid, k in zip(scl.scorecard_id, scl.merchant_key, strict=True):
        td = sc_truth.get(int(sid))
        if td is None:
            continue
        sc_n += 1
        sc_ok += isinstance(k, str) and label.get(k) == true[td]
    co_truth = collections_truth_deal(cl, truth, ctx.ground_truth_dir)
    co = {"with_deal_number": [0, 0], "name_only": [0, 0]}
    for cid, k, did in zip(cl.collection_id, cl.merchant_key, cl.deal_id_raw, strict=True):
        td = co_truth.get(int(cid))
        if td is None:
            continue
        grp = "name_only" if pd.isna(did) else "with_deal_number"
        co[grp][1] += 1
        co[grp][0] += isinstance(k, str) and label.get(k) == true[td]

    errors = []
    for k in sorted(set(pred.values())):
        ms = sorted({true[d] for d in pred if pred[d] == k and d in true})
        if len(ms) > 1:
            errors.append({"merchant_key": k, "true_merchants": ms})
    split = []
    inv: dict[str, set[str]] = {}
    for d, m in true.items():
        if d in pred:
            inv.setdefault(m, set()).add(pred[d])
    for m, ks in sorted(inv.items()):
        if len(ks) > 1:
            split.append({"merchant_id": m, "merchant_keys": sorted(ks)})

    metrics = {
        "pairwise_f1": round(f1, 6),
        "pairwise_precision": round(p, 6),
        "pairwise_recall": round(r, 6),
        "deal_to_merchant_accuracy": round(acc, 6),
        "n_deals": len(pred),
        "n_clusters": len(set(pred.values())),
        "n_true_merchants": len(set(true.values())),
        "merges_by_method": merges,
        "scorecard_merchant_accuracy": round(sc_ok / sc_n, 6) if sc_n else None,
        "collections_merchant_accuracy": {g: (round(a / n, 6) if n else None) for g, (a, n) in co.items()},
    }
    notes = [
        f"pairs: true {tt:,}, predicted {pp:,}, both {tp:,}",
        f"{len(errors)} clusters mix merchants; {len(split)} merchants split across clusters",
        f"scorecards with an identifiable truth deal: {sc_n}/{len(scl)}; collections rows: "
        f"{co['with_deal_number'][1] + co['name_only'][1]}/{len(cl)}",
    ]
    if unknown or missing:
        notes.append(f"deals not in truth: {unknown[:10]}; truth deals not resolved: {missing[:10]}")
    return SuiteOutput(
        metrics, details={"mixed_clusters": errors[:50], "split_merchants": split[:50]}, notes=notes
    )
