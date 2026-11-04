"""Part 4 unit tests on hand-built fixtures: schedule, D16, D17, §2.1 precedence, order invariance,
relationship / portfolio arithmetic, and the METRICS.md §7 worked example (M0219)."""

from __future__ import annotations

import csv
import random
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcarisk.ledger import commission_cents, match_returns
from mcarisk.metrics.deal import Deal, Event, StatusEvent, deal_metrics, distressed_refinance
from mcarisk.metrics.portfolio import add_months, portfolio_metrics
from mcarisk.metrics.relationship import relationship_metrics
from mcarisk.metrics.schedule import cal_index, sched_code

CI = cal_index()
FIX = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 9, 30)


# ------------------------------------------------------------------------------------------ builders
class Book:
    """Builds a deal's ledger events with increasing event ids."""

    def __init__(self, deal: Deal):
        self.deal = deal
        self.events: list[Event] = []
        self.status: list[StatusEvent] = []
        self._id = 0

    def add(self, d: date, kind: str, amt: int, **kw) -> Event:
        self._id += 1
        e = Event(self._id, self.deal.deal_id, d, kind, amt, **kw)
        self.events.append(e)
        return e

    def fund(self, net: int) -> None:
        self.add(self.deal.funding_date, "funding_out", -net)

    def pay(self, days: list[date], amt: int | None = None, returned: set[int] | None = None) -> None:
        """Debits on ``days``; indices in ``returned`` get a matched return 2 business days later."""
        amt = amt or self.deal.payment_cents
        returned = returned or set()
        for i, d in enumerate(days):
            deb = self.add(d, "debit", amt)
            if i in returned:
                r = self.add(CI.add_bdays(d, 2), "return", -amt, matched_event_id=deb.event_id)
                self.events[self.events.index(deb)] = Event(
                    deb.event_id, deb.deal_id, deb.event_date, "debit", amt, matched_event_id=r.event_id
                )

    def sched(self, n: int, start: date | None = None) -> list[date]:
        code = sched_code(self.deal.frequency, self.deal.funding_date)
        d0 = start or self.deal.funding_date
        return [CI.nth_sched_after(code, d0, k) for k in range(1, n + 1)]

    def metrics(self, as_of: date = AS_OF, **kw):
        return deal_metrics(self.deal, self.events, self.status, as_of=as_of, **kw)


def daily(deal_id=1, rtr=100_000, pay=1_000, funding=date(2025, 1, 6), **kw) -> Deal:
    return Deal(deal_id, "MK1", funding, kw.pop("advance", 80_000), rtr, kw.pop("fee", 2_000),
                kw.pop("rate", 0.1), pay, "daily", **kw)  # fmt: skip


def weekly(deal_id=1, rtr=100_000, pay=5_000, funding=date(2025, 1, 6)) -> Deal:
    return Deal(deal_id, "MK1", funding, 80_000, rtr, 2_000, 0.1, pay, "weekly")


# ------------------------------------------------------------------------------------------ schedule
def test_daily_schedule_is_business_days():
    # Labor Day 2026-09-07: the daily schedule skips it
    assert CI.nth_sched_after("daily", date(2026, 9, 4), 1) == date(2026, 9, 8)
    assert CI.bdays_between(date(2026, 9, 4), date(2026, 9, 8)) == 1


def test_weekly_schedule_holiday_moves_back_within_week():
    # Juneteenth 2026 is Friday 06-19: a Friday schedule pays Thursday 06-18 that week
    assert CI.nth_sched_after("w4", date(2026, 6, 12), 1) == date(2026, 6, 18)
    # Labor Day 2026-09-07 on a Monday schedule: no earlier day that week -> next payday 09-14
    assert CI.nth_sched_after("w0", date(2026, 8, 31), 1) == date(2026, 9, 14)
    # the code is the weekday of the first business day after funding (Fri 2025-01-03 -> Mon)
    assert sched_code("weekly", date(2025, 1, 3)) == "w0"
    assert sched_code("daily", date(2025, 1, 3)) == "daily"


def test_commission_rounding_half_up():
    assert commission_cents(3_200_000, 0.107) == 342_400
    assert commission_cents(None, 0.1) is None
    assert commission_cents(1_005, 0.5) == 503  # 502.5 -> 503


def test_match_returns_prefers_two_business_day_lag():
    rows = [
        {"event_id": 1, "deal_id": 1, "kind": "debit", "amount_cents": 500, "event_date": date(2025, 3, 3), "matched_event_id": None},
        {"event_id": 2, "deal_id": 1, "kind": "debit", "amount_cents": 500, "event_date": date(2025, 3, 4), "matched_event_id": None},
        {"event_id": 3, "deal_id": 1, "kind": "return", "amount_cents": -500, "event_date": date(2025, 3, 5), "matched_event_id": None},
        {"event_id": 4, "deal_id": 1, "kind": "return", "amount_cents": -999, "event_date": date(2025, 3, 6), "matched_event_id": None},
    ]  # fmt: skip
    unmatched = match_returns(rows)
    assert rows[0]["matched_event_id"] == 3 and rows[2]["matched_event_id"] == 1  # 03-03 is 2 bd before 03-05
    assert rows[1]["matched_event_id"] is None
    assert unmatched == [4]


# ------------------------------------------------------------------------------------------ D16
def test_d16_daily_20_missed_days_fires_on_the_20th():
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(40)
    b.pay(days[:10])  # last good payment = days[9]
    b.pay(days[10:30], returned=set(range(20)))  # 20 returned attempts
    m = b.metrics(as_of=date(2025, 6, 30))
    assert m["d16_default"] and m["d16_trigger"] == "missed_payments"
    assert m["d16_recognition_date"] == CI.nth_sched_after("daily", days[9], 20)
    assert m["d16_default_date"] == CI.add_bdays(days[9], 1)
    assert m["outcome"] == "defaulted"
    # one day earlier the rule has not fired yet
    early = b.metrics(as_of=CI.nth_sched_after("daily", days[9], 19))
    assert not early["d16_default"] and early["outcome"] == "active"


def test_d16_19_missed_days_then_good_payment_does_not_fire():
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(40)
    b.pay(days[:10])
    b.pay(days[10:29], returned=set(range(19)))
    b.pay(days[29:31])  # the 20th scheduled day is good
    assert not b.metrics(as_of=CI.add_bdays(days[30], 5))["d16_default"]


def test_d16_weekly_deal_four_missed_payments():
    b = Book(weekly())
    b.fund(78_000)
    days = b.sched(12)
    b.pay(days[:5])
    b.pay(days[5:8], returned={0, 1, 2})  # 3 missed weekly payments: not yet
    m3 = b.metrics(as_of=CI.add_bdays(days[7], 2))
    assert not m3["d16_default"]
    b.pay(days[8:9], returned={0})  # the 4th
    m4 = b.metrics(as_of=date(2025, 12, 31))
    assert m4["d16_default"] and m4["d16_trigger"] == "missed_payments"
    assert m4["d16_recognition_date"] == days[8]
    assert m4["d16_default_date"] == CI.add_bdays(days[4], 1)


def test_d16_collections_log_default_before_the_20_day_rule():
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(30)
    b.pay(days[:10])
    log_day = CI.add_bdays(days[9], 5)  # 5 business days after the last good payment
    b.status.append(StatusEvent(1, 1, log_day, "default"))
    m = b.metrics(as_of=date(2025, 6, 30))
    assert m["d16_trigger"] == "collections_log"
    assert m["d16_recognition_date"] == log_day
    assert m["d16_default_date"] == CI.add_bdays(days[9], 1)
    # a bankruptcy entry fires D16 too
    b.status[0] = StatusEvent(1, 1, log_day, "bankruptcy")
    assert b.metrics(as_of=date(2025, 6, 30))["d16_trigger"] == "collections_log"


def test_d16_balance_zero_never_defaults():
    b = Book(daily(rtr=10_000, pay=1_000))
    b.fund(7_800)
    b.pay(b.sched(10))  # paid in full after 10 payments, then nothing for months
    m = b.metrics(as_of=date(2025, 12, 31))
    assert not m["d16_default"]
    assert m["outcome"] == "paid_in_full" and m["d7_cash_from_merchant_cents"] == 10_000
    assert m["outcome_date"] == b.sched(10)[-1]


def test_d16_not_after_internal_payoff():
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(30)
    b.pay(days[:10])
    b.add(days[10], "internal_payoff", 90_000, successor_deal_id=2)
    m = b.metrics(as_of=date(2025, 12, 31))
    assert not m["d16_default"] and m["outcome"] in ("renewed_healthy", "refinanced_distressed")


# ------------------------------------------------------------------------------------------ D17
def _renewal_book(pay_returned: set[int] = frozenset(), short: bool = False, mod: bool = False) -> Book:
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(40)
    amt = 800 if short else 1_000  # 80% of the scheduled payment -> pace 0.8 < 0.85
    b.pay(days[:40], amt=amt, returned=set(pay_returned))
    if mod:
        b.add(days[20], "modification", 0, new_payment_cents=900)
    b.add(CI.add_bdays(days[39], 3), "internal_payoff", 50_000, successor_deal_id=2)
    return b


SUCC = {2: daily(deal_id=2, pay=450)}


def test_d17_none_is_renewed_healthy():
    m = _renewal_book().metrics(successors=SUCC)
    assert m["d17_distressed"] is False and m["outcome"] == "renewed_healthy"
    assert m["d17_payment_ratio"] == pytest.approx(0.45)


def test_d17_return_trigger_alone():
    m = _renewal_book(pay_returned={25, 30, 35}).metrics(successors=SUCC)
    assert m["d17_d14_trigger"] and not m["d17_d13_trigger"] and not m["d17_mod_trigger"]
    assert m["d17_d14_returns"] == 3 and m["d17_d14_attempts"] == 20
    assert m["outcome"] == "refinanced_distressed"
    # 2 returns is not enough
    m2 = _renewal_book(pay_returned={30, 35}).metrics(successors=SUCC)
    assert not m2["d17_distressed"]


def test_d17_pace_trigger_alone():
    m = _renewal_book(short=True).metrics(successors=SUCC)
    assert m["d17_d13_trigger"] and not m["d17_d14_trigger"] and not m["d17_mod_trigger"]
    assert m["d17_d13_value"] < 0.85 and m["outcome"] == "refinanced_distressed"


def test_d17_modification_trigger_alone_and_payment_ratio_uses_modified_payment():
    m = _renewal_book(mod=True).metrics(successors=SUCC)
    assert m["d17_mod_trigger"] and not m["d17_d14_trigger"] and not m["d17_d13_trigger"]
    assert m["outcome"] == "refinanced_distressed"
    assert m["d17_payment_ratio"] == pytest.approx(450 / 900)


def test_d17_return_basis_asof_vs_attempt():
    """A return that posts after the renewal date is unknown on it (asof) but counted on attempt basis."""
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(40)
    b.pay(days[:40], returned={30, 35})
    deb = b.add(days[39] if False else CI.add_bdays(days[39], 1), "debit", 1_000)
    t = CI.add_bdays(deb.event_date, 1)
    b.add(t, "internal_payoff", 50_000, successor_deal_id=2)
    b.add(CI.add_bdays(deb.event_date, 2), "return", -1_000, matched_event_id=deb.event_id)
    asof = distressed_refinance(b.deal, sorted(b.events, key=lambda e: (e.event_date, e.event_id)), 100_000, t,
                                None, CI, basis="asof")  # fmt: skip
    att = distressed_refinance(b.deal, sorted(b.events, key=lambda e: (e.event_date, e.event_id)), 100_000, t,
                               None, CI, basis="attempt", as_of=AS_OF)  # fmt: skip
    assert asof["d14_returns"] == 2 and not asof["d14_trigger"]
    assert att["d14_returns"] == 3 and att["d14_trigger"]


# ------------------------------------------------------------------------------------------ precedence
def _defaulted_book() -> tuple[Book, list[date]]:
    b = Book(daily())
    b.fund(78_000)
    days = b.sched(20)
    b.pay(days[:10])
    b.status.append(StatusEvent(1, 1, CI.add_bdays(days[9], 15), "default"))
    return b, days


def test_precedence_write_off_event_beats_everything():
    b, _days = _defaulted_book()
    b.status.append(StatusEvent(2, 1, date(2025, 4, 1), "settlement", 20_000))
    b.add(date(2025, 5, 1), "settlement", 20_000)
    b.add(date(2025, 6, 1), "write_off", 70_000)
    assert b.metrics()["outcome"] == "written_off"


def test_precedence_settled_beats_defaulted_and_auto_write_off():
    b, _days = _defaulted_book()
    b.status.append(StatusEvent(2, 1, date(2025, 4, 1), "settlement", 30_000))
    for i, d in enumerate((date(2025, 5, 1), date(2025, 6, 2), date(2025, 7, 1))):
        b.add(d, "settlement", 10_000 - (1 if i == 2 else 0))  # rounding: 29,999 of 30,000 (3 instalments)
    m = b.metrics()
    assert m["outcome"] == "settled" and m["outcome_date"] == date(2025, 7, 1)
    assert m["d18_bad_naive"] and m["d19_bad_economic"]


def test_precedence_default_180_days_without_settlement_is_written_off():
    b, _days = _defaulted_book()
    m = b.metrics()
    dd = m["d16_default_date"]
    assert m["outcome"] == "written_off"
    assert (m["outcome_date"] - dd).days == 180
    # before 180 days it is defaulted; with an unfinished settlement past 180 days it is still written off
    before = b.metrics(as_of=date.fromordinal(dd.toordinal() + 179))
    assert before["outcome"] == "defaulted"
    b.status.append(StatusEvent(2, 1, date(2025, 4, 1), "settlement", 30_000))
    b.add(date(2025, 5, 1), "settlement", 10_000)
    assert b.metrics()["outcome"] == "written_off"


def test_precedence_defaulted_beats_refinance_and_open_states():
    b, days = _defaulted_book()
    b.add(CI.add_bdays(days[9], 30), "internal_payoff", 70_000, successor_deal_id=2)
    m = b.metrics(as_of=CI.add_bdays(days[9], 40))
    assert m["outcome"] == "defaulted"


def test_precedence_early_payoff_and_paid_in_full_and_modified():
    b = Book(daily(rtr=10_000, pay=1_000))
    b.fund(7_800)
    days = b.sched(10)
    b.pay(days[:7])
    b.add(days[7], "early_payoff_cash", 2_800)
    assert b.metrics()["outcome"] == "paid_in_full_early"
    # paid in full within one cent
    c = Book(daily(rtr=10_001, pay=1_000))
    c.fund(7_800)
    c.pay(c.sched(10))
    assert c.metrics()["outcome"] == "paid_in_full"
    d = Book(daily(rtr=10_002, pay=1_000))
    d.fund(7_800)
    d.pay(d.sched(10))
    d.add(d.sched(10)[-1], "modification", 0, new_payment_cents=2)
    m = d.metrics(as_of=CI.add_bdays(d.sched(10)[-1], 3))
    assert m["outcome"] == "active_modified" and m["remaining_rtr_cents"] == 2


def test_deal_funded_after_as_of_is_excluded():
    b = Book(daily(funding=date(2026, 10, 5)))
    assert b.metrics() is None


# ------------------------------------------------------------------------------------------ order invariance
@settings(max_examples=40, deadline=None)
@given(seed=st.integers(0, 10_000), n_ret=st.integers(0, 12), renew=st.booleans())
def test_metrics_invariant_to_event_order(seed, n_ret, renew):
    rng = random.Random(seed)
    b = Book(daily(rtr=60_000, pay=1_000))
    b.fund(45_000)
    days = b.sched(50)
    b.pay(days[: rng.randint(10, 50)], returned=set(rng.sample(range(10), min(n_ret, 10))))
    if renew:
        b.add(CI.add_bdays(b.events[-1].event_date, 3), "internal_payoff", 20_000, successor_deal_id=2)
    if rng.random() < 0.5:
        b.add(days[5], "modification", 0, new_payment_cents=700)
    ref = b.metrics(successors=SUCC)
    shuffled = b.events[:]
    rng.shuffle(shuffled)
    st_shuf = b.status[:]
    rng.shuffle(st_shuf)
    got = deal_metrics(b.deal, shuffled, st_shuf, as_of=AS_OF, successors=SUCC)
    assert got == ref
    rel = [ref, {**ref, "deal_id": 9, "funding_date": date(2024, 1, 2)}]
    assert relationship_metrics(rel) == relationship_metrics(list(reversed(rel)))


# ------------------------------------------------------------------------------------------ relationship / portfolio
def test_relationship_status_and_loss_arithmetic():
    base = {"merchant_key": "M", "as_of": AS_OF, "commission_known": True, "d17_distressed": None,
            "d9_reported_collected_cents": 0, "outcome_date": date(2025, 1, 1), "remaining_rtr_cents": 500}  # fmt: skip
    deals = [
        {**base, "deal_id": 1, "funding_date": date(2024, 1, 2), "d6_funder_cash_out_cents": 10_000,
         "d7_cash_from_merchant_cents": 4_000, "outcome": "refinanced_distressed", "d17_distressed": True},
        {**base, "deal_id": 2, "funding_date": date(2024, 6, 3), "d6_funder_cash_out_cents": 1_000,
         "d7_cash_from_merchant_cents": 5_000, "outcome": "written_off"},
    ]  # fmt: skip
    r = relationship_metrics(deals)
    assert r["r1_funder_cash_out_cents"] == 11_000 and r["r2_cash_from_merchant_cents"] == 9_000
    assert (
        r["r5_status"] == "resolved"
        and r["r8_realized_loss_cents"] == 2_000
        and r["r6_economic_exposure_cents"] is None
    )
    assert r["r9_relationship_bad"] and r["r10_n_distressed_refi"] == 1 and r["r10_days_to_resolution"] == 365
    deals[1]["outcome"] = "defaulted"
    r = relationship_metrics(deals)
    assert (
        r["r5_status"] == "open"
        and r["r6_economic_exposure_cents"] == 2_000
        and r["r7_contractual_balance_cents"] == 500
    )
    assert not r["r9_relationship_bad"]


def test_add_months_clamps_to_month_end():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 11, 15), 3) == date(2025, 2, 15)


# ------------------------------------------------------------------------------------------ M0219 (§7)
def _m0219_fixture() -> tuple[list[Deal], list[Event], list[StatusEvent]]:
    """Deals 1102 and 1150 with the contract terms printed in METRICS.md §7, and the ledger events the
    pipeline builds for them from raw/ (tests/fixtures/m0219_*.csv). Deal 1102 has no tracker row in
    raw/, so its advance, fee and commission % come from §7 here (fee = advance - net funding), and its
    ``commission_out`` event (absent from the pipeline ledger) is added."""

    def opt(v):
        return int(v) if v != "" else None

    with (FIX / "m0219_ledger.csv").open() as f:
        events = [
            Event(
                int(r["event_id"]),
                int(r["deal_id"]),
                date.fromisoformat(r["event_date"]),
                r["kind"],
                int(r["amount_cents"]),
                opt(r["matched_event_id"]),
                opt(r["new_payment_cents"]),
                opt(r["successor_deal_id"]),
            )
            for r in csv.DictReader(f)
        ]
    with (FIX / "m0219_status.csv").open() as f:
        status = [
            StatusEvent(
                int(r["status_id"]),
                int(r["deal_id"]),
                date.fromisoformat(r["event_date"]),
                r["event_type"],
                opt(r["agreed_amount_cents"]),
            )
            for r in csv.DictReader(f)
        ]
    d1102 = Deal(1102, "M0219", date(2023, 11, 30), 3_200_000, 4_096_000, 137_500, 0.107, 40_960, "daily")
    d1150 = Deal(1150, "M0219", date(2024, 2, 14), 2_600_000, 3_172_000, 52_500, 0.008, 18_126, "daily")
    events.append(Event(1, 1102, date(2023, 11, 30), "commission_out", -commission_cents(3_200_000, 0.107)))
    return [d1102, d1150], events, status


def assert_m0219_section7(m1102: dict, m1150: dict, rel: dict, *, check_commission: bool = True) -> None:
    """Every number of METRICS.md §7 (dollars as printed, rounded to the dollar / 0.1 pp / 0.01)."""

    def usd(c):
        return round(c / 100)

    # deal 1102
    assert m1102["funding_date"] == date(2023, 11, 30) and m1102["frequency"] == "daily"
    assert usd(m1102["d2_rtr_cents"]) == 40_960
    assert usd(m1102["d4_net_cash_to_merchant_cents"]) == 30_625
    assert usd(m1102["d7_cash_from_merchant_cents"]) == 18_022
    assert usd(m1102["d8_internal_payoff_out_cents"]) == 22_938
    assert round(m1102["d10_reported_collection_rate"], 3) == 1.000
    assert round(m1102["d11_adjusted_collection_rate"], 3) == 0.440
    assert m1102["outcome"] == "refinanced_distressed" and m1102["successor_deal_id"] == 1150
    # deal 1150
    assert m1150["funding_date"] == date(2024, 2, 14)
    assert (
        usd(m1150["d1_advance_cents"]) == 26_000
        and round(m1150["d2_rtr_cents"] / m1150["d1_advance_cents"], 2) == 1.22
    )
    assert usd(m1150["d2_rtr_cents"]) == 31_720
    assert usd(m1150["d4_net_cash_to_merchant_cents"]) == 2_537 and usd(m1150["d5_commission_cents"]) == 208
    assert usd(m1150["d6_funder_cash_out_cents"]) == 2_745
    assert usd(m1150["d7_cash_from_merchant_cents"]) == 8_700 and m1150["d8_internal_payoff_out_cents"] == 0
    assert (
        round(m1150["d10_reported_collection_rate"], 3)
        == 0.274
        == round(m1150["d11_adjusted_collection_rate"], 3)
    )
    assert m1150["outcome"] == "written_off" and usd(m1150["written_off_cents"]) == 23_020
    assert round(m1102["d17_payment_ratio"], 2) == 0.44  # $181.26 / $409.60
    # relationship
    assert usd(rel["r2_cash_from_merchant_cents"]) == 26_723
    assert rel["r5_status"] == "resolved" and rel["r9_relationship_bad"]
    if check_commission:
        assert (
            usd(m1102["d1_advance_cents"]) == 32_000
            and round(m1102["d2_rtr_cents"] / m1102["d1_advance_cents"], 2) == 1.28
        )
        assert usd(m1102["d5_commission_cents"]) == 3_424
        assert usd(m1102["d6_funder_cash_out_cents"]) == 34_049
        assert usd(rel["r1_funder_cash_out_cents"]) == 36_794
        assert round(rel["r3_cash_multiple"], 2) == 0.73
        assert usd(rel["r8_realized_loss_cents"]) == 10_072


def test_m0219_worked_example_hand_built_fixture():
    deals, events, status = _m0219_fixture()
    by = {d.deal_id: d for d in deals}
    m = {
        d.deal_id: deal_metrics(
            d,
            [e for e in events if e.deal_id == d.deal_id],
            [s for s in status if s.deal_id == d.deal_id],
            successors=by,
        )
        for d in deals
    }
    rel = relationship_metrics(m.values())
    assert_m0219_section7(m[1102], m[1150], rel)
    # §7 payment line: $409.60 daily / $181.26 daily, 175 days for 1150 (RTR / payment)
    assert by[1102].payment_cents == 40_960 and by[1150].payment_cents == 18_126
    assert round(by[1150].rtr_cents / by[1150].payment_cents) == 175
    # the ledger identity holds for both deals
    assert m[1102]["remaining_rtr_cents"] == 0
    assert m[1150]["remaining_rtr_cents"] == m[1150]["written_off_cents"]
    pm = portfolio_metrics(m.values(), [rel], [("M0219", e.event_date, e.kind, e.amount_cents) for e in events
                           if e.kind in ("funding_out", "commission_out", "debit", "return")])  # fmt: skip
    p5 = {r["metric"]: r for r in pm if r["metric_id"] == "P5"}
    assert p5["capital_weighted_loss_rate"]["numerator"] == rel["r8_realized_loss_cents"]
