"""Misposted payment repair (METRICS.md §1 rule L3).

L3: "A DEBIT posted to deal P after P's internal payoff date, while P's renewal N is active, is
reassigned to N. The reassignment is logged as a repair."

* "after" is strictly later than the payoff date ``t``.
* N is active on date ``x`` when ``N.funding_date <= x`` and N has not closed before ``x`` (``close``:
  N's own payoff date, if any).
* Only ``DEBIT`` rows move. Every other row keeps its posted deal.
* A DEBIT on a deal after that deal's payoff that L3 cannot reassign (cash payoff, or N not active) is
  left as posted and queued for review.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

REASON = "l3_posted_to_prior_after_internal_payoff"


@dataclass(frozen=True)
class Debit:
    payment_id: int
    deal_id_posted: int
    txn_date: date


def repair_mispostings(
    debits: list[Debit],
    internal: Mapping[int, tuple[date, int]],  # P -> (internal payoff date, N)
    cash_payoff: Mapping[int, date],  # P -> cash payoff date
    funding: Mapping[int, date],
    close: Mapping[int, date],
) -> tuple[dict[int, int], list[dict], list[dict]]:
    """Return ({payment_id: repaired deal}, repair log rows, exceptions)."""
    repaired: dict[int, int] = {}
    log: list[dict] = []
    exc: list[dict] = []
    for d in sorted(debits, key=lambda x: x.payment_id):
        p = d.deal_id_posted
        if p in internal:
            t, n = internal[p]
            if d.txn_date <= t:
                continue
            nf, nc = funding.get(n), close.get(n)
            if nf is not None and nf <= d.txn_date and (nc is None or d.txn_date <= nc):
                repaired[d.payment_id] = n
                log.append(
                    {
                        "payment_id": d.payment_id,
                        "txn_date": d.txn_date,
                        "deal_id_posted": p,
                        "deal_id_repaired": n,
                        "reason": REASON,
                        "detail": f"DEBIT {d.txn_date} on {p} after its internal payoff {t}; renewal {n} active",
                    }
                )
            else:
                exc.append(
                    _exc(d, f"after internal payoff {t} of {p}, but renewal {n} not active on that date")
                )
        elif p in cash_payoff and d.txn_date > cash_payoff[p]:
            exc.append(_exc(d, f"after cash payoff {cash_payoff[p]} of {p}; no renewal to reassign to"))
    return repaired, log, exc


def _exc(d: Debit, detail: str) -> dict:
    return {
        "rule_id": "L3",
        "entity": "payment",
        "entity_id": str(d.payment_id),
        "reason": "debit_after_payoff_not_reassigned",
        "detail": detail,
    }
