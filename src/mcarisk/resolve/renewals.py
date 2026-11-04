"""Renewal links (METRICS.md §6 Q2).

Q2: "In priority order: an explicit field (renewal_of, Prior Deal); notes regex (renewal of NNNN,
paid off #NNNN, refi #NNNN); inference via L1. Each link records its method."

Methods and confidence:
  explicit   ``deals_std.prior_deal_id`` (2024 ``Prior Deal``, 2025+ ``renewal_of``)        1.0
  regex      a deal number in the tracker notes. Patterns present in this book:
             ``renewal of NNNN``, ``RENEWAL - paid off #NNNN``, ``refi #NNNN``,
             ``ren. prev deal NNNN``                                                        0.95
  inferred   L1: an internal payoff on P whose successor is N (:mod:`payoffs`)              L1 confidence

An explicit link wins over a conflicting regex link (both are kept in the review queue). A tracker link
whose prior deal is unknown, later than N, or belongs to another resolved merchant is queued for review;
the first two are dropped, a cross-merchant link is kept at confidence 0.7.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

M_EXPLICIT = "explicit"
M_REGEX = "regex"
M_INFERRED = "inferred"

_LINK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("renewal_of", re.compile(r"\brenewal\s+of\s+#?\s*(\d{3,6})\b", re.I)),
    ("paid_off", re.compile(r"\bpaid\s*off\s+#?\s*(\d{3,6})\b", re.I)),
    ("refi", re.compile(r"\brefi(?:nance)?\s+(?:of\s+)?#?\s*(\d{3,6})\b", re.I)),
    ("ren_prev_deal", re.compile(r"\bren(?:ewal)?\.?\s*prev(?:ious)?\.?\s+deal\s+#?\s*(\d{3,6})\b", re.I)),
]
_SAYS_RENEWAL = re.compile(r"\b(renewal|rnwl|refi|ren\.)", re.I)
_SAYS_NEW = re.compile(r"\bnew\b", re.I)


@dataclass(frozen=True)
class LinkNote:
    prior_deal_id: int | None
    pattern: str | None
    says_renewal: bool
    says_new: bool


def parse_link_note(text: str | None) -> LinkNote:
    """Extract an explicit prior-deal number from tracker notes (Q2 notes regex)."""
    t = text or ""
    for name, pat in _LINK_PATTERNS:
        m = pat.search(t)
        if m:
            return LinkNote(int(m.group(1)), name, True, False)
    return LinkNote(None, None, bool(_SAYS_RENEWAL.search(t)), bool(_SAYS_NEW.search(t)))


@dataclass(frozen=True)
class TrackerDeal:
    deal_id: int
    funding_date: date
    prior_deal_id: int | None  # explicit field
    notes: str | None


def tracker_links(
    deals: list[TrackerDeal],
    known: Mapping[int, date],
    merchant_of: Mapping[int, str],
) -> tuple[dict[int, dict], list[dict]]:
    """Explicit-field and notes-regex links, N -> link record. ``known``: every deal id -> funding date."""
    links: dict[int, dict] = {}
    exc: list[dict] = []
    for d in sorted(deals, key=lambda x: x.deal_id):
        note = parse_link_note(d.notes)
        cand: list[tuple[str, int, float, str]] = []
        if d.prior_deal_id is not None:
            cand.append((M_EXPLICIT, int(d.prior_deal_id), 1.0, "tracker prior-deal field"))
        if note.prior_deal_id is not None:
            cand.append((M_REGEX, note.prior_deal_id, 0.95, f"notes '{d.notes}' ({note.pattern})"))
        if not cand:
            continue
        if len(cand) == 2 and cand[0][1] != cand[1][1]:
            exc.append(
                _exc(d.deal_id, "explicit_vs_notes_conflict", f"field {cand[0][1]} vs notes {cand[1][1]}")
            )
        method, prior, conf, evidence = cand[0]
        if len(cand) == 2 and cand[0][1] == cand[1][1]:
            evidence += " + notes agree"
        if prior not in known:
            exc.append(
                _exc(d.deal_id, "prior_deal_unknown", f"prior {prior} ({method}) not in tracker or ACH")
            )
            continue
        if known[prior] > d.funding_date or prior == d.deal_id:
            exc.append(
                _exc(
                    d.deal_id,
                    "prior_deal_not_earlier",
                    f"prior {prior} funded {known[prior]} >= {d.funding_date}",
                )
            )
            continue
        if merchant_of.get(prior) != merchant_of.get(d.deal_id):
            conf = 0.7
            exc.append(
                _exc(
                    d.deal_id,
                    "link_crosses_merchants",
                    f"prior {prior} is {merchant_of.get(prior)} but deal is {merchant_of.get(d.deal_id)}",
                )
            )
        links[d.deal_id] = {
            "deal_id": d.deal_id,
            "prior_deal_id": prior,
            "method": method,
            "confidence": conf,
            "evidence": evidence,
        }
    return links, exc


def final_links(
    tracker: Mapping[int, dict],
    payoffs: list[dict],
    deals: list[TrackerDeal],
) -> tuple[list[dict], list[dict]]:
    """Tracker links + L1-inferred links for deals without one. One row per renewal deal N."""
    out = {n: dict(r) for n, r in tracker.items()}
    exc: list[dict] = []
    for p in payoffs:
        n = p.get("successor_deal_id")
        if p.get("payoff_class") != "internal_payoff" or n is None:
            continue
        if n in out:
            if out[n]["prior_deal_id"] != p["deal_id"]:
                exc.append(
                    _exc(
                        n,
                        "l1_successor_disagrees_with_tracker_link",
                        f"tracker says prior {out[n]['prior_deal_id']}, L1 payoff on {p['deal_id']} points here",
                    )
                )
            else:
                out[n]["evidence"] += f"; L1 payoff {p['payment_id']} ({p['method']})"
            continue
        out[n] = {
            "deal_id": n,
            "prior_deal_id": p["deal_id"],
            "method": M_INFERRED,
            "confidence": p["confidence"],
            "evidence": f"L1 internal payoff payment {p['payment_id']} on {p['txn_date']} via {p['method']}",
        }
    # one prior -> one renewal
    by_prior: dict[int, list[int]] = {}
    for n, r in out.items():
        by_prior.setdefault(r["prior_deal_id"], []).append(n)
    for prior, ns in by_prior.items():
        if len(ns) > 1:
            exc.append(
                _exc(min(ns), "prior_deal_has_several_renewals", f"prior {prior} <- deals {sorted(ns)}")
            )
    # notes say renewal but nothing links the deal
    for d in deals:
        if d.deal_id not in out and parse_link_note(d.notes).says_renewal:
            exc.append(_exc(d.deal_id, "notes_say_renewal_but_no_link", f"notes '{d.notes}'"))
    return [out[n] for n in sorted(out)], exc


def _exc(deal_id: int, reason: str, detail: str) -> dict:
    return {"rule_id": "Q2", "entity": "deal", "entity_id": str(deal_id), "reason": reason, "detail": detail}
