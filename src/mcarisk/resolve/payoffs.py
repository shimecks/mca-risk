"""Internal vs cash payoff classification (METRICS.md §1 rule L1).

L1: "A PAYOFF row on deal P dated t is an internal_payoff if a deal N exists that satisfies all of:
  * N belongs to the same resolved merchant;
  * N funds within 0-2 business days of t;
  * either the tracker links N to P (renewal_of / Prior Deal / notes), or the payoff amount is within
    +-$1 of N's payoff_amt, or N's funding row satisfies advance - fee - funding_out ~= payoff amount
    (+-$1).
Otherwise the payoff is early_payoff_cash. The description text is a hint only."

Implementation notes
--------------------
* Business days are the Fed ACH calendar (:mod:`mcarisk.calendar`). "Within 0-2 business days of t" is
  read as ``|business days between t and N's funding date| <= 2`` (either side of t). On this book the
  true lag is always 0, so the two readings give identical results.
* ``funding_out`` is the ACH FUNDING amount, stored negative (funder perspective); the identity uses its
  magnitude: ``advance - fee - |funding_out| = payoff``.
* The description ("RENEWAL PAYOFF", "PAYOFF", "EARLY PAYOFF", "PAYOFF - DISC") never decides the class.
  A disagreement between the hint and the class goes to the review queue.
* A candidate N with **no tracker row** (Q5 ``tracker_missing``) has no ``payoff_amt``, ``advance`` or
  ``fee`` and no tracker link, so L1's third bullet cannot be evaluated for it. Policy
  ``strict`` (default, METRICS.md as written) classifies the payoff ``early_payoff_cash`` and queues an
  exception; policy ``infer`` (an extension that is *not* in METRICS.md v1.0; opt-in via
  ``MCARISK_L1_TRACKER_MISSING=infer``) classifies it ``internal_payoff`` with method
  ``inferred_tracker_missing`` and confidence 0.6, also queued for review.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from mcarisk.calendar import business_days_between

INTERNAL = "internal_payoff"
CASH = "early_payoff_cash"
WINDOW_BDAYS = 2
AMOUNT_TOL_CENTS = 100  # +-$1
POLICY_STRICT = "strict"
POLICY_INFER = "infer"
POLICY_ENV = "MCARISK_L1_TRACKER_MISSING"

HINT_INTERNAL = {"RENEWAL PAYOFF"}
HINT_CASH = {"EARLY PAYOFF", "PAYOFF - DISC"}


def policy_from_env() -> str:
    p = os.environ.get(POLICY_ENV, POLICY_STRICT).strip().lower() or POLICY_STRICT
    if p not in (POLICY_STRICT, POLICY_INFER):
        raise ValueError(f"{POLICY_ENV} must be '{POLICY_STRICT}' or '{POLICY_INFER}', got {p!r}")
    return p


@dataclass(frozen=True)
class DealInfo:
    deal_id: int
    merchant_key: str
    funding_date: date
    advance_cents: int | None = None
    fee_cents: int | None = None
    funding_net_cents: int | None = None  # |ACH FUNDING amount|
    payoff_amt_cents: int | None = None  # tracker payoff_amt (2025+)
    tracker_missing: bool = False


@dataclass(frozen=True)
class Payoff:
    payment_id: int
    deal_id: int  # P, as posted
    txn_date: date
    amount_cents: int
    description: str | None = None


def bdays_apart(a: date, b: date) -> int:
    """Business days between two dates, regardless of order (0 for the same day)."""
    lo, hi = (a, b) if a <= b else (b, a)
    return business_days_between(lo, hi)


def evaluate_candidate(p: Payoff, n: DealInfo, tracker_links: Mapping[int, int]) -> dict[str, bool | None]:
    """The three L1 criteria for one candidate successor (None = cannot be evaluated)."""
    link = tracker_links.get(n.deal_id) == p.deal_id
    pa = None if n.payoff_amt_cents is None else abs(n.payoff_amt_cents - p.amount_cents) <= AMOUNT_TOL_CENTS
    if None in (n.advance_cents, n.fee_cents, n.funding_net_cents):
        ff = None
    else:
        ff = abs(n.advance_cents - n.fee_cents - n.funding_net_cents - p.amount_cents) <= AMOUNT_TOL_CENTS
    return {"tracker_link": link, "payoff_amt": pa, "funding_formula": ff}


def _hint(desc: str | None) -> str | None:
    d = (desc or "").strip().upper()
    if d in HINT_INTERNAL:
        return INTERNAL
    if d in HINT_CASH:
        return CASH
    return None


def classify_payoffs(
    payoffs: list[Payoff],
    deals: Mapping[int, DealInfo],
    tracker_links: Mapping[int, int],
    policy: str = POLICY_STRICT,
) -> tuple[list[dict], list[dict]]:
    """Classify every PAYOFF row (L1). Returns (rows, exceptions)."""
    by_key: dict[str, list[DealInfo]] = {}
    for d in deals.values():
        by_key.setdefault(d.merchant_key, []).append(d)
    rows: list[dict] = []
    exc: list[dict] = []
    claimed: dict[int, int] = {}  # successor deal -> payment_id that claimed it
    for p in sorted(payoffs, key=lambda x: (x.txn_date, x.payment_id)):
        pd_ = deals.get(p.deal_id)
        cands: list[tuple[DealInfo, int, dict]] = []
        if pd_ is not None:
            for n in by_key.get(pd_.merchant_key, []):
                if n.deal_id == p.deal_id:
                    continue
                gap = bdays_apart(p.txn_date, n.funding_date)
                if gap <= WINDOW_BDAYS:
                    cands.append((n, gap, evaluate_candidate(p, n, tracker_links)))
        sat = [c for c in cands if any(v is True for v in c[2].values())]
        unevaluable = [c for c in cands if c not in sat and c[0].tracker_missing]
        rec = {
            "payment_id": p.payment_id,
            "deal_id": p.deal_id,
            "txn_date": p.txn_date,
            "amount_cents": p.amount_cents,
            "description": p.description,
            "n_candidates": len(cands),
            "successor_deal_id": None,
            "bday_gap": None,
            "l1_tracker_link": None,
            "l1_payoff_amt": None,
            "l1_funding_formula": None,
        }
        if sat:
            sat.sort(
                key=lambda c: (
                    -sum(v is True for v in c[2].values()),
                    not c[2]["tracker_link"],
                    c[1],
                    c[0].deal_id,
                )
            )
            n, gap, crit = sat[0]
            n_true = sum(v is True for v in crit.values())
            method = next(k for k in ("tracker_link", "payoff_amt", "funding_formula") if crit[k] is True)
            conf = 1.0 if n_true >= 2 else (0.95 if method != "tracker_link" else 0.9)
            if len(sat) > 1:
                conf = min(conf, 0.7)
                exc.append(
                    _exc(p, "multiple_successor_candidates", f"candidates {[c[0].deal_id for c in sat]}")
                )
            rec.update(
                payoff_class=INTERNAL,
                successor_deal_id=n.deal_id,
                bday_gap=gap,
                l1_tracker_link=crit["tracker_link"],
                l1_payoff_amt=crit["payoff_amt"],
                l1_funding_formula=crit["funding_formula"],
                method=method,
                confidence=conf,
            )
        elif unevaluable:
            n, gap, crit = sorted(unevaluable, key=lambda c: (c[1], c[0].deal_id))[0]
            detail = (
                f"same-merchant deal {n.deal_id} funded {n.funding_date} ({gap} bd) has no tracker row: "
                "L1 amount/link criteria cannot be evaluated"
            )
            if policy == POLICY_INFER:
                rec.update(
                    payoff_class=INTERNAL,
                    successor_deal_id=n.deal_id,
                    bday_gap=gap,
                    method="inferred_tracker_missing",
                    confidence=0.6,
                )
                exc.append(_exc(p, "l1_successor_tracker_missing_inferred_internal", detail))
            else:
                rec.update(payoff_class=CASH, method="l1_unevaluable_tracker_missing", confidence=0.5)
                exc.append(_exc(p, "l1_successor_tracker_missing_classified_cash", detail))
        else:
            rec.update(
                payoff_class=CASH,
                method="no_successor" if not cands else "successor_criteria_failed",
                confidence=1.0 if not cands else 0.9,
            )
            if cands:
                exc.append(
                    _exc(
                        p,
                        "same_merchant_funding_in_window_not_linked",
                        f"deal(s) {[c[0].deal_id for c in cands]} fund within {WINDOW_BDAYS} bd but meet "
                        "no L1 criterion; classified early_payoff_cash",
                    )
                )
        hint = _hint(p.description)
        rec["description_hint"] = hint
        rec["hint_agrees"] = None if hint is None else hint == rec["payoff_class"]
        if hint is not None and hint != rec["payoff_class"]:
            exc.append(
                _exc(
                    p, "description_hint_disagrees", f"'{p.description}' but classified {rec['payoff_class']}"
                )
            )
        s = rec["successor_deal_id"]
        if s is not None:
            if s in claimed:
                exc.append(
                    _exc(p, "successor_claimed_twice", f"deal {s} also claimed by payment {claimed[s]}")
                )
            claimed.setdefault(s, p.payment_id)
        rows.append(rec)
    return rows, exc


def _exc(p: Payoff, reason: str, detail: str) -> dict:
    return {
        "rule_id": "L1",
        "entity": "payment",
        "entity_id": str(p.payment_id),
        "reason": reason,
        "detail": f"PAYOFF deal {p.deal_id} {p.txn_date} ${p.amount_cents / 100:,.2f}: {detail}",
    }
