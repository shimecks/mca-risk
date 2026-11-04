"""T1 fixed-horizon target (METRICS.md §5) and its rates by vintage and segment. Pure functions.

**T1** = D19 (economic bad, METRICS.md §2) within **180 business days** of funding. Population: deals funded
at least 180 Fed ACH business days before the as-of date. SQL mirror: ``t1_targets_sql`` /
``t1_metrics_sql`` in ``sql/metrics/04_targets.sql`` (tests assert Python == SQL).

Conventions (METRICS.md §5 says "within 180 business days of funding"; this module fixes the reading):

* **Horizon date** h = ``add_business_days(funding_date, 180)`` (the 180th business day strictly after
  funding; ``mcarisk.calendar``).
* **Mature** deal: h <= the as-of date. Only mature deals carry a target; immature deals are kept in
  ``deal_targets`` with ``mature = false`` and NULL targets, so a vintage can be flagged.
* **T1 economic** = D19 of ``deal_metrics(deal, as_of=h)``: the deal evaluated with every event dated
  after h ignored (§0 as-of discipline). A deal defaulted, settled, written off or refinanced
  distressed (D17) by h is bad. **T1 naive** = D18 at h (reported alongside, §5).
  Under the default D17 ``attempt`` basis the renewal-date returns look ahead up to h only.
* **Rates** (``t1_rates``): bad / mature deals, per dimension x segment. ``maturity`` of a group:
  ``mature`` when every funded deal in it is mature, ``immature`` when none is, else ``partial``. For
  the vintage dimension this marks vintages younger than the horizon.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

from mcarisk.calendar import add_business_days
from mcarisk.metrics.deal import (
    AS_OF_DEFAULT,
    D17_DEFAULT_BASIS,
    Deal,
    Event,
    StatusEvent,
    deal_metrics,
    vintage,
)
from mcarisk.metrics.schedule import cal_index

T1_HORIZON_BDAYS = 180  # METRICS.md §5 T1
T1_DIMENSIONS = ("all", "vintage", "industry", "iso", "positions", "burden")


def t1_horizon_date(funding_date: date, horizon_bdays: int = T1_HORIZON_BDAYS) -> date:
    """T1 horizon: the ``horizon_bdays``-th Fed ACH business day strictly after funding."""
    return add_business_days(funding_date, horizon_bdays)


def is_mature(funding_date: date, as_of: date, horizon_bdays: int = T1_HORIZON_BDAYS) -> bool:
    """T1 population: the horizon date is on or before the as-of date."""
    return t1_horizon_date(funding_date, horizon_bdays) <= as_of


def t1_deal_targets(
    deals: Iterable[Deal],
    events: Iterable[Event],
    status: Iterable[StatusEvent] = (),
    *,
    as_of: date = AS_OF_DEFAULT,
    d17_basis: str = D17_DEFAULT_BASIS,
    horizon_bdays: int = T1_HORIZON_BDAYS,
) -> list[dict[str, Any]]:
    """One row per deal funded on or before ``as_of``: horizon date, maturity and (mature only) T1."""
    deals = list(deals)
    by_id = {d.deal_id: d for d in deals}
    ev: dict[int, list[Event]] = defaultdict(list)
    for e in events:
        ev[e.deal_id].append(e)
    st: dict[int, list[StatusEvent]] = defaultdict(list)
    for s in status:
        st[s.deal_id].append(s)
    cal = cal_index()
    out = []
    for d in sorted(deals, key=lambda x: x.deal_id):
        if d.funding_date > as_of:
            continue
        h = t1_horizon_date(d.funding_date, horizon_bdays)
        mature = h <= as_of
        m = None
        if mature:
            m = deal_metrics(
                d,
                ev.get(d.deal_id, ()),
                st.get(d.deal_id, ()),
                as_of=h,
                successors=by_id,
                cal=cal,
                d17_basis=d17_basis,
            )
        out.append(
            {
                "deal_id": d.deal_id,
                "merchant_key": d.merchant_key,
                "funding_date": d.funding_date,
                "vintage": vintage(d.funding_date),
                "horizon_bdays": horizon_bdays,
                "horizon_date": h,
                "mature": mature,
                "outcome_at_horizon": m["outcome"] if m else None,
                "t1_bad_economic": m["d19_bad_economic"] if m else None,  # T1 (D19 at the horizon)
                "t1_bad_naive": m["d18_bad_naive"] if m else None,  # D18 at the horizon, for contrast
                "as_of": as_of,
            }
        )
    return out


def t1_rates(
    target_rows: Iterable[dict[str, Any]],
    attributes: dict[int, dict[str, str]] | None = None,
    *,
    as_of: date = AS_OF_DEFAULT,
    horizon_bdays: int = T1_HORIZON_BDAYS,
) -> list[dict[str, Any]]:
    """T1 economic and naive bad rates over mature deals, for every dimension in ``T1_DIMENSIONS``.

    ``attributes`` maps deal_id -> {industry, iso, positions, burden} segment labels (from
    ``mcarisk.metrics.attributes``); a missing deal gets ``unknown``. Long rows: metric_id ``T1``,
    metric ``bad_rate_economic`` | ``bad_rate_naive``, dimension, segment, value, numerator,
    denominator (mature deals), n_deals (funded deals), maturity."""
    attributes = attributes or {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in target_rows:
        a = attributes.get(r["deal_id"], {})
        groups[("all", "all")].append(r)
        groups[("vintage", r["vintage"])].append(r)
        for dim in ("industry", "iso", "positions", "burden"):
            groups[(dim, a.get(dim) or "unknown")].append(r)
    out = []
    for (dim, seg), g in sorted(groups.items()):
        mat = [r for r in g if r["mature"]]
        n_m = len(mat)
        maturity = "mature" if n_m == len(g) else ("immature" if n_m == 0 else "partial")
        for metric, col in (("bad_rate_economic", "t1_bad_economic"), ("bad_rate_naive", "t1_bad_naive")):
            nb = sum(bool(r[col]) for r in mat)
            out.append(
                {
                    "metric_id": "T1",
                    "metric": metric,
                    "dimension": dim,
                    "segment": seg,
                    "value": nb / n_m if n_m else None,
                    "numerator": nb,
                    "denominator": n_m,
                    "n_deals": len(g),
                    "maturity": maturity,
                    "horizon_bdays": horizon_bdays,
                    "as_of": as_of,
                }
            )
    return out


# ------------------------------------------------------------------------------------------ T2 / T3
T2_BAD_OUTCOMES = ("written_off", "settled", "defaulted", "refinanced_distressed")  # D19 (METRICS.md §2)
T2_GOOD_CLOSES = ("renewed_healthy", "paid_in_full_early", "paid_in_full")
T3_WINDOW_MONTHS = 18  # METRICS.md §5 T3


def t2_survival_rows(
    deal_metric_rows: Iterable[dict[str, Any]], *, as_of: date = AS_OF_DEFAULT
) -> list[dict]:
    """METRICS.md §5 **T2**: time to the D19 event, for every deal; censored at a good close or as-of.

    Reads ``deal_metrics`` rows as of ``as_of`` (D19 is not redefined here). Event date: the D16
    default date (last good payment + 1 bd) when D16 fired, else the outcome date (the internal-payoff
    date for ``refinanced_distressed``; the write-off / settlement date otherwise). Censoring date: the
    outcome date of a good close (``T2_GOOD_CLOSES``), else ``as_of`` for open deals. Duration is in Fed
    ACH business days from funding (``business_days_between``), at least 1 for an event."""
    from mcarisk.calendar import business_days_between

    out = []
    for m in deal_metric_rows:
        if m["d19_bad_economic"]:
            end = m["d16_default_date"] if m["d16_default"] and m["d16_default_date"] else m["outcome_date"]
            end = end or as_of
            event = True
        elif m["outcome"] in T2_GOOD_CLOSES and m["outcome_date"] is not None:
            end, event = m["outcome_date"], False
        else:
            end, event = as_of, False
        end = min(end, as_of)
        dur = business_days_between(m["funding_date"], end) if end > m["funding_date"] else 0
        out.append(
            {
                "deal_id": m["deal_id"],
                "funding_date": m["funding_date"],
                "end_date": end,
                "duration_bdays": max(dur, 1) if event else dur,
                "event": event,
                "outcome": m["outcome"],
            }
        )
    return out


def _add_months(d: date, k: int) -> date:
    import calendar as _cal

    y, mth = divmod(d.month - 1 + k, 12)
    y, mth = d.year + y, mth + 1
    return date(y, mth, min(d.day, _cal.monthrange(y, mth)[1]))


def t3_relationship_rows(
    rel_rows: Iterable[dict[str, Any]], *, as_of: date = AS_OF_DEFAULT, window_months: int = T3_WINDOW_MONTHS
) -> list[dict]:
    """METRICS.md §5 **T3**: relationship R9, for relationships resolved within 18 months of first funding.

    ``observable`` = the whole window has elapsed (first funding + 18 months <= as-of), so the population
    is not limited to fast resolvers. ``in_population`` = observable and resolved (R5) by first funding
    + 18 months (R10 days to resolution). Survivorship caveat (§5): relationships still open at 18 months
    (long-lived chains) are left out."""
    out = []
    for r in rel_rows:
        first = r["first_funding_date"]
        end = _add_months(first, window_months)
        observable = end <= as_of
        days = r["r10_days_to_resolution"]
        resolved_in = (
            r["r5_status"] == "resolved"
            and days is not None
            and (first.toordinal() + days) <= end.toordinal()
        )
        out.append(
            {
                "merchant_key": r["merchant_key"],
                "first_funding_date": first,
                "window_end": end,
                "observable": observable,
                "in_population": bool(observable and resolved_in),
                "t3_bad": bool(r["r9_relationship_bad"]) if (observable and resolved_in) else None,
            }
        )
    return out
