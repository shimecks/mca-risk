"""Origination-only model features (METRICS.md §5 feature rules) with a leakage guard. Pure functions.

**Feature rules (METRICS.md §5).** Only information known at the funding date is used:

* the underwriting fields (FICO, average monthly revenue, NSFs in 90 days, positions, time in business,
  industry, state): the tracker's inline fields (2025+) first, else the Q3-matched scorecard (2023-24),
  whose application date is 0-10 days *before* funding;
* the contract terms (advance, factor, term, frequency, fee, commission %), the ISO, and
  **burden** = daily equivalent x 21 / average monthly revenue (§5; ``metrics.attributes.burden``);
* for renewals, **prior-deal features as of the renewal (= this deal's funding) date** with no
  look-ahead: D13 pace, D14 return rate and the payment ratio of the prior deal, computed from the prior
  deal's ledger events dated on or before the funding date only. A return that posts after the funding
  date does not count, even if its debit was attempted before (this is why D17 values, which look up to
  2 business days ahead under METRICS.md v1.2, are never features).

Never used: tracker ``status``, outcomes, the metric / target tables, or any payment data dated after
the funding date. ``FEATURE_INPUT_SQL`` names every input column explicitly; ``FORBIDDEN_INPUTS``
lists what must never appear in it (a static test checks both), and ``prior_events_asof`` filters the
ledger by date before any feature is computed (a perturbation test checks that).

**Harmonized time in business.** The 2025+ tracker gives ``tib_months`` (integer months); the
2023-24 scorecards give ``tib_years`` with one decimal. Both become ``tib_months`` (years x 12, so the
scorecard values are only precise to +/- 0.6 months); ``tib_source`` records which. The model uses
log(tib_months / 24), where that precision difference is negligible.

**Renewal link.** ``prior_deal_id`` is the Q2 link (explicit field, notes, or L1). At funding the funder
knows whether the new deal pays off a prior one (the payoff is netted out of the FUNDING row), so the
link itself is origination information.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from typing import Any

from mcarisk.metrics.attributes import burden
from mcarisk.metrics.deal import Deal, Event, distressed_refinance
from mcarisk.metrics.schedule import CalIndex, cal_index

# Every input column, by name (no SELECT *). Contract + underwriting fields known at funding, the Q2
# renewal link, and the Q3 scorecard (application date <= funding date).
FEATURE_INPUT_SQL = """
SELECT d.deal_id, d.merchant_key, d.funding_date, COALESCE(d.tracker_missing, false) AS tracker_missing,
       d.advance_cents, d.factor, d.rtr_cents, d.fee_cents, d.commission_rate, d.term_bdays,
       COALESCE(d.payment_cents, d.payment_cents_ach) AS payment_cents,
       COALESCE(d.frequency, d.frequency_ach) AS frequency,
       d.iso_name,
       d.prior_deal_id, d.link_method,
       d.fico AS fico_tracker, d.avg_monthly_rev_cents AS rev_tracker, d.tib_months AS tib_months_tracker,
       d.positions AS positions_tracker, d.nsf_90d AS nsf_tracker, d.industry AS industry_tracker,
       d.state AS state_tracker,
       s.app_date AS scorecard_app_date, s.fico AS fico_scorecard, s.avg_monthly_rev_cents AS rev_scorecard,
       s.tib_years AS tib_years_scorecard, s.positions AS positions_scorecard, s.nsf_90d AS nsf_scorecard,
       s.industry AS industry_scorecard, s.state AS state_scorecard
FROM deals_resolved d
LEFT JOIN scorecard_links l ON l.deal_id = d.deal_id
LEFT JOIN scorecards_std s ON s.scorecard_id = l.scorecard_id
ORDER BY d.deal_id
"""

# Ledger kinds that prior-deal features read (D7 cash, D14 returns, modifications). Only events dated on
# or before the consuming deal's funding date are passed on (``prior_events_asof``).
PRIOR_EVENT_SQL = """
SELECT event_id, deal_id, event_date, kind, amount_cents, matched_event_id, new_payment_cents
FROM ledger
WHERE kind IN ('debit', 'return', 'early_payoff_cash', 'settlement', 'modification')
ORDER BY deal_id, event_date, event_id
"""

# Substrings that must never appear in the feature input SQL (status, outcomes, post-funding metric or
# target tables, payoff results, free-text notes, D17 values). Checked by tests/test_model_unit.py.
FORBIDDEN_INPUTS = (
    "status",
    "outcome",
    "deal_metrics",
    "deal_targets",
    "relationship_metrics",
    "portfolio_metrics",
    "target_metrics",
    "payoff_date",
    "closed_by_payoff",
    "next_deal_id",
    "payoff_amt",
    "notes",
    "d17",
    "d16",
    "collections",
    "write_off",
)

NUMERIC_FEATURES = (
    "fico",
    "avg_monthly_rev_cents",
    "nsf_90d",
    "positions",
    "tib_months",
    "burden",
    "advance_cents",
    "factor",
    "term_bdays",
    "fee_cents",
    "commission_rate",
    "is_weekly",
    "is_renewal",
    "chain_seq",
    "prior_pace_asof",
    "prior_return_rate_asof",
    "prior_attempts_asof",
    "prior_modified_asof",
    "prior_payment_ratio",
    "prior_stress_rule_asof",
    "prior_term_elapsed",
)
CATEGORICAL_FEATURES = ("industry", "state", "iso")
UNDERWRITING = ("fico", "avg_monthly_rev_cents", "nsf_90d", "positions", "tib_months")


def _first(*xs: Any) -> Any:
    for x in xs:
        if x is not None and not (isinstance(x, float) and math.isnan(x)):
            return x
    return None


def underwriting(row: dict[str, Any]) -> dict[str, Any]:
    """Tracker inline underwriting fields first, else the scorecard's; TIB harmonized to months."""
    tib_m = row.get("tib_months_tracker")
    tib_src = "tracker_months" if tib_m is not None else None
    if tib_m is None and row.get("tib_years_scorecard") is not None:
        tib_m = round(float(row["tib_years_scorecard"]) * 12, 1)
        tib_src = "scorecard_years"
    return {
        "fico": _first(row.get("fico_tracker"), row.get("fico_scorecard")),
        "avg_monthly_rev_cents": _first(row.get("rev_tracker"), row.get("rev_scorecard")),
        "nsf_90d": _first(row.get("nsf_tracker"), row.get("nsf_scorecard")),
        "positions": _first(row.get("positions_tracker"), row.get("positions_scorecard")),
        "tib_months": None if tib_m is None else float(tib_m),
        "tib_source": tib_src,
        "industry": _first(row.get("industry_tracker"), row.get("industry_scorecard")),
        "state": _first(row.get("state_tracker"), row.get("state_scorecard")),
    }


def prior_events_asof(events: Iterable[Event], t: date) -> list[Event]:
    """The leakage guard for prior-deal features: only events dated on or before ``t``."""
    return sorted((e for e in events if e.event_date <= t), key=lambda e: (e.event_date, e.event_id))


def _modal_debit(events: list[Event]) -> int | None:
    debits = [e.amount_cents for e in events if e.kind == "debit"]
    if not debits:
        return None
    c = Counter(debits)
    top = max(c.values())
    return min(a for a, n in c.items() if n == top)


def prior_features(
    deal: Deal,
    prior: Deal,
    prior_rtr: int | None,
    prior_events: Iterable[Event],
    cal: CalIndex,
    *,
    prior_term_bdays: int | None = None,
) -> dict[str, Any]:
    """As-of-renewal prior-deal features for renewal ``deal`` of ``prior`` (METRICS.md §5).

    t = ``deal.funding_date``. Uses only the prior's events dated <= t (``prior_events_asof``) and the
    ``asof`` return basis (no look-ahead): D13 pace (D7 / D12 at t), D14 return rate over the last 20
    attempts, a modification on or before t, the payment ratio (new daily equivalent / prior effective
    daily equivalent at t), and the D17 *rule* evaluated on those as-of values (not the D17 metric)."""
    t = deal.funding_date
    evs = prior_events_asof(prior_events, t)
    p = prior
    if p.payment_cents is None or p.tracker_missing:
        # tracker-missing prior: the installment is the modal debit observed up to t, not over its life
        p = replace(prior, payment_cents=_modal_debit(evs) or prior.payment_cents)
    r = distressed_refinance(p, evs, prior_rtr, t, deal, cal, basis="asof", as_of=t)
    elapsed = None
    if prior_term_bdays:
        elapsed = cal.bdays_between(p.funding_date, t) / prior_term_bdays
    return {
        "prior_pace_asof": r["d13_value"],
        "prior_return_rate_asof": r["d14_value"],
        "prior_attempts_asof": r["d14_attempts"],
        "prior_modified_asof": int(r["mod_trigger"]),
        "prior_payment_ratio": r["payment_ratio"],
        "prior_stress_rule_asof": int(r["distressed"]),
        "prior_term_elapsed": elapsed,
    }


def _deal(row: dict[str, Any]) -> Deal:
    return Deal(
        deal_id=row["deal_id"],
        merchant_key=row["merchant_key"],
        funding_date=row["funding_date"],
        advance_cents=row["advance_cents"],
        rtr_cents=row["rtr_cents"],
        fee_cents=row["fee_cents"],
        commission_rate=row["commission_rate"],
        payment_cents=row["payment_cents"],
        frequency=row["frequency"],
        tracker_missing=bool(row["tracker_missing"]),
    )


def compute_features(
    rows: Iterable[dict[str, Any]], events: Iterable[Event], cal: CalIndex | None = None
) -> list[dict[str, Any]]:
    """One feature row per deal from ``FEATURE_INPUT_SQL`` rows and ledger events (``PRIOR_EVENT_SQL``).

    Missing values stay None here; imputation is the model's job (``mcarisk.model.train``)."""
    cal = cal or cal_index()
    rows = sorted(rows, key=lambda r: r["deal_id"])
    by_id = {r["deal_id"]: r for r in rows}
    ev: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        ev[e.deal_id].append(e)
    out = []
    for r in rows:
        uw = underwriting(r)
        d = _deal(r)
        pid = r["prior_deal_id"]
        seq, cur = 1, r
        seen = {r["deal_id"]}
        while cur["prior_deal_id"] is not None and cur["prior_deal_id"] in by_id:
            cur = by_id[cur["prior_deal_id"]]
            if cur["deal_id"] in seen or cur["funding_date"] > r["funding_date"]:
                break
            seen.add(cur["deal_id"])
            seq += 1
        f: dict[str, Any] = {
            "deal_id": r["deal_id"],
            "merchant_key": r["merchant_key"],
            "funding_date": r["funding_date"],
            "tracker_missing": bool(r["tracker_missing"]),
            **uw,
            "iso": r["iso_name"],
            "burden": burden(r["payment_cents"], r["frequency"], uw["avg_monthly_rev_cents"]),
            "advance_cents": r["advance_cents"],
            "factor": r["factor"],
            "term_bdays": r["term_bdays"],
            "fee_cents": r["fee_cents"],
            "commission_rate": r["commission_rate"],
            "is_weekly": None if r["frequency"] is None else int(r["frequency"] == "weekly"),
            "is_renewal": int(pid is not None),
            "chain_seq": seq,
            "prior_deal_id": pid,
            "prior_pace_asof": None,
            "prior_return_rate_asof": None,
            "prior_attempts_asof": None,
            "prior_modified_asof": None,
            "prior_payment_ratio": None,
            "prior_stress_rule_asof": None,
            "prior_term_elapsed": None,
        }
        if pid is not None and pid in by_id:
            pr = by_id[pid]
            f.update(
                prior_features(
                    d,
                    _deal(pr),
                    pr["rtr_cents"],
                    ev.get(pid, ()),
                    cal,
                    prior_term_bdays=pr["term_bdays"],
                )
            )
        f["uw_missing"] = int(any(f[k] is None for k in UNDERWRITING))
        out.append(f)
    return out


def load_feature_inputs(con) -> tuple[list[dict[str, Any]], list[Event]]:
    cur = con.execute(FEATURE_INPUT_SQL)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    events = [Event(*r) for r in con.execute(PRIOR_EVENT_SQL).fetchall()]
    return rows, events


def build_features(con) -> list[dict[str, Any]]:
    """Feature rows for every deal in ``deals_resolved`` (reads only the inputs named above)."""
    rows, events = load_feature_inputs(con)
    return compute_features(rows, events)
