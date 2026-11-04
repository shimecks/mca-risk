"""E7 Metrics (plan.md §4): deal / relationship metrics vs truth on correctly linked records ($1 per
record) and P-metrics vs ``ground_truth/portfolio_expected.json`` (+/-0.5 pp). Also end to end.

**As-of adjustment of the truth.** The truth tables count 41 ACH returns dated after the as-of date
(2026-10-01/02) in ``cash_from_merchant`` and treat their debits as failed in ``balance_asof``.
METRICS.md §0 says nothing after the as-of date is used, so the eval removes those events from the
truth record values before comparing (D7, D9, D10, D11, R2, R4 and the R6 / R7 / R8 that depend on
them). The size of the adjustment is reported (``truth_post_asof_events``).

**Correctly linked records** (the gated population):
* a deal is correctly linked when its pipeline merchant cluster is its true merchant's cluster, its
  prior link equals the truth's, its ledger cash events equal the true cash flows for the deal (same
  (date, kind, cents) multiset, so L1 / L2 / L3 are right for it), and every collections row the
  pipeline attached to it truly belongs to it;
* metrics that need tracker contract fields are compared only where those fields were observed
  (``rtr_source = tracker``, ``commission_known``); tracker-missing deals (Q5) are scored end to end;
* D12 is compared where the truth defines it (open ``active*`` deals) and the outcome agrees (E6
  scores outcome disagreements);
* a relationship is correctly linked when its deal set equals its true merchant's deal set, every deal
  is correctly linked and has observed contract fields, and R5 agrees with the truth (R6 / R7 / R8
  switch on R5).

**Comparison basis for the D17-dependent P-metrics** (user decisions 2026-11-04): the P4 economic bad
rate, the P8 distressed share of renewals and the P8 distressed-chain loss share are compared with METRICS.md D17 (v1.2) applied to the
TRUE deals and cash flows (``spec_d17_on_truth``), not with the simulator's latent distressed labels,
which no observable definition can reproduce exactly. The gap to the simulator labels is reported,
split into definition error (spec-on-truth - simulator) and pipeline error (pipeline - spec-on-truth).

Gated: ``deal_metric_max_abs_err_usd`` (D1-D9, D12) and ``relationship_metric_max_abs_err_usd`` (R1,
R2, R4, R6, R7, R8) over linked records; ``portfolio_metric_max_abs_err_pp`` over every rate in
``portfolio_expected.json`` that has a P-metric counterpart. Report-only: rates per record, dollar
P-metrics (relative error), and ``end_to_end`` (every record, no linkage or observability filter).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from evals.common import EvalContext, SuiteOutput
from evals.truth import collections_truth_deal

SUITE_ID = "E7"
AS_OF = "2026-09-30"
CASH_IN = ("debit", "return", "early_payoff_cash", "settlement")

DEAL_USD = {  # pipeline column -> truth column (dollars)
    "d1_advance_cents": "advance",
    "d2_rtr_cents": "rtr",
    "d3_fee_cents": "fee",
    "d4_net_cash_to_merchant_cents": "net_cash_to_merchant",
    "d5_commission_cents": "commission_amt",
    "d6_funder_cash_out_cents": "funder_cash_out",
    "d7_cash_from_merchant_cents": "cash_from_merchant",
    "d8_internal_payoff_out_cents": "internal_payoff_out",
    "d9_reported_collected_cents": "reported_collected",
    "d12_expected_collected_cents": "expected_collected_asof",
}
NEEDS_TRACKER = {
    "d1_advance_cents",
    "d2_rtr_cents",
    "d3_fee_cents",
    "d5_commission_cents",
    "d6_funder_cash_out_cents",
    "d10_reported_collection_rate",
    "d11_adjusted_collection_rate",
}
DEAL_RATE = {
    "d10_reported_collection_rate": "reported_collection_rate",
    "d11_adjusted_collection_rate": "renewal_adjusted_collection_rate",
    "d13_pace": "pace_asof",
}
REL_USD = {
    "r1_funder_cash_out_cents": "funder_cash_out",
    "r2_cash_from_merchant_cents": "cash_from_merchant",
    "r4_net_cash_position_cents": "net_cash_position",
    "r6_economic_exposure_cents": "economic_exposure",
    "r7_contractual_balance_cents": "contractual_balance_asof",
    "r8_realized_loss_cents": "realized_loss",
}
# portfolio_expected.json key -> (metric_id, metric) ; rates compared in percentage points
P_RATES = {
    "closed_deals_reported_collection_rate_rtr_weighted": ("P1", "reported_collection_rate"),
    "closed_deals_renewal_adjusted_collection_rate_rtr_weighted": ("P1", "adjusted_collection_rate"),
    "share_of_reported_collections_that_are_internal_payoffs": ("P3", "internal_payoff_share"),
    "deal_level_bad_rate_naive_closed": ("P4", "bad_rate_naive"),
    "deal_level_bad_rate_economic_closed": ("P4", "bad_rate_economic"),
    "resolved_relationships_with_loss_share": ("P5", "resolved_bad_share"),
    "resolved_relationship_cash_multiple": ("P5", "resolved_cash_multiple"),
    "distressed_refi_chains_ending_in_loss_share": ("P8", "bad_rate_chains_with_distressed"),
    "renewed_deals_with_distressed_refi/renewed_deals_total": ("P8", "distressed_share_of_renewals"),
    "masking_gap (reported - adjusted)": ("P2", "renewal_masking_gap_pp"),
}
P_USD = {
    "open_economic_exposure_total": ("P6", "economic_exposure_cents"),
    "open_contractual_balance_total": ("P6", "contractual_balance_cents"),
}


# P-metrics whose expected value comes from applying METRICS.md (v1.2) D17 to the true ledger, not
# from the simulator's labels (user decision 2026-11-04; see thresholds.json E7.comparison_basis).
SPEC_ON_TRUTH = {  # every D17-dependent P-metric (user decisions 2026-11-04)
    "deal_level_bad_rate_economic_closed",
    "renewed_deals_with_distressed_refi/renewed_deals_total",
    "distressed_refi_chains_ending_in_loss_share",
}
CLOSED_TRUTH = {
    "paid_in_full",
    "paid_in_full_early",
    "renewed_healthy",
    "refinanced_distressed",
    "settled",
    "written_off",
}


def spec_d17_on_truth(truth: pd.DataFrame, cf: pd.DataFrame, as_of: date) -> dict[int, bool]:
    """D17 (METRICS.md v1.2, default ``attempt`` basis) applied to the TRUE deals and cash flows.

    Returns prior deal id -> distressed, for every true renewal. It uses the pipeline's own pure
    functions (``distressed_refinance``, ``match_returns``) on truth inputs, so it measures the
    definition, not the pipeline's linkage."""
    from mcarisk.ledger import KIND_ORDER, match_returns
    from mcarisk.metrics.deal import D17_DEFAULT_BASIS, Deal, Event, distressed_refinance
    from mcarisk.metrics.schedule import cal_index

    def cents(x) -> int:
        return round(float(x) * 100)

    deals = {}
    for r in truth.itertuples():
        deals[int(r.deal_id)] = Deal(
            int(r.deal_id),
            str(r.merchant_id),
            date.fromisoformat(str(r.funding_date)),
            cents(r.advance),
            cents(r.rtr),
            cents(r.fee),
            float(r.commission_pct),
            cents(r.payment),
            str(r.frequency),
        )
    rolled = {
        int(r.deal_id): int(r.rolled_to_deal_id)
        for r in truth.itertuples()
        if not pd.isna(r.rolled_to_deal_id)
    }
    rows = []
    for r in cf[cf.kind != "commission_out"].itertuples():
        rows.append(
            {
                "deal_id": int(r.deal_id),
                "event_date": date.fromisoformat(str(r.date)[:10]),
                "kind": r.kind,
                "amount_cents": cents(r.amount),
                "matched_event_id": None,
            }
        )
    for r in truth.itertuples():
        if not pd.isna(r.modified_date):
            rows.append(
                {
                    "deal_id": int(r.deal_id),
                    "event_date": date.fromisoformat(str(r.modified_date)),
                    "kind": "modification",
                    "amount_cents": 0,
                    "matched_event_id": None,
                    "new_payment_cents": cents(r.modified_payment),
                }
            )
    rows.sort(key=lambda x: (x["deal_id"], x["event_date"], KIND_ORDER[x["kind"]], x["amount_cents"]))
    for i, x in enumerate(rows, start=1):
        x["event_id"] = i
    match_returns(rows)
    by: dict[int, list] = {}
    for x in rows:
        succ = rolled.get(x["deal_id"]) if x["kind"] == "internal_payoff" else None
        by.setdefault(x["deal_id"], []).append(
            Event(
                x["event_id"],
                x["deal_id"],
                x["event_date"],
                x["kind"],
                x["amount_cents"],
                x["matched_event_id"],
                x.get("new_payment_cents"),
                succ,
            )
        )
    ci = cal_index()
    out = {}
    for pid, nid in rolled.items():
        evs = [e for e in by.get(pid, []) if e.event_date <= as_of]
        ipo = [e for e in evs if e.kind == "internal_payoff"]
        if not ipo:
            continue
        d = deals[pid]
        res = distressed_refinance(
            d, evs, d.rtr_cents, ipo[0].event_date, deals.get(nid), ci, basis=D17_DEFAULT_BASIS, as_of=as_of
        )
        out[pid] = res["distressed"]
    return out


def _usd_err(p, t) -> float | None:
    if p is None or t is None or pd.isna(p) or pd.isna(t):
        return None
    return abs(float(p) / 100.0 - float(t))


def _summ(errs: dict[str, list[float]]) -> dict[str, dict]:
    out = {}
    for k, v in errs.items():
        a = np.array(v, dtype=float)
        out[k] = {
            "n": len(a),
            "max_abs_err": None if not len(a) else round(float(a.max()), 6),
            "within_1usd_rate" if not k.startswith(("d10", "d11", "d13")) else "within_1e-4_rate": (
                None
                if not len(a)
                else round(float((a <= (1.0 if not k.startswith(("d10", "d11", "d13")) else 1e-4)).mean()), 6)
            ),
            "sum_abs_err": round(float(a.sum()), 2),
        }
    return out


def run(ctx: EvalContext) -> SuiteOutput:
    gt = ctx.ground_truth_dir
    truth = pd.read_csv(gt / "deals.csv")
    rel_t = pd.read_csv(gt / "relationships.csv")
    cf = pd.read_csv(gt / "cashflows.csv")
    expected = json.loads((gt / "portfolio_expected.json").read_text(encoding="utf-8"))
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        dm = con.execute("SELECT * FROM deal_metrics").df()
        rm = con.execute("SELECT * FROM relationship_metrics").df()
        pm = con.execute("SELECT * FROM portfolio_metrics WHERE k IS NULL").df()
        dr = con.execute("SELECT deal_id, merchant_key, prior_deal_id FROM deals_resolved").df()
        led = con.execute(
            "SELECT deal_id, event_date, kind, amount_cents FROM ledger WHERE source = 'ach'"
        ).df()
        cl = con.execute(
            "SELECT l.collection_id, l.deal_id AS linked, l.method, c.log_date, c.merchant_name_raw, c.deal_id_raw, "
            "c.event_type, c.amount_cents FROM collections_links l JOIN collections_std c USING (collection_id)"
        ).df()
    finally:
        con.close()

    t = truth.set_index("deal_id")
    # ---- as-of adjustment of the truth
    cf["date"] = cf["date"].astype(str)
    post = cf[(cf.date > AS_OF) & cf.kind.isin(CASH_IN)]
    post_by_deal = post.groupby("deal_id").amount.sum()
    t["cfm_asof"] = (t.cash_from_merchant - post_by_deal.reindex(t.index).fillna(0)).round(2)
    t["rep_asof"] = (t.cfm_asof + t.internal_payoff_out).round(2)
    t["rate_rep_asof"] = t.rep_asof / t.rtr
    t["rate_adj_asof"] = t.cfm_asof / t.rtr
    t["cash_from_merchant"] = t.cfm_asof
    t["reported_collected"] = t.rep_asof
    t["reported_collection_rate"] = t.rate_rep_asof
    t["renewal_adjusted_collection_rate"] = t.rate_adj_asof
    t["pace_asof"] = t.cfm_asof / t.expected_collected_asof

    # ---- linkage checks per deal
    cf_ach = cf[cf.kind != "commission_out"]
    t_ms: dict[int, Counter] = defaultdict(Counter)
    for r in cf_ach.itertuples():
        t_ms[int(r.deal_id)][(r.date, r.kind, round(r.amount * 100))] += 1
    p_ms: dict[int, Counter] = defaultdict(Counter)
    for r in led.itertuples():
        p_ms[int(r.deal_id)][
            (pd.Timestamp(r.event_date).date().isoformat(), r.kind, int(r.amount_cents))
        ] += 1
    true_m = t.merchant_id.to_dict()
    clusters = dr.groupby("merchant_key").deal_id.apply(lambda s: frozenset(int(x) for x in s)).to_dict()
    true_clusters = (
        truth.groupby("merchant_id").deal_id.apply(lambda s: frozenset(int(x) for x in s)).to_dict()
    )
    mk_of = dr.set_index("deal_id").merchant_key.to_dict()
    prior_p = dr.set_index("deal_id").prior_deal_id.to_dict()
    ct = collections_truth_deal(cl, truth, gt)
    bad_col_deals = {
        int(r.linked)
        for r in cl.itertuples()
        if not pd.isna(r.linked) and int(r.collection_id) in ct and ct[int(r.collection_id)] != int(r.linked)
    }
    reasons: Counter = Counter()
    linked: dict[int, bool] = {}
    for did in dm.deal_id.astype(int):
        why = None
        if did not in t.index:
            why = "unknown_deal"
        elif clusters.get(mk_of.get(did)) != true_clusters.get(true_m[did]):
            why = "merchant_cluster"
        elif not (
            (pd.isna(prior_p.get(did)) and pd.isna(t.at[did, "prior_deal_id"]))
            or (not pd.isna(prior_p.get(did)) and not pd.isna(t.at[did, "prior_deal_id"])
                and int(prior_p[did]) == int(t.at[did, "prior_deal_id"]))
        ):  # fmt: skip
            why = "renewal_link"
        elif p_ms.get(did, Counter()) != t_ms.get(did, Counter()):
            why = "ledger_events"
        elif did in bad_col_deals:
            why = "collections_link"
        linked[did] = why is None
        if why:
            reasons[why] += 1

    # ---- deal metrics
    dmi = dm.set_index("deal_id")
    lk_err: dict[str, list[float]] = defaultdict(list)
    e2e_err: dict[str, list[float]] = defaultdict(list)
    worst: dict[str, tuple] = {}
    missing: Counter = Counter()  # (metric, in linked scope) -> pipeline or truth value missing
    for did, r in dmi.iterrows():
        if did not in t.index:
            continue
        tr = t.loc[did]
        observed = r.rtr_source == "tracker" and bool(r.commission_known)
        t_out = str(tr.outcome).replace("active_stressed", "active")
        for pc, tc in {**DEAL_USD, **DEAL_RATE}.items():
            pv, tv = r[pc], tr[tc]
            if pc in ("d12_expected_collected_cents", "d13_pace") and (
                pd.isna(tv) or not str(tr.outcome).startswith("active")
            ):
                continue
            if pd.isna(pv) and pd.isna(tv):
                continue
            if pd.isna(pv) or pd.isna(tv):
                missing[(pc, linked[did] and (pc not in NEEDS_TRACKER or observed))] += 1
                continue
            elif pc in DEAL_RATE:
                err = abs(float(pv) - float(tv))
            else:
                err = _usd_err(pv, tv)
            e2e_err[pc].append(err)
            ok_scope = linked[did] and (pc not in NEEDS_TRACKER or observed)
            if pc in ("d12_expected_collected_cents", "d13_pace"):
                ok_scope = ok_scope and r.outcome == t_out and observed
            if ok_scope:
                lk_err[pc].append(err)
                if pc in DEAL_USD and err > worst.get(pc, (-1,))[0]:
                    worst[pc] = (err, int(did))

    # ---- relationship metrics
    rel_t = rel_t.set_index("merchant_id")
    post_m = post.groupby("merchant_id").amount.sum()
    rel_t["cash_from_merchant"] = (rel_t.cash_from_merchant - post_m.reindex(rel_t.index).fillna(0)).round(2)
    rel_t["net_cash_position"] = (rel_t.cash_from_merchant - rel_t.funder_cash_out).round(2)
    last_deal = truth.sort_values("deal_id").groupby("merchant_id").tail(1).set_index("merchant_id").deal_id
    post_last = post_by_deal.reindex(last_deal.values).fillna(0).values
    open_ = rel_t.status == "open"
    rel_t["economic_exposure"] = np.where(open_, np.maximum(0, -rel_t.net_cash_position), 0.0)
    rel_t["realized_loss"] = np.where(~open_, np.maximum(0, -rel_t.net_cash_position), 0.0)
    rel_t["contractual_balance_asof"] = np.where(
        open_,
        rel_t.contractual_balance_asof
        + pd.Series(post_last, index=last_deal.index).reindex(rel_t.index).fillna(0),
        0.0,
    )
    rmi = rm.set_index("merchant_key")
    rl_err: dict[str, list[float]] = defaultdict(list)
    re2e_err: dict[str, list[float]] = defaultdict(list)
    rel_reasons: Counter = Counter()
    rworst: dict[str, tuple] = {}
    obs = dmi.apply(lambda r: r.rtr_source == "tracker" and bool(r.commission_known), axis=1)
    for mk, r in rmi.iterrows():
        deals = clusters.get(mk, frozenset())
        mids = Counter(true_m.get(d) for d in deals)
        mid = mids.most_common(1)[0][0]
        if mid not in rel_t.index:
            continue
        tr = rel_t.loc[mid]
        why = None
        if deals != true_clusters.get(mid):
            why = "merchant_cluster"
        elif not all(linked.get(d, False) for d in deals):
            why = "deal_not_linked"
        elif not all(obs.get(d, False) for d in deals):
            why = "contract_fields_unobserved"
        elif r.r5_status != tr.status:
            why = "status_differs"
        if why:
            rel_reasons[why] += 1
        for pc, tc in REL_USD.items():
            pv = r[pc] if not pd.isna(r[pc]) else 0  # NULL (not applicable) == truth 0.0
            err = _usd_err(pv, tr[tc])
            re2e_err[pc].append(err)
            if why is None:
                rl_err[pc].append(err)
                if err > rworst.get(pc, (-1,))[0]:
                    rworst[pc] = (err, mk)

    # ---- portfolio
    pv = {(r.metric_id, r.metric): r.value for r in pm.itertuples() if r.segment == "all"}
    exp = dict(expected)
    exp["renewed_deals_with_distressed_refi/renewed_deals_total"] = (
        expected["renewed_deals_with_distressed_refi"] / expected["renewed_deals_total"]
    )
    exp["masking_gap (reported - adjusted)"] = (
        expected["closed_deals_reported_collection_rate_rtr_weighted"]
        - expected["closed_deals_renewal_adjusted_collection_rate_rtr_weighted"]
    )
    # spec-on-truth expected values for the D17-dependent rates
    sot = spec_d17_on_truth(truth, cf, date.fromisoformat(AS_OF))
    n_sot = sum(sot.values())
    cl_t = truth[truth.outcome.isin(CLOSED_TRUTH)]
    naive_t = int(cl_t.outcome.isin(["settled", "written_off"]).sum())
    spec_exp = {
        "deal_level_bad_rate_economic_closed": (naive_t + n_sot) / len(cl_t),
        "renewed_deals_with_distressed_refi/renewed_deals_total": n_sot / len(sot),
    }
    # the distressed-chain loss share (P8) with chains defined by spec D17 on the truth
    mid_of = truth.set_index("deal_id").merchant_id
    chain_m = {mid_of[pid] for pid, v in sot.items() if v}
    res_t = rel_t[rel_t.status == "resolved"]
    bad_t = res_t.cash_from_merchant / res_t.funder_cash_out < 1.0
    in_chain = res_t.index.isin(list(chain_m))
    chain_sot = {
        "spec_on_truth": round(float(bad_t[in_chain].mean()), 6),
        "chains": int(in_chain.sum()),
        "simulator_label": expected["distressed_refi_chains_ending_in_loss_share"],
        "pipeline": None
        if pv.get(("P8", "bad_rate_chains_with_distressed")) is None
        else round(pv[("P8", "bad_rate_chains_with_distressed")], 6),
    }
    spec_exp["distressed_refi_chains_ending_in_loss_share"] = chain_sot["spec_on_truth"]
    p_rows = {}
    for key, mid in P_RATES.items():
        ours = pv.get(mid)
        if mid[1] == "renewal_masking_gap_pp" and ours is not None:
            ours = ours / 100.0
        on_truth = key in SPEC_ON_TRUTH
        ref = spec_exp[key] if on_truth else exp[key]
        p_rows[key] = {
            "metric": f"{mid[0]} {mid[1]}",
            "comparison_basis": "spec_on_truth" if on_truth else "portfolio_expected.json",
            "expected": round(ref, 6),
            "pipeline": None if ours is None else round(ours, 6),
            "abs_err_pp": None if ours is None else round(abs(ours - ref) * 100, 4),
        }
        if on_truth:
            p_rows[key].update(
                simulator_label=round(exp[key], 6),
                definition_error_pp=round((ref - exp[key]) * 100, 4),  # spec-on-truth - simulator
                pipeline_error_pp=None if ours is None else round((ours - ref) * 100, 4),  # pipeline - spec
            )
    p_usd = {}
    for key, mid in P_USD.items():
        ours = pv.get(mid)
        p_usd[key] = {
            "expected_usd": expected[key],
            "pipeline_usd": None if ours is None else round(ours / 100, 2),
            "rel_err_pct": None
            if ours is None
            else round(abs(ours / 100 - expected[key]) / expected[key] * 100, 4),
        }
    p_max = max(v["abs_err_pp"] for v in p_rows.values() if v["abs_err_pp"] is not None)
    p_fail = {
        k: v["abs_err_pp"] for k, v in p_rows.items() if v["abs_err_pp"] is None or v["abs_err_pp"] > 0.5
    }

    def mx(errs: dict[str, list[float]], keys) -> float:
        vals = [e for k in keys for e in errs.get(k, [])]
        return round(max(vals), 6) if vals else 0.0

    deal_max = mx(lk_err, DEAL_USD)
    rel_max = mx(rl_err, REL_USD)
    n_lk = sum(linked.values())
    e2e = {
        "deal": _summ(e2e_err),
        "relationship": _summ(re2e_err),
        "deal_max_abs_err_usd": mx(e2e_err, DEAL_USD),
        "relationship_max_abs_err_usd": mx(re2e_err, REL_USD),
        "portfolio_rates_vs_expected_pp": {k: v["abs_err_pp"] for k, v in p_rows.items()},
        "portfolio_usd_vs_expected": p_usd,
    }
    metrics = {
        "deal_metric_max_abs_err_usd": deal_max,
        "relationship_metric_max_abs_err_usd": rel_max,
        "portfolio_metric_max_abs_err_pp": round(p_max, 4),
        "end_to_end": e2e,
        "linked_deals": n_lk,
        "deals": len(linked),
        "deals_not_linked_by_reason": dict(reasons),
        "linked_relationships": len(rmi) - sum(rel_reasons.values()),
        "relationships": len(rmi),
        "relationships_excluded_by_reason": dict(rel_reasons),
        "deal_metrics_linked": _summ(lk_err),
        "relationship_metrics_linked": _summ(rl_err),
        "portfolio_rates": p_rows,
        "spec_on_truth": {
            "chains_with_distressed_loss_share": chain_sot,
            "d17_distressed_true_renewals": n_sot,
            "true_renewals": len(sot),
            "simulator_distressed_refis": int(expected["renewed_deals_with_distressed_refi"]),
            "gap_to_simulator_labels_pp": {
                k: {
                    "definition_error_pp": p_rows[k]["definition_error_pp"],
                    "pipeline_error_pp": p_rows[k]["pipeline_error_pp"],
                }
                for k in sorted(SPEC_ON_TRUTH)
            },
        },
        "portfolio_usd": p_usd,
        "portfolio_rates_failing": p_fail,
        "missing_values": {
            f"{k}{'' if lk else ' (end to end only)'}": v for (k, lk), v in sorted(missing.items())
        },
        "truth_post_asof_events": {
            "n": len(post),
            "usd": round(float(post.amount.sum()), 2),
            "deals": int(post.deal_id.nunique()),
        },
    }
    notes = [
        f"deals: {n_lk}/{len(linked)} correctly linked (not linked: {dict(reasons)}); max $ error on linked "
        f"records {deal_max:.4f} (worst {dict((k, v[1]) for k, v in worst.items() if v[0] > 1.0)})",
        f"relationships: {metrics['linked_relationships']}/{len(rmi)} in scope (excluded: {dict(rel_reasons)}); "
        f"max $ error {rel_max:.4f}",
        f"end to end: deal max $ error {e2e['deal_max_abs_err_usd']}, relationship {e2e['relationship_max_abs_err_usd']}",
        "portfolio (pp vs expected): "
        + ", ".join(f"{v['metric']} {v['abs_err_pp']}" for v in p_rows.values()),
        "portfolio $ (rel. error %): " + ", ".join(f"{k} {v['rel_err_pct']}" for k, v in p_usd.items()),
        f"truth as-of adjustment: {len(post)} post-as-of returns (${post.amount.sum():,.2f}) removed from truth values",
    ]
    return SuiteOutput(metrics=metrics, notes=notes)
