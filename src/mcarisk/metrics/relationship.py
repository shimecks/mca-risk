"""Relationship-level metrics R1-R10 (METRICS.md §3). Pure functions over ``deal_metrics`` rows.

A relationship is every deal of one resolved merchant (``merchant_key``, Q1), ordered by funding date
(ties: deal id). The *last deal* decides R5. SQL mirror: ``sql/metrics/02_relationship_metrics.sql``.

Conventions:
* Only deals funded on or before the as-of date belong to the relationship (deal_metrics already
  excludes later ones).
* R6 / R7 apply to open relationships and R8 to resolved ones; they are NULL otherwise.
* R7 = remaining RTR of the last (open) deal = D2 - D7 - D8; NULL when its RTR is unknown
  (tracker-missing).
* R1 includes D6 as computed: when a deal's commission is unknown (tracker-missing), R1 is a lower bound
  and ``n_commission_unknown`` counts those deals.
* R10: ``n_deals``; ``n_distressed_refi`` = renewals of this relationship's deals where D17 holds;
  ``days_to_resolution`` = calendar days from the first funding to the last deal's outcome date
  (resolved only).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

RESOLVED_LAST_OUTCOMES = ("paid_in_full", "paid_in_full_early", "settled", "written_off")


def relationship_metrics(deals: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """R1-R10 for one relationship, from its deals' ``deal_metrics`` rows (same as_of, same merchant)."""
    ds = sorted(deals, key=lambda d: (d["funding_date"], d["deal_id"]))
    if not ds:
        raise ValueError("a relationship needs at least one deal")
    keys = {d["merchant_key"] for d in ds}
    if len(keys) != 1:
        raise ValueError(f"deals from several merchants: {sorted(keys)}")
    last = ds[-1]
    r1 = sum(d["d6_funder_cash_out_cents"] for d in ds)  # R1 funder cash out
    r2 = sum(d["d7_cash_from_merchant_cents"] for d in ds)  # R2 cash from merchant
    r3 = r2 / r1 if r1 else None  # R3 cash multiple
    r4 = r2 - r1  # R4 net cash position
    r5 = "resolved" if last["outcome"] in RESOLVED_LAST_OUTCOMES else "open"  # R5 status
    is_open = r5 == "open"
    r6 = max(0, -r4) if is_open else None  # R6 economic exposure (open only)
    r7 = last["remaining_rtr_cents"] if is_open else None  # R7 contractual balance (open only)
    r8 = max(0, -r4) if not is_open else None  # R8 realized loss (resolved only)
    r9 = (not is_open) and r3 is not None and r3 < 1.0  # R9 relationship bad
    first = ds[0]["funding_date"]
    days = (last["outcome_date"] - first).days if (not is_open and last["outcome_date"]) else None
    return {
        "merchant_key": last["merchant_key"],
        "as_of": last["as_of"],
        "first_funding_date": first,
        "first_funding_quarter": f"{first.year}Q{(first.month - 1) // 3 + 1}",
        "last_deal_id": last["deal_id"],
        "last_outcome": last["outcome"],
        "r1_funder_cash_out_cents": r1,
        "r2_cash_from_merchant_cents": r2,
        "r3_cash_multiple": r3,
        "r4_net_cash_position_cents": r4,
        "r5_status": r5,
        "r6_economic_exposure_cents": r6,
        "r7_contractual_balance_cents": r7,
        "r8_realized_loss_cents": r8,
        "r9_relationship_bad": r9,
        "r10_n_deals": len(ds),
        "r10_n_distressed_refi": sum(bool(d["d17_distressed"]) for d in ds),
        "r10_days_to_resolution": days,
        "n_commission_unknown": sum(not d["commission_known"] for d in ds),
        "reported_collected_cents": sum(d["d9_reported_collected_cents"] for d in ds),
    }


def all_relationship_metrics(deal_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for d in deal_rows:
        by.setdefault(d["merchant_key"], []).append(d)
    return [relationship_metrics(v) for _, v in sorted(by.items())]
