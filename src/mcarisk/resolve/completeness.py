"""Tracker-missing deals reconstructed from ACH (METRICS.md §6 Q5).

Q5: "Every deal with ACH activity has a tracker row. Missing rows are reconstructed from ACH (funding
amount) and flagged tracker_missing."

A reconstructed deal gets what the ACH export can tell honestly:
  * ``funding_date`` and ``funding_net_cents`` from its FUNDING row (lineage = that row);
  * ``merchant_name_raw`` = the deal's most frequent ACH name;
  * ``payment_cents_ach`` = the most frequent DEBIT amount, ``frequency_ach`` from the remittance
    description (``DAILY REMIT`` / ``WEEKLY REMIT``). These are observations, not contract terms.
Contract fields (advance, factor, RTR, fee, commission, term) stay NULL: they are not in the ACH data.
A deal with ACH rows but no FUNDING row is queued for review and gets the date of its first ACH row.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

FLAG = "tracker_missing"


def reconstruct_missing(tracker_ids: set[int], payments: list[dict]) -> tuple[list[dict], list[dict]]:
    """``payments``: dicts with deal_id_posted, txn_date, txn_type, amount_cents, description,
    merchant_name_raw, payment_id + lineage. Returns (reconstructed deal rows, exceptions)."""
    by_deal: dict[int, list[dict]] = {}
    for p in payments:
        if p["deal_id_posted"] not in tracker_ids:
            by_deal.setdefault(p["deal_id_posted"], []).append(p)
    out, exc = [], []
    for deal, rows in sorted(by_deal.items()):
        rows = sorted(rows, key=lambda r: (r["txn_date"], r["payment_id"]))
        funding = [r for r in rows if r["txn_type"] == "FUNDING"]
        names = Counter(r["merchant_name_raw"] for r in rows)
        top = max(names.values())
        name = sorted((n for n, c in names.items() if c == top), key=lambda s: (-len(s), s))[0]
        debits = Counter(r["amount_cents"] for r in rows if r["txn_type"] == "DEBIT")
        pay = None
        if debits:
            top_d = max(debits.values())
            pay = max(a for a, c in debits.items() if c == top_d)
        descs = Counter((r["description"] or "").upper() for r in rows if r["txn_type"] == "DEBIT")
        freq = None
        if descs:
            d0 = descs.most_common(1)[0][0]
            freq = "daily" if "DAILY" in d0 else "weekly" if "WEEKLY" in d0 else None
        src = funding[0] if funding else rows[0]
        if not funding:
            exc.append(
                {
                    "rule_id": "Q5",
                    "entity": "deal",
                    "entity_id": str(deal),
                    "reason": "tracker_missing_without_funding_row",
                    "detail": f"{len(rows)} ACH rows, no FUNDING; funding date taken from first row",
                }
            )
        fd: date = src["txn_date"]
        out.append(
            {
                "deal_id": deal,
                "merchant_name_raw": name,
                "funding_date": fd,
                "funding_net_cents": -funding[0]["amount_cents"] if funding else None,
                "payment_cents_ach": pay,
                "frequency_ach": freq,
                "n_ach_rows": len(rows),
                "src_file": src["src_file"],
                "src_sheet": src["src_sheet"],
                "src_row": src["src_row"],
            }
        )
    return out, exc
