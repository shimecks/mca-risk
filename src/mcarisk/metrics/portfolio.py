"""Portfolio metrics P1-P9 (METRICS.md §4). Pure functions over deal / relationship metric rows.

Output is a long table: one row per (metric_id, metric, segment, k) with ``value``, ``numerator``,
``denominator`` and ``unit`` (ratio | pp | cents | count). ``segment`` is ``all`` for the headline row,
a funding-quarter vintage for P4 / P7 rows, ``closed`` for the P9 variant. SQL mirror:
``sql/metrics/03_portfolio_metrics.sql``.

Conventions (METRICS.md leaves these open):
* **Closed deals** (P1, P4): outcome in written_off, settled, refinanced_distressed, renewed_healthy,
  paid_in_full_early, paid_in_full. ``defaulted`` is open (R5 treats it as open). P1 is RTR-weighted,
  so closed deals with unknown RTR (tracker-missing, early payoff) are left out of P1 only.
* P4 vintage = funding quarter of the deal. P7 vintage = quarter of the relationship's first funding.
* P7: for month k, cash in / cash out of the relationship's ledger events dated on or before
  first funding + k calendar months (day clamped to month end); a relationship enters month k only if
  that date is on or before the as-of date (censoring).
* P8 distressed share = renewals where D17 holds / all renewals (deals closed by an internal payoff);
  the loss rates are R9 rates over resolved relationships with / without a D17 renewal.
* P9 = modified deals (any modification event) whose outcome is paid_in_full or renewed_healthy, over
  all modified deals (segment ``all``) and over closed modified deals (segment ``closed``).
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

from mcarisk.metrics.deal import AS_OF_DEFAULT, CLOSED_OUTCOMES

CASH_IN = ("debit", "return", "early_payoff_cash", "settlement")
CASH_OUT = ("funding_out", "commission_out")


def add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _row(mid: str, metric: str, value, num=None, den=None, unit="ratio", segment="all", k=None, as_of=None):
    return {
        "metric_id": mid,
        "metric": metric,
        "segment": segment,
        "k": k,
        "value": None if value is None else float(value),
        "numerator": num,
        "denominator": den,
        "unit": unit,
        "as_of": as_of,
    }


def portfolio_metrics(
    deal_rows: Iterable[dict[str, Any]],
    rel_rows: Iterable[dict[str, Any]],
    cash_events: Iterable[tuple[str, date, str, int]],
    *,
    as_of: date = AS_OF_DEFAULT,
) -> list[dict[str, Any]]:
    """P1-P9. ``cash_events``: (merchant_key, event_date, kind, amount_cents) for the cash ledger kinds."""
    deals = list(deal_rows)
    rels = list(rel_rows)
    out: list[dict[str, Any]] = []

    def add(*a, **kw):
        out.append(_row(*a, as_of=as_of, **kw))

    closed = [d for d in deals if d["outcome"] in CLOSED_OUTCOMES]
    # P1 reported vs adjusted collection rate, RTR-weighted over closed deals
    w = [d for d in closed if d["d2_rtr_cents"] is not None]
    rtr = sum(d["d2_rtr_cents"] for d in w)
    rep = sum(d["d9_reported_collected_cents"] for d in w)
    adj = sum(d["d7_cash_from_merchant_cents"] for d in w)
    p1r, p1a = _ratio(rep, rtr), _ratio(adj, rtr)
    add("P1", "reported_collection_rate", p1r, rep, rtr)
    add("P1", "adjusted_collection_rate", p1a, adj, rtr)
    # P2 renewal masking gap (pp)
    add("P2", "renewal_masking_gap_pp", None if p1r is None else (p1r - p1a) * 100, rep - adj, rtr, unit="pp")
    # P3 internal-payoff share of reported collections, all deals
    d8 = sum(d["d8_internal_payoff_out_cents"] for d in deals)
    d9 = sum(d["d9_reported_collected_cents"] for d in deals)
    add("P3", "internal_payoff_share", _ratio(d8, d9), d8, d9)
    # P4 bad rates over closed deals, overall and by vintage
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in closed:
        groups["all"].append(d)
        groups[d["vintage"]].append(d)
    for seg in sorted(groups):
        g = groups[seg]
        nb = sum(bool(d["d18_bad_naive"]) for d in g)
        eb = sum(bool(d["d19_bad_economic"]) for d in g)
        add("P4", "bad_rate_naive", _ratio(nb, len(g)), nb, len(g), segment=seg)
        add("P4", "bad_rate_economic", _ratio(eb, len(g)), eb, len(g), segment=seg)
    # P5 relationship loss metrics over resolved relationships
    res = [r for r in rels if r["r5_status"] == "resolved"]
    nbad = sum(bool(r["r9_relationship_bad"]) for r in res)
    r1 = sum(r["r1_funder_cash_out_cents"] for r in res)
    r2 = sum(r["r2_cash_from_merchant_cents"] for r in res)
    r8 = sum(r["r8_realized_loss_cents"] for r in res)
    add("P5", "resolved_bad_share", _ratio(nbad, len(res)), nbad, len(res))
    add("P5", "capital_weighted_loss_rate", _ratio(r8, r1), r8, r1)
    add("P5", "resolved_cash_multiple", _ratio(r2, r1), r2, r1)
    # P6 exposure over open relationships
    op = [r for r in rels if r["r5_status"] == "open"]
    r6 = sum(r["r6_economic_exposure_cents"] for r in op)
    r7 = sum(r["r7_contractual_balance_cents"] or 0 for r in op)
    n_unk = sum(r["r7_contractual_balance_cents"] is None for r in op)
    add("P6", "economic_exposure_cents", r6, r6, len(op), unit="cents")
    add("P6", "contractual_balance_cents", r7, r7, len(op) - n_unk, unit="cents")
    # P7 vintage curves: cumulative R2 / R1 by months since first funding
    ff = {r["merchant_key"]: r["first_funding_date"] for r in rels}
    q = {r["merchant_key"]: r["first_funding_quarter"] for r in rels}
    ev: dict[str, list[tuple[date, str, int]]] = defaultdict(list)
    for mk, dt, kind, amt in cash_events:
        if dt <= as_of and mk in ff:
            ev[mk].append((dt, kind, amt))
    cin: dict[tuple[str, int], int] = defaultdict(int)
    cout: dict[tuple[str, int], int] = defaultdict(int)
    for mk, first in ff.items():
        es = sorted(ev.get(mk, []))
        k = i = run_in = run_out = 0
        while (cut := add_months(first, k)) <= as_of:
            while i < len(es) and es[i][0] <= cut:
                _, kd, a = es[i]
                if kd in CASH_IN:
                    run_in += a
                elif kd in CASH_OUT:
                    run_out -= a
                i += 1
            cin[(q[mk], k)] += run_in
            cout[(q[mk], k)] += run_out
            k += 1
    for seg, k in sorted(cin.keys() | cout.keys()):
        add(
            "P7",
            "cumulative_cash_multiple",
            _ratio(cin[(seg, k)], cout[(seg, k)]),
            cin[(seg, k)],
            cout[(seg, k)],
            segment=seg,
            k=k,
        )
    # P8 refinance quality
    ren = [d for d in deals if d["d17_distressed"] is not None]
    nd = sum(bool(d["d17_distressed"]) for d in ren)
    add("P8", "distressed_share_of_renewals", _ratio(nd, len(ren)), nd, len(ren))
    wd = [r for r in res if r["r10_n_distressed_refi"] > 0]
    wo = [r for r in res if r["r10_n_distressed_refi"] == 0]
    for name, g in (("bad_rate_chains_with_distressed", wd), ("bad_rate_chains_without_distressed", wo)):
        nb = sum(bool(r["r9_relationship_bad"]) for r in g)
        add("P8", name, _ratio(nb, len(g)), nb, len(g))
    # P9 modification cure rate
    mod = [d for d in deals if d["has_modification"]]
    for seg, g in (("all", mod), ("closed", [d for d in mod if d["outcome"] in CLOSED_OUTCOMES])):
        nc = sum(d["outcome"] in ("paid_in_full", "renewed_healthy") for d in g)
        add("P9", "modification_cure_rate", _ratio(nc, len(g)), nc, len(g), segment=seg)
    return out
