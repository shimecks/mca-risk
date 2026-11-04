"""Origination attributes for segmenting T1 / P4 bad rates (METRICS.md §5 feature list). Pure functions.

Only information known at the funding date (§5 feature rules): industry, ISO, number of positions and
**burden** = daily equivalent x 21 / average monthly revenue (§5; daily equivalent per §0 = payment for
daily deals, payment / 5 for weekly). SQL mirror: ``deal_attributes_sql`` in ``sql/metrics/04_targets.sql``.

Sources (``deals_resolved`` + the Q3 scorecard match):
* industry, positions, average monthly revenue: the tracker's inline underwriting fields (2025+) when
  present, else the matched scorecard (2023-24, Q3 ``scorecard_links``). Tracker-missing deals have
  neither, so they are ``unknown``.
* ISO: the tracker's ``iso_name``. Payment / frequency: the tracker's, else the ACH reconstruction (Q5).

Buckets use exact integer arithmetic so Python and SQL agree at the edges:
burden >= e% <=> payment x 21 x 100 >= e x divisor x avg monthly revenue.
"""

from __future__ import annotations

import itertools
from typing import Any

BURDEN_EDGES_PCT = (15, 20, 25, 35)  # buckets: <15%, 15-20%, 20-25%, 25-35%, >=35%
UNKNOWN = "unknown"


def burden(
    payment_cents: int | None, frequency: str | None, avg_monthly_rev_cents: int | None
) -> float | None:
    """§5 burden = daily equivalent x 21 / average monthly revenue (a fraction)."""
    if payment_cents is None or not avg_monthly_rev_cents:
        return None
    div = 5 if frequency == "weekly" else 1
    return (payment_cents * 21) / (div * avg_monthly_rev_cents)


def burden_bucket(payment_cents: int | None, frequency: str | None, avg_monthly_rev_cents: int | None) -> str:
    if payment_cents is None or not avg_monthly_rev_cents:
        return UNKNOWN
    div = 5 if frequency == "weekly" else 1
    lhs = payment_cents * 21 * 100
    edges = BURDEN_EDGES_PCT
    if lhs < edges[0] * div * avg_monthly_rev_cents:
        return f"<{edges[0]}%"
    for lo, hi in itertools.pairwise(edges):
        if lhs < hi * div * avg_monthly_rev_cents:
            return f"{lo}-{hi}%"
    return f">={edges[-1]}%"


def positions_bucket(positions: int | None) -> str:
    if positions is None:
        return UNKNOWN
    return "3+" if positions >= 3 else str(int(positions))


def deal_attributes(row: dict[str, Any]) -> dict[str, Any]:
    """Segment labels for one deal from its joined inputs (see ``ATTR_INPUT_SQL``)."""
    pay, freq = row["payment_cents"], row["frequency"]
    rev = row["avg_monthly_rev_cents"]
    return {
        "deal_id": row["deal_id"],
        "industry": row["industry"] or UNKNOWN,
        "iso": row["iso_name"] or UNKNOWN,
        "positions": positions_bucket(row["positions"]),
        "burden": burden_bucket(pay, freq, rev),
        "positions_n": row["positions"],
        "avg_monthly_rev_cents": rev,
        "burden_value": burden(pay, freq, rev),
        "attr_source": row["attr_source"],
    }


# The joined inputs, one row per deal. Input plumbing only (no metric logic).
ATTR_INPUT_SQL = """
SELECT d.deal_id,
       COALESCE(d.payment_cents, d.payment_cents_ach) AS payment_cents,
       COALESCE(d.frequency, d.frequency_ach) AS frequency,
       COALESCE(d.industry, s.industry) AS industry,
       d.iso_name,
       COALESCE(d.positions, s.positions) AS positions,
       COALESCE(d.avg_monthly_rev_cents, s.avg_monthly_rev_cents) AS avg_monthly_rev_cents,
       CASE WHEN COALESCE(d.tracker_missing, false) THEN 'none'
            WHEN s.scorecard_id IS NOT NULL THEN 'tracker+scorecard'
            ELSE 'tracker' END AS attr_source
FROM deals_resolved d
LEFT JOIN scorecard_links l ON l.deal_id = d.deal_id
LEFT JOIN scorecards_std s ON s.scorecard_id = l.scorecard_id
ORDER BY d.deal_id
"""
