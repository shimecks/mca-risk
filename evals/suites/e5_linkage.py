"""E5 Linkage (plan.md §4). Gates: renewal links P >= 0.98, R >= 0.97; L1 accuracy >= 0.99; L3 recall >= 0.95.

* **Renewal links** (Q2): ``deal_links`` (N, P) vs the true ``(deal_id, prior_deal_id)`` pairs of
  ``ground_truth/deals.csv``. precision = correct / predicted, recall = correct / true. Reported by method
  (explicit / regex / inferred): each method's precision and its share of the true links.
* **L1**: every ``payoff_classes`` row vs the truth kind of the same ACH PAYOFF (``cashflows.csv``
  ``internal_payoff`` / ``early_payoff_cash`` on the same deal and date; PAYOFF rows are never misposted).
  accuracy = correct / PAYOFF rows. Also the successor-deal accuracy of internal payoffs, and the accuracy
  of the description hint alone (for contrast).
* **L3**: the true misposted rows are ``anomalies.csv`` ``payment_posted_to_prior_deal`` (renewal N, row
  date, posted deal P). recall = those rows whose ``payments_resolved.deal_id`` is N / true rows;
  precision = correct repairs / repairs. Also: rows of ``payments_resolved`` whose deal differs from the
  true ledger (multiset on date, type, amount, deal), before and after repair.
* **Q3** scorecard match rate (report-only): scorecards linked to their true deal / scorecards with an
  identifiable true deal (:mod:`evals.truth`). **Q4** collections link accuracy likewise.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date

import duckdb
import pandas as pd

from evals.common import EvalContext, SuiteOutput
from evals.truth import collections_truth_deal, scorecard_truth_deal

SUITE_ID = "E5"
_MP = re.compile(r"ACH row dated (\d{4}-\d{2}-\d{2}) shows deal (\d+)")
KIND_TO_TYPE = {
    "funding_out": "FUNDING",
    "debit": "DEBIT",
    "return": "RETURN",
    "internal_payoff": "PAYOFF",
    "early_payoff_cash": "PAYOFF",
    "settlement": "SETTLEMENT",
}


def _date(v) -> date:
    return v if type(v) is date else pd.Timestamp(v).date()


def run(ctx: EvalContext) -> SuiteOutput:
    gt = ctx.ground_truth_dir
    truth = pd.read_csv(gt / "deals.csv")
    cf = pd.read_csv(gt / "cashflows.csv")
    an = pd.read_csv(gt / "anomalies.csv")
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        links = con.execute("SELECT deal_id, prior_deal_id, method FROM deal_links").df()
        pc = con.execute(
            "SELECT payment_id, deal_id, txn_date, payoff_class, successor_deal_id, method, "
            "description_hint FROM payoff_classes"
        ).df()
        pr = con.execute(
            "SELECT payment_id, txn_date, deal_id, deal_id_posted, txn_type, amount_cents, repaired "
            "FROM payments_resolved"
        ).df()
        scl = con.execute(
            "SELECT l.scorecard_id, l.deal_id AS linked, s.app_date, s.fico, s.avg_monthly_rev_cents, s.positions, "
            "s.state, s.tib_years FROM scorecard_links l JOIN scorecards_std s USING (scorecard_id)"
        ).df()
        cl = con.execute(
            "SELECT l.collection_id, l.deal_id AS linked, l.method, c.log_date, c.merchant_name_raw, c.deal_id_raw, "
            "c.event_type, c.amount_cents FROM collections_links l JOIN collections_std c USING (collection_id)"
        ).df()
        policy = con.execute("SELECT detail FROM data_quality WHERE rule_id = 'L1'").fetchone()[0]
    finally:
        con.close()

    # ---- renewal links
    true_links = {
        (int(r.deal_id), int(r.prior_deal_id)) for r in truth.itertuples() if not pd.isna(r.prior_deal_id)
    }
    pred = {(int(r.deal_id), int(r.prior_deal_id)): r.method for r in links.itertuples()}
    tp = sum(1 for k in pred if k in true_links)
    prec = tp / len(pred) if pred else 0.0
    rec = tp / len(true_links) if true_links else 1.0
    by_method = {}
    for m in sorted(set(pred.values())):
        ks = [k for k, v in pred.items() if v == m]
        ok = sum(k in true_links for k in ks)
        by_method[m] = {
            "links": len(ks),
            "correct": ok,
            "precision": round(ok / len(ks), 6),
            "share_of_true_links": round(ok / len(true_links), 6),
        }
    missed = sorted(true_links - set(pred))
    wrong = sorted(k for k in pred if k not in true_links)

    # ---- L1
    tk = {
        (int(r.deal_id), r.date): r.kind
        for r in cf[cf.kind.isin(["internal_payoff", "early_payoff_cash"])].itertuples()
    }
    nxt = {int(r.prior_deal_id): int(r.deal_id) for r in truth.itertuples() if not pd.isna(r.prior_deal_id)}
    ok = succ_ok = succ_n = hint_ok = 0
    l1_err = []
    by_l1m: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in pc.itertuples():
        t = tk.get((int(r.deal_id), _date(r.txn_date).isoformat()))
        good = t == r.payoff_class
        ok += good
        by_l1m[r.method][0] += good
        by_l1m[r.method][1] += 1
        hint_guess = "internal_payoff" if r.description_hint == "internal_payoff" else "early_payoff_cash"
        hint_ok += hint_guess == t
        if not good:
            l1_err.append(
                {
                    "payment_id": int(r.payment_id),
                    "deal_id": int(r.deal_id),
                    "pred": r.payoff_class,
                    "truth": t,
                    "method": r.method,
                }
            )
        if r.payoff_class == "internal_payoff" and t == "internal_payoff":
            succ_n += 1
            succ_ok += (not pd.isna(r.successor_deal_id)) and nxt.get(int(r.deal_id)) == int(
                r.successor_deal_id
            )
    l1_acc = ok / len(pc) if len(pc) else 0.0

    # ---- L3
    mp = an[an.type == "payment_posted_to_prior_deal"]
    deb = pr[pr.txn_type == "DEBIT"].copy()
    deb["d"] = deb.txn_date.map(lambda v: _date(v).isoformat())
    by_posted = defaultdict(list)
    for r in deb.itertuples():
        by_posted[(int(r.deal_id_posted), r.d)].append(r)
    hits = 0
    true_rows: set[int] = set()
    for r in mp.itertuples():
        m = _MP.search(r.detail)
        n, d, p = int(r.deal_id), m.group(1), int(m.group(2))
        rows = by_posted.get((p, d), [])
        cand = [x for x in rows if int(x.deal_id) == n]
        if cand:
            hits += 1
            true_rows.add(int(cand[0].payment_id))
    l3_rec = hits / len(mp) if len(mp) else 1.0
    reps = deb[deb.repaired]
    l3_prec = (sum(int(x) in true_rows for x in reps.payment_id) / len(reps)) if len(reps) else 1.0

    t_ms = Counter()
    for r in cf[cf.kind != "commission_out"].itertuples():
        t_ms[(r.date, KIND_TO_TYPE[r.kind], round(r.amount * 100), int(r.deal_id))] += 1

    def unmatched(col: str) -> int:
        ms = Counter(
            (_date(a).isoformat(), b, int(c), int(d))
            for a, b, c, d in zip(pr.txn_date, pr.txn_type, pr.amount_cents, pr[col], strict=True)
        )
        return sum((ms - t_ms).values())

    # ---- Q3 / Q4
    sct = scorecard_truth_deal(scl, truth)
    sc_ok = sum(
        1
        for r in scl.itertuples()
        if int(r.scorecard_id) in sct and not pd.isna(r.linked) and int(r.linked) == sct[int(r.scorecard_id)]
    )
    sc_rate = sc_ok / len(sct) if sct else None
    ct = collections_truth_deal(cl, truth, gt)
    col_acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in cl.itertuples():
        if int(r.collection_id) not in ct:
            continue
        g = "with_deal_number" if r.method == "deal_number" else "name_only"
        col_acc[g][1] += 1
        col_acc[g][0] += (not pd.isna(r.linked)) and int(r.linked) == ct[int(r.collection_id)]

    metrics = {
        "renewal_link_precision": round(prec, 6),
        "renewal_link_recall": round(rec, 6),
        "l1_payoff_accuracy": round(l1_acc, 6),
        "l3_misposting_recall": round(l3_rec, 6),
        "l3_misposting_precision": round(l3_prec, 6),
        "scorecard_match_rate": None if sc_rate is None else round(sc_rate, 6),
        "renewal_links_by_method": by_method,
        "renewal_links_true": len(true_links),
        "renewal_links_predicted": len(pred),
        "l1_accuracy_by_method": {
            m: {"n": n, "correct": c, "accuracy": round(c / n, 6)} for m, (c, n) in sorted(by_l1m.items())
        },
        "l1_successor_accuracy": round(succ_ok / succ_n, 6) if succ_n else None,
        "l1_description_hint_only_accuracy": round(hint_ok / len(pc), 6) if len(pc) else None,
        "l1_policy": policy.rsplit("policy ", 1)[-1].strip("'"),
        "l3_true_misposted_rows": len(mp),
        "l3_repairs": len(reps),
        "rows_on_wrong_deal_as_posted": unmatched("deal_id_posted"),
        "rows_on_wrong_deal_after_repair": unmatched("deal_id"),
        "scorecards_with_truth_deal": len(sct),
        "collections_link_accuracy": {g: round(c / n, 6) for g, (c, n) in sorted(col_acc.items())},
    }
    notes = [
        f"links: {tp} correct of {len(pred)} predicted, {len(true_links)} true; missed {len(missed)}, wrong {len(wrong)}",
        f"L1: {ok}/{len(pc)} payoffs correct; L3: {hits}/{len(mp)} misposted rows repaired, {len(reps)} repairs",
    ]
    details = {"missed_links": missed[:100], "wrong_links": wrong[:100], "l1_errors": l1_err[:100]}
    return SuiteOutput(metrics, details=details, notes=notes)
