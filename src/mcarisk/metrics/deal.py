"""Deal-level metrics D1-D19 and the §2.1 outcome (METRICS.md §2). Pure functions.

Inputs are plain dataclasses built from the ``ledger``, ``deal_status_events`` and ``deals_resolved``
tables; nothing here touches the database. Every metric is evaluated **as of** a date (default
2026-09-30): events dated after it are ignored, and a debit counts as *returned* only if its return is
dated on or before the evaluation date. Business days use the Fed ACH calendar
(``mcarisk.metrics.schedule``). The SQL mirror is ``sql/metrics/01_deal_metrics.sql``.

Conventions this module fixes where METRICS.md is silent (documented in PROGRESS.md, Part 4):

* **Good payment** (D15, D16): a ``debit`` not reversed by a ``return`` dated on or before the
  evaluation date. Returns are paired with debits in the ledger (``matched_event_id``).
* **Daily equivalent** (§0, D12): ``payment`` (daily) or ``payment / 5`` (weekly), kept exact; D12 is
  rounded half-up to the cent once, after multiplying by the business-day count.
* **Close date** (D12 "or close"): the outcome date of a closed outcome (below).
* **Outcome dates**: written_off = the earlier of the write-off log date and default date + 180 days;
  settled = the instalment that completed the agreed amount; renewed / refinanced = the internal-payoff
  date; paid_in_full_early = the cash-payoff date; paid_in_full = the last good debit; defaulted = the
  D16 recognition date; open outcomes have none.
* **D16 "balance above 0"** uses the §2.1 paid_in_full tolerance: a balance of at most $0.01 counts as
  zero, so a deal that is paid in full under §2.1 can never also default on a 1-cent residual.
* **Settlement complete** (§2.1 ``settled``): instalments received >= agreed amount - 1 cent per
  instalment received (each instalment is the agreed amount / n rounded to the cent).
* **Tracker-missing deals** (Q5): advance, fee, commission % and RTR are not in the data. D1, D3 and D5
  are NULL (``commission_known`` false) and D6 = D4 + 0 is a lower bound. D2 (RTR) is *inferred* from
  the ledger identity when it can be: a deal closed by an internal payoff has RTR = net debits +
  internal payoff (``rtr_source = inferred_internal_payoff``); a deal whose debits stopped (no attempt
  for the D16 window of 20 / 4 scheduled days before the as-of date) after a good last debit, with
  D14 < 3/20 and no collections-log entry, is paid in full with RTR = CFM (``inferred_paid_in_full``). Otherwise D2 is NULL, D12 is uncapped, and
  D16's "balance above 0" is taken as true (the balance is unknown, not zero).
* The schedule for D12 / D16 uses the tracker payment and frequency, or the ACH modal debit and
  ``DAILY/WEEKLY REMIT`` for tracker-missing deals.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

from mcarisk.metrics.schedule import CalIndex, cal_index, sched_code

AS_OF_DEFAULT = date(2026, 9, 30)

CASH_IN_KINDS = ("debit", "return", "early_payoff_cash", "settlement")
CLOSED_OUTCOMES = (
    "written_off",
    "settled",
    "refinanced_distressed",
    "renewed_healthy",
    "paid_in_full_early",
    "paid_in_full",
)
OPEN_OUTCOMES = ("defaulted", "active_modified", "active")
OUTCOMES = (
    "written_off",
    "settled",
    "defaulted",
    "refinanced_distressed",
    "renewed_healthy",
    "paid_in_full_early",
    "paid_in_full",
    "active_modified",
    "active",
)
D16_DAILY_DAYS = 20  # D16(b): consecutive scheduled payment days without a good payment (daily deals)
D16_WEEKLY_DAYS = 4  # ... and for weekly deals
WRITE_OFF_DAYS = 180  # §2.1 written_off: default + 180 calendar days
D17_RETURNS_NUM, D17_RETURNS_DEN = 3, 20  # D14 >= 3/20
D17_PACE = Fraction(85, 100)  # D13 < 0.85
PIF_TOLERANCE_CENTS = 1  # §2.1 paid_in_full: CFM >= RTR - $0.01

# D17 return basis (METRICS.md is ambiguous; see PROGRESS.md Part 4 open questions):
#   asof    (default, literal): only returns posted on or before the renewal date are known then.
#   attempt : a debit attempted on or before the renewal date counts as returned if its return posts
#             later (normally 2 business days), up to the metric as-of date.
D17_BASES = ("asof", "attempt")
D17_BASIS_ENV = "MCARISK_D17_RETURNS"


def d17_basis_from_env() -> str:
    import os

    b = os.environ.get(D17_BASIS_ENV, "asof").strip().lower() or "asof"
    if b not in D17_BASES:
        raise ValueError(f"{D17_BASIS_ENV} must be one of {D17_BASES}, got {b!r}")
    return b


@dataclass(frozen=True)
class Deal:
    deal_id: int
    merchant_key: str
    funding_date: date
    advance_cents: int | None
    rtr_cents: int | None  # tracker RTR (None for tracker-missing deals)
    fee_cents: int | None
    commission_rate: float | None
    payment_cents: int | None  # installment as stated (weekly amount for weekly deals)
    frequency: str | None  # daily | weekly
    tracker_missing: bool = False


@dataclass(frozen=True)
class Event:
    event_id: int
    deal_id: int
    event_date: date
    kind: str
    amount_cents: int
    matched_event_id: int | None = None
    new_payment_cents: int | None = None
    successor_deal_id: int | None = None


@dataclass(frozen=True)
class StatusEvent:
    status_id: int
    deal_id: int
    event_date: date
    event_type: str  # default | bankruptcy | settlement
    agreed_amount_cents: int | None = None


# ------------------------------------------------------------------------------------------ helpers
def daily_equivalent(payment_cents: int | None, frequency: str | None) -> Fraction | None:
    """§0 daily equivalent: payment (daily) or payment / 5 (weekly), exact."""
    if payment_cents is None:
        return None
    return Fraction(payment_cents, 5) if frequency == "weekly" else Fraction(payment_cents)


def round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def vintage(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


class _View:
    """The deal's events as known on date ``t``: events dated <= t. With ``returns_until`` (D17
    ``attempt`` basis) the returns of debits attempted on or before ``t`` that post after ``t`` but on or
    before ``returns_until`` are included too."""

    def __init__(self, events: list[Event], t: date, returns_until: date | None = None):
        self.t = t
        self.events = [e for e in events if e.event_date <= t]
        if returns_until is not None and returns_until > t:
            ids = {e.event_id for e in self.events if e.kind == "debit"}
            self.events += [
                e
                for e in events
                if e.kind == "return" and t < e.event_date <= returns_until and e.matched_event_id in ids
            ]
        self.returned = {
            e.matched_event_id for e in self.events if e.kind == "return" and e.matched_event_id is not None
        }
        self.debits = [e for e in self.events if e.kind == "debit"]
        self.good = [e for e in self.debits if e.event_id not in self.returned]

    def cfm(self) -> int:
        return sum(e.amount_cents for e in self.events if e.kind in CASH_IN_KINDS)

    def sum_kind(self, kind: str) -> int:
        return sum(e.amount_cents for e in self.events if e.kind == kind)

    def first(self, kind: str) -> Event | None:
        xs = [e for e in self.events if e.kind == kind]
        return xs[0] if xs else None

    def last_good_on_or_before(self, d: date) -> date | None:
        xs = [e.event_date for e in self.good if e.event_date <= d]
        return max(xs) if xs else None

    def return_stats(self) -> tuple[int, int]:
        """D14 inputs: (returns among the last 20 debit attempts, number of those attempts)."""
        last = self.debits[-20:]
        return sum(e.event_id in self.returned for e in last), len(last)


def _sorted(events: Iterable[Event]) -> list[Event]:
    return sorted(events, key=lambda e: (e.event_date, e.event_id))


def effective_payment(deal: Deal, events: list[Event], t: date) -> tuple[int | None, str]:
    """Installment in force on ``t``: the latest modification on or before ``t``, else the original."""
    mods = [e for e in events if e.kind == "modification" and e.event_date <= t and e.new_payment_cents]
    if mods:
        return mods[-1].new_payment_cents, "modified"
    return deal.payment_cents, "original"


def expected_collected(deal: Deal, rtr: int | None, end: date, cal: CalIndex) -> int | None:
    """D12: min(RTR, business days from funding to ``end`` x original daily equivalent), half-up cents."""
    de = daily_equivalent(deal.payment_cents, deal.frequency)
    if de is None:
        return None
    n = max(0, cal.bdays_between(deal.funding_date, end))
    x = round_half_up(n * de)
    return x if rtr is None else min(rtr, x)


def _pace(cfm: int, d12: int | None) -> float | None:
    """D13 = D7 / D12 (None when nothing was expected yet)."""
    return None if not d12 else cfm / d12


def _return_rate(ret: int, att: int) -> float | None:
    """D14 = returns among the last 20 attempts / min(20, attempts)."""
    return None if att == 0 else ret / att


# ------------------------------------------------------------------------------------------ D16
def default_event(
    deal: Deal,
    events: list[Event],
    status: list[StatusEvent],
    rtr: int | None,
    as_of: date,
    cal: CalIndex,
    close_limit: date | None,
) -> dict[str, Any]:
    """D16 default event as of ``as_of``.

    Fires on the earlier of (a) a collections-log ``default`` / ``bankruptcy`` entry for the deal and
    (b) no good payment for 20 consecutive scheduled payment days (4 for weekly deals) while the balance
    is above 0. (b) is checked in every gap between good payments (the funding date anchors the first
    gap) and only before the deal closed by a payoff (``close_limit``). The *recognition* date is the
    day (a) or (b) fires; the *default date* is the last good payment on or before it plus one business
    day (the funding date stands in when there was no good payment).
    """
    v = _View(events, as_of)
    k = D16_WEEKLY_DAYS if deal.frequency == "weekly" else D16_DAILY_DAYS
    code = sched_code(deal.frequency, deal.funding_date)
    anchors = sorted({deal.funding_date, *(e.event_date for e in v.good)})
    rec_b = None
    for i, g in enumerate(anchors):
        r = cal.nth_sched_after(code, g, k)
        if r is None or r > as_of:
            break
        nxt = anchors[i + 1] if i + 1 < len(anchors) else None
        if nxt is not None and nxt <= r:
            continue
        if close_limit is not None and r >= close_limit:
            break
        if rtr is not None:
            bal = rtr - sum(e.amount_cents for e in v.events if e.kind in CASH_IN_KINDS and e.event_date <= r)
            if bal <= PIF_TOLERANCE_CENTS:  # same $0.01 tolerance as §2.1 paid_in_full
                continue
        rec_b = r
        break
    log = [
        s.event_date for s in status if s.event_type in ("default", "bankruptcy") and s.event_date <= as_of
    ]
    rec_a = min(log) if log else None
    if rec_a is None and rec_b is None:
        return {"fired": False, "default_date": None, "recognition_date": None, "trigger": None}
    if rec_b is None or (rec_a is not None and rec_a <= rec_b):
        rec, trig = rec_a, "collections_log"
    else:
        rec, trig = rec_b, "missed_payments"
    lg = v.last_good_on_or_before(rec) or deal.funding_date
    return {"fired": True, "default_date": cal.add_bdays(lg, 1), "recognition_date": rec, "trigger": trig}


# ------------------------------------------------------------------------------------------ D17
def distressed_refinance(
    deal: Deal,
    events: list[Event],
    rtr: int | None,
    renewal_date: date,
    successor: Deal | None,
    cal: CalIndex,
    *,
    basis: str = "asof",
    as_of: date = AS_OF_DEFAULT,
) -> dict[str, Any]:
    """D17 (v1): the renewal of this (prior) deal on ``renewal_date`` is distressed when, as of that
    date, D14 >= 3/20, or D13 < 0.85, or the deal has a modification event.

    Severity attribute (not part of the test): payment ratio = the successor's daily equivalent / this
    deal's effective daily equivalent on the renewal date (after any modification).

    ``basis`` selects which returns are known on the renewal date (``D17_BASES``)."""
    v = _View(events, renewal_date, as_of if basis == "attempt" else None)
    ret, att = v.return_stats()
    d14 = _return_rate(ret, att)
    trig14 = att > 0 and ret * D17_RETURNS_DEN >= D17_RETURNS_NUM * att
    d12 = expected_collected(deal, rtr, renewal_date, cal)
    cfm = v.cfm()
    d13 = _pace(cfm, d12)
    trig13 = bool(d12) and Fraction(cfm, d12) < D17_PACE
    trigm = any(e.kind == "modification" for e in v.events)
    pay, _ = effective_payment(deal, v.events, renewal_date)
    prior_de = daily_equivalent(pay, deal.frequency)
    new_de = daily_equivalent(successor.payment_cents, successor.frequency) if successor else None
    ratio = float(new_de / prior_de) if (prior_de and new_de is not None) else None
    return {
        "distressed": bool(trig14 or trig13 or trigm),
        "d14_trigger": bool(trig14),
        "d13_trigger": bool(trig13),
        "mod_trigger": bool(trigm),
        "d14_value": d14,
        "d13_value": d13,
        "d14_returns": ret,
        "d14_attempts": att,
        "payment_ratio": ratio,
    }


# ------------------------------------------------------------------------------------------ RTR
def effective_rtr(
    deal: Deal, events: list[Event], status: list[StatusEvent], as_of: date, cal: CalIndex
) -> tuple[int | None, str | None]:
    """D2 RTR: the tracker's, or for a tracker-missing deal the ledger-identity inference (module doc)."""
    if deal.rtr_cents is not None:
        return deal.rtr_cents, "tracker"
    v = _View(events, as_of)
    if v.first("internal_payoff") is not None:
        return v.cfm() + v.sum_kind("internal_payoff"), "inferred_internal_payoff"
    if v.first("early_payoff_cash") is not None or not v.debits:
        return None, None
    if any(s.event_date <= as_of for s in status):  # default / bankruptcy / settlement on the log
        return None, None
    last = v.debits[-1]
    k = D16_WEEKLY_DAYS if deal.frequency == "weekly" else D16_DAILY_DAYS
    stop = cal.nth_sched_after(sched_code(deal.frequency, deal.funding_date), last.event_date, k)
    ret, att = v.return_stats()
    if (
        last.event_id not in v.returned
        and stop is not None
        and stop <= as_of
        and ret * D17_RETURNS_DEN < D17_RETURNS_NUM * att
    ):
        return v.cfm(), "inferred_paid_in_full"
    return None, None


# ------------------------------------------------------------------------------------------ main
def deal_metrics(
    deal: Deal,
    events: Iterable[Event],
    status: Iterable[StatusEvent] = (),
    *,
    as_of: date = AS_OF_DEFAULT,
    successors: dict[int, Deal] | None = None,
    cal: CalIndex | None = None,
    d17_basis: str = "asof",
) -> dict[str, Any] | None:
    """All deal metrics (D1-D19) and the §2.1 outcome for one deal as of ``as_of``.

    Returns None for a deal funded after ``as_of``. ``successors`` maps deal_id -> Deal for the payment
    ratio of D17 (the successor named on the internal-payoff event)."""
    if deal.funding_date > as_of:
        return None
    cal = cal or cal_index()
    evs = _sorted(e for e in events if e.event_date <= as_of)
    sts = sorted((s for s in status if s.event_date <= as_of), key=lambda s: (s.event_date, s.status_id))
    v = _View(evs, as_of)
    rtr, rtr_src = effective_rtr(deal, evs, sts, as_of, cal)

    d1 = deal.advance_cents  # D1 advance
    d3 = deal.fee_cents  # D3 fee
    d4 = -v.sum_kind("funding_out")  # D4 net cash to merchant = -funding_out
    comm_ev = [e for e in evs if e.kind == "commission_out"]
    commission_known = not deal.tracker_missing and deal.commission_rate is not None
    d5 = -sum(e.amount_cents for e in comm_ev) if commission_known else None  # D5 commission
    d6 = d4 + (d5 or 0)  # D6 funder cash out (a lower bound when D5 is unknown)
    d7 = v.cfm()  # D7 cash from merchant
    d8 = v.sum_kind("internal_payoff")  # D8 internal payoff out
    d9 = d7 + d8  # D9 reported collected
    d10 = d9 / rtr if rtr else None  # D10 reported collection rate
    d11 = d7 / rtr if rtr else None  # D11 renewal-adjusted collection rate

    ipo = v.first("internal_payoff")
    epo = v.first("early_payoff_cash")
    close_limit = min([e.event_date for e in (ipo, epo) if e is not None], default=None)

    d16 = default_event(deal, evs, sts, rtr, as_of, cal, close_limit)

    # settlement
    agreed = [s for s in sts if s.event_type == "settlement" and s.agreed_amount_cents is not None]
    agreed_amt = agreed[-1].agreed_amount_cents if agreed else None
    settle_done = None
    if agreed_amt is not None:
        run, n = 0, 0
        for e in evs:
            if e.kind == "settlement":
                run += e.amount_cents
                n += 1
                if run >= agreed_amt - n:
                    settle_done = e.event_date
                    break

    wo_ev = v.first("write_off")
    wo_dates = [wo_ev.event_date] if wo_ev else []
    if d16["fired"] and settle_done is None:
        auto = d16["default_date"] + timedelta(days=WRITE_OFF_DAYS)
        if auto <= as_of:
            wo_dates.append(auto)

    d17: dict[str, Any] | None = None
    if ipo is not None:
        succ = (successors or {}).get(ipo.successor_deal_id) if ipo.successor_deal_id else None
        d17 = distressed_refinance(deal, evs, rtr, ipo.event_date, succ, cal, basis=d17_basis, as_of=as_of)

    last_good = v.good[-1].event_date if v.good else None
    if wo_dates:
        outcome, odate = "written_off", min(wo_dates)
    elif settle_done is not None:
        outcome, odate = "settled", settle_done
    elif d16["fired"]:
        outcome, odate = "defaulted", d16["recognition_date"]
    elif ipo is not None:
        outcome = "refinanced_distressed" if d17 and d17["distressed"] else "renewed_healthy"
        odate = ipo.event_date
    elif epo is not None:
        outcome, odate = "paid_in_full_early", epo.event_date
    elif rtr is not None and d7 >= rtr - PIF_TOLERANCE_CENTS:
        outcome, odate = "paid_in_full", last_good
    elif any(e.kind == "modification" for e in evs):
        outcome, odate = "active_modified", None
    else:
        outcome, odate = "active", None
    is_closed = outcome in CLOSED_OUTCOMES

    end = odate if (is_closed and odate is not None) else as_of
    d12 = expected_collected(deal, rtr, end, cal)  # D12 expected collected to date
    d13 = _pace(d7, d12)  # D13 pace
    ret, att = v.return_stats()
    d14 = _return_rate(ret, att)  # D14 return rate (last 20)
    d15 = cal.bdays_between(last_good, as_of) if last_good else None  # D15 days since last good payment
    d18 = outcome in ("defaulted", "settled", "written_off")  # D18 deal bad (naive)
    d19 = d18 or outcome == "refinanced_distressed"  # D19 deal bad (economic)

    remaining = rtr - d7 - d8 if rtr is not None else None
    return {
        "deal_id": deal.deal_id,
        "merchant_key": deal.merchant_key,
        "as_of": as_of,
        "funding_date": deal.funding_date,
        "vintage": vintage(deal.funding_date),
        "frequency": deal.frequency,
        "tracker_missing": deal.tracker_missing,
        "commission_known": commission_known,
        "rtr_source": rtr_src,
        "d1_advance_cents": d1,
        "d2_rtr_cents": rtr,
        "d3_fee_cents": d3,
        "d4_net_cash_to_merchant_cents": d4,
        "d5_commission_cents": d5,
        "d6_funder_cash_out_cents": d6,
        "d7_cash_from_merchant_cents": d7,
        "d8_internal_payoff_out_cents": d8,
        "d9_reported_collected_cents": d9,
        "d10_reported_collection_rate": d10,
        "d11_adjusted_collection_rate": d11,
        "d12_expected_collected_cents": d12,
        "d13_pace": d13,
        "d14_return_rate_last20": d14,
        "d14_returns": ret,
        "d14_attempts": att,
        "d15_bdays_since_good_payment": d15,
        "last_good_payment_date": last_good,
        "d16_default": d16["fired"],
        "d16_default_date": d16["default_date"],
        "d16_recognition_date": d16["recognition_date"],
        "d16_trigger": d16["trigger"],
        "renewal_date": ipo.event_date if ipo else None,
        "successor_deal_id": ipo.successor_deal_id if ipo else None,
        "d17_distressed": d17["distressed"] if d17 else None,
        "d17_d14_trigger": d17["d14_trigger"] if d17 else None,
        "d17_d13_trigger": d17["d13_trigger"] if d17 else None,
        "d17_mod_trigger": d17["mod_trigger"] if d17 else None,
        "d17_d14_value": d17["d14_value"] if d17 else None,
        "d17_d14_returns": d17["d14_returns"] if d17 else None,
        "d17_d14_attempts": d17["d14_attempts"] if d17 else None,
        "d17_d13_value": d17["d13_value"] if d17 else None,
        "d17_payment_ratio": d17["payment_ratio"] if d17 else None,
        "outcome": outcome,
        "outcome_date": odate,
        "is_closed": is_closed,
        "d18_bad_naive": d18,
        "d19_bad_economic": d19,
        "settlement_agreed_cents": agreed_amt,
        "settlement_received_cents": v.sum_kind("settlement"),
        "written_off_cents": v.sum_kind("write_off"),
        "remaining_rtr_cents": remaining,
        "has_modification": any(e.kind == "modification" for e in evs),
    }


def all_deal_metrics(
    deals: Iterable[Deal],
    events: Iterable[Event],
    status: Iterable[StatusEvent] = (),
    *,
    as_of: date = AS_OF_DEFAULT,
    d17_basis: str = "asof",
) -> list[dict[str, Any]]:
    """``deal_metrics`` for every deal (deals funded after ``as_of`` are skipped)."""
    deals = list(deals)
    by_id = {d.deal_id: d for d in deals}
    ev: dict[int, list[Event]] = {}
    for e in events:
        ev.setdefault(e.deal_id, []).append(e)
    st: dict[int, list[StatusEvent]] = {}
    for s in status:
        st.setdefault(s.deal_id, []).append(s)
    cal = cal_index()
    out = []
    for d in sorted(deals, key=lambda x: x.deal_id):
        m = deal_metrics(
            d,
            ev.get(d.deal_id, ()),
            st.get(d.deal_id, ()),
            as_of=as_of,
            successors=by_id,
            cal=cal,
            d17_basis=d17_basis,
        )
        if m is not None:
            out.append(m)
    return out
