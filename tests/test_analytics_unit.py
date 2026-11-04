"""Part 5 unit tests on tiny hand-built fixtures: Wilson CI, the T1 horizon boundary and maturity flags,
segment attribute buckets, and every figure function end to end (pure metrics -> tables -> figure data
-> PNG + plotly JSON + CSV)."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import duckdb
import plotly.io as pio
import pytest

from mcarisk.analytics import data, figures
from mcarisk.analytics.stage import render_figure
from mcarisk.analytics.stats import wilson_ci
from mcarisk.calendar import add_business_days
from mcarisk.metrics.attributes import burden, burden_bucket, positions_bucket
from mcarisk.metrics.deal import Deal, Event, all_deal_metrics
from mcarisk.metrics.portfolio import portfolio_metrics
from mcarisk.metrics.relationship import all_relationship_metrics
from mcarisk.metrics.stage import DEAL_DDL, PORT_DDL, REL_DDL, TARGET_DDL, TMETRIC_DDL
from mcarisk.metrics.targets import is_mature, t1_deal_targets, t1_horizon_date, t1_rates
from mcarisk.resolve.stage import write_table

AS_OF = date(2026, 9, 30)


# ------------------------------------------------------------------------------------------ Wilson
@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (5, 10, 0.236593, 0.763407),  # textbook value
        (0, 10, 0.0, 0.277532),  # k = 0: lower bound exactly 0, upper = z^2 / (n + z^2)
        (10, 10, 0.722468, 1.0),  # k = n: upper bound exactly 1
        (1, 1, 0.206549, 1.0),  # k = n = 1: lower = 1 / (1 + z^2)
        (36, 89, 0.308534, 0.508358),  # P8 chains with a distressed refi
    ],
)
def test_wilson_known_values(k, n, lo, hi):
    a, b = wilson_ci(k, n)
    assert a == pytest.approx(lo, abs=1e-6)
    assert b == pytest.approx(hi, abs=1e-6)


def test_wilson_edge_cases():
    assert wilson_ci(0, 0) == (None, None)
    lo, hi = wilson_ci(0, 7)
    assert lo == 0.0 and hi < 1
    lo, hi = wilson_ci(7, 7)
    assert hi == 1.0 and lo > 0
    z2 = 1.959963984540054**2
    assert wilson_ci(0, 50)[1] == pytest.approx(z2 / (50 + z2))
    for bad in ((-1, 5), (6, 5), (0, -1)):
        with pytest.raises(ValueError):
            wilson_ci(*bad)
    # symmetric: the interval of k/n mirrors the interval of (n-k)/n
    lo, hi = wilson_ci(3, 20)
    lo2, hi2 = wilson_ci(17, 20)
    assert lo == pytest.approx(1 - hi2) and hi == pytest.approx(1 - lo2)


# ------------------------------------------------------------------------------------------ tiny book
def _day_before(d: date) -> date:
    return d - timedelta(days=1)


def _bdays(start: date, n: int) -> list[date]:
    return [add_business_days(start, i) for i in range(1, n + 1)]


class Book:
    """A tiny book: 4 merchants, 5 deals across 2024Q1, 2024Q2 and 2026Q3."""

    def __init__(self):
        self.deals: list[Deal] = []
        self.events: list[Event] = []
        self._id = 0

    def ev(self, deal_id, d, kind, amt, **kw):
        self._id += 1
        self.events.append(Event(self._id, deal_id, d, kind, amt, **kw))
        return self._id

    def deal(self, deal_id, mk, f, advance, rtr, pay, comm=0.1, fee=0):
        self.deals.append(Deal(deal_id, mk, f, advance, rtr, fee, comm, pay, "daily"))
        self.ev(deal_id, f, "funding_out", -(advance - fee))
        self.ev(deal_id, f, "commission_out", -round(advance * comm))


def tiny_book() -> Book:
    b = Book()
    # M1: deal 1 paid 60 days then renewed (healthy) into deal 2, which pays in full
    f1 = date(2024, 1, 3)
    b.deal(1, "MKA", f1, 800000, 1000000, 10000)
    days = _bdays(f1, 60)
    for d in days:
        b.ev(1, d, "debit", 10000)
    t = days[-1]
    b.deal(2, "MKA", t, 1000000, 1200000, 15000)
    b.ev(1, t, "internal_payoff", 400000, successor_deal_id=2)
    for d in _bdays(t, 80):
        b.ev(2, d, "debit", 15000)
    # M2: deal 3 pays 10 days then stops -> D16 default -> written off after 180 days
    f3 = date(2024, 4, 1)
    b.deal(3, "MKB", f3, 800000, 1000000, 10000)
    for d in _bdays(f3, 10):
        b.ev(3, d, "debit", 10000)
    # M3: deal 4 pays in full within 2024Q2
    f4 = date(2024, 5, 1)
    b.deal(4, "MKC", f4, 400000, 500000, 10000)
    for d in _bdays(f4, 50):
        b.ev(4, d, "debit", 10000)
    # M4: deal 5 funded recently: immature on T1
    f5 = date(2026, 8, 3)
    b.deal(5, "MKD", f5, 400000, 500000, 5000)
    for d in _bdays(f5, 20):
        b.ev(5, d, "debit", 5000)
    return b


def tiny_db(b: Book) -> duckdb.DuckDBPyConnection:
    dm = all_deal_metrics(b.deals, b.events, (), as_of=AS_OF)
    rm = all_relationship_metrics(dm)
    mk = {d.deal_id: d.merchant_key for d in b.deals}
    cash = [
        (mk[e.deal_id], e.event_date, e.kind, e.amount_cents)
        for e in b.events
        if e.kind not in ("internal_payoff", "modification", "write_off")
    ]
    pm = portfolio_metrics(dm, rm, cash, as_of=AS_OF)
    tg = t1_deal_targets(b.deals, b.events, (), as_of=AS_OF)
    attrs = {
        1: {"industry": "Retail", "iso": "ISO A", "positions": "0", "burden": "<15%"},
        2: {"industry": "Retail", "iso": "ISO A", "positions": "1", "burden": "15-20%"},
        3: {"industry": "Trucking", "iso": "ISO B", "positions": "2", "burden": "<15%"},
        4: {"industry": "Trucking", "iso": "ISO B", "positions": "0", "burden": "20-25%"},
    }
    tm = t1_rates(tg, attrs, as_of=AS_OF)
    con = duckdb.connect()
    write_table(con, "deal_metrics", dm, DEAL_DDL)
    write_table(con, "relationship_metrics", rm, REL_DDL)
    write_table(con, "portfolio_metrics", pm, PORT_DDL)
    write_table(con, "deal_targets", tg, TARGET_DDL)
    write_table(con, "target_metrics", tm, TMETRIC_DDL)
    return con


def test_tiny_book_outcomes_and_t1():
    b = tiny_book()
    dm = {d["deal_id"]: d for d in all_deal_metrics(b.deals, b.events, (), as_of=AS_OF)}
    assert dm[1]["outcome"] == "renewed_healthy"
    assert dm[2]["outcome"] == "paid_in_full"
    assert dm[3]["outcome"] == "written_off"
    assert dm[4]["outcome"] == "paid_in_full"
    tg = {r["deal_id"]: r for r in t1_deal_targets(b.deals, b.events, (), as_of=AS_OF)}
    assert [tg[i]["mature"] for i in (1, 2, 3, 4, 5)] == [True, True, True, True, False]
    # deal 3 defaults (D16) and is written off (default + 180 days) before its 180-bd horizon: T1-bad
    assert tg[3]["outcome_at_horizon"] == "written_off"
    assert tg[3]["t1_bad_economic"] and tg[3]["t1_bad_naive"]
    assert not tg[1]["t1_bad_economic"] and not tg[4]["t1_bad_economic"]
    assert tg[5]["t1_bad_economic"] is None and tg[5]["outcome_at_horizon"] is None
    assert tg[5]["horizon_date"] == add_business_days(date(2026, 8, 3), 180)


# ------------------------------------------------------------------------------------------ T1 boundary
def test_t1_horizon_boundary():
    f = date(2025, 12, 31)
    h = t1_horizon_date(f)
    assert h == add_business_days(f, 180)
    assert is_mature(f, h)  # horizon date == as-of: mature
    assert not is_mature(f, _day_before(h))  # one business day short: immature


def test_t1_targets_and_vintage_maturity_at_the_boundary():
    """Two deals in one quarter: the vintage is partial while only the earlier deal has reached the
    horizon, mature once both have, immature before either."""
    b = Book()
    fa, fb = date(2025, 1, 2), date(2025, 3, 31)
    b.deal(1, "MKA", fa, 400000, 500000, 10000)
    b.deal(2, "MKB", fb, 400000, 500000, 10000)
    for d in _bdays(fa, 50):
        b.ev(1, d, "debit", 10000)
    for d in _bdays(fb, 50):
        b.ev(2, d, "debit", 10000)
    ha, hb = t1_horizon_date(fa), t1_horizon_date(fb)

    def maturity(as_of):
        tg = t1_deal_targets(b.deals, b.events, (), as_of=as_of)
        rows = t1_rates(tg, as_of=as_of)
        v = {
            r["segment"]: r
            for r in rows
            if r["dimension"] == "vintage" and r["metric"] == "bad_rate_economic"
        }
        return v["2025Q1"], {r["deal_id"]: r["mature"] for r in tg}

    r, m = maturity(_day_before(ha))
    assert (
        r["maturity"] == "immature"
        and r["denominator"] == 0
        and r["value"] is None
        and m == {1: False, 2: False}
    )
    r, m = maturity(ha)
    assert (
        r["maturity"] == "partial"
        and r["denominator"] == 1
        and r["n_deals"] == 2
        and m == {1: True, 2: False}
    )
    r, m = maturity(_day_before(hb))
    assert r["maturity"] == "partial"
    r, m = maturity(hb)
    assert r["maturity"] == "mature" and r["denominator"] == 2 and r["value"] == 0.0


def test_t1_ignores_events_after_the_horizon():
    """A write-off logged after the horizon does not make the deal T1-bad; one before it does."""
    b = Book()
    f = date(2024, 2, 1)
    b.deal(1, "MKA", f, 800000, 1000000, 2000)  # still paying (and open) at the horizon
    for d in _bdays(f, 200):
        b.ev(1, d, "debit", 2000)
    h = t1_horizon_date(f)
    late = Book()
    late.deals, late.events = list(b.deals), list(b.events)
    late.events.append(Event(999, 1, add_business_days(h, 1), "write_off", 0))
    t_late = t1_deal_targets(late.deals, late.events, (), as_of=AS_OF)[0]
    assert t_late["t1_bad_economic"] is False and t_late["outcome_at_horizon"] == "active"
    early = Book()
    early.deals, early.events = list(b.deals), list(b.events)
    early.events.append(Event(999, 1, h, "write_off", 0))
    assert t1_deal_targets(early.deals, early.events, (), as_of=AS_OF)[0]["t1_bad_economic"] is True


def test_t1_rates_dimensions_and_unknown_segments():
    b = tiny_book()
    tg = t1_deal_targets(b.deals, b.events, (), as_of=AS_OF)
    rows = t1_rates(tg, {1: {"industry": "Retail"}}, as_of=AS_OF)
    dims = {r["dimension"] for r in rows}
    assert dims == {"all", "vintage", "industry", "iso", "positions", "burden"}
    ind = {
        r["segment"]: r for r in rows if r["dimension"] == "industry" and r["metric"] == "bad_rate_economic"
    }
    assert set(ind) == {"Retail", "unknown"}
    assert ind["unknown"]["n_deals"] == 4 and ind["unknown"]["denominator"] == 3
    al = {r["metric"]: r for r in rows if r["dimension"] == "all"}
    assert al["bad_rate_economic"]["numerator"] == 1 and al["bad_rate_economic"]["denominator"] == 4
    assert al["bad_rate_economic"]["maturity"] == "partial"


# ------------------------------------------------------------------------------------------ attributes
def test_burden_and_buckets_exact_at_edges():
    # monthly revenue 2,100,000 cents; daily payment 15,000 -> burden exactly 15%
    assert burden(15000, "daily", 2100000) == pytest.approx(0.15)
    assert burden_bucket(15000, "daily", 2100000) == "15-20%"
    assert burden_bucket(14999, "daily", 2100000) == "<15%"
    assert burden_bucket(75000, "weekly", 2100000) == "15-20%"  # weekly: payment / 5
    assert burden_bucket(35000, "daily", 2100000) == ">=35%"
    assert burden_bucket(34999, "daily", 2100000) == "25-35%"
    assert burden_bucket(None, "daily", 2100000) == "unknown"
    assert burden_bucket(15000, "daily", None) == "unknown"
    assert burden(15000, "daily", 0) is None
    assert [positions_bucket(p) for p in (None, 0, 1, 2, 3, 7)] == ["unknown", "0", "1", "2", "3+", "3+"]


# ------------------------------------------------------------------------------------------ figures
@pytest.fixture(scope="module")
def tiny_con():
    con = tiny_db(tiny_book())
    yield con
    con.close()


@pytest.mark.parametrize("name", list(figures.FIGURES))
def test_each_figure_runs_on_a_tiny_fixture(tiny_con, name, tmp_path):
    df = data.DATASETS[name](tiny_con)
    assert len(df) > 0, name
    kw = {"overall": 0.25} if name == "segments" else {}
    paths = render_figure(name, df, tmp_path, **kw)
    png = tmp_path / f"{name}.png"
    assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    fig = pio.from_json((tmp_path / f"{name}.plotly.json").read_text(encoding="utf-8"))
    assert len(fig.data) > 0
    assert any(mid in json.dumps(fig.layout.title.to_plotly_json()) for mid in figures.FIGURE_METRICS[name])
    assert (tmp_path / "data" / f"{name}.csv").exists()
    assert set(paths) == {"png", "plotly", "csv"}


def test_figure_data_carries_wilson_bounds(tiny_con):
    seg = data.segments(tiny_con)
    for r in seg.itertuples():
        lo, hi = wilson_ci(int(r.bad), int(r.mature_deals))
        assert (lo is None and math.isnan(float(r.t1_economic_ci_lo))) or float(r.t1_economic_ci_lo) == lo
        assert (hi is None and math.isnan(float(r.t1_economic_ci_hi))) or float(r.t1_economic_ci_hi) == hi
    bv = data.bad_rate_vintage(tiny_con).set_index("vintage")
    assert bv.loc["2026Q3", "maturity"] == "immature"
    assert bv.loc["2024Q1", "maturity"] == "mature"


def test_every_figure_cites_metric_ids():
    for name in figures.FIGURES:
        ids = figures.FIGURE_METRICS[name]
        assert ids and all(i[0] in "DPRT" for i in ids)


def test_wilson_matches_statsmodels_on_a_grid():
    from statsmodels.stats.proportion import proportion_confint

    for n in (1, 2, 7, 38, 89, 591):
        for k in sorted({0, 1, n // 3, n // 2, n - 1, n}):
            lo, hi = wilson_ci(k, n)
            elo, ehi = proportion_confint(k, n, method="wilson")
            assert lo == pytest.approx(elo, abs=1e-12) and hi == pytest.approx(ehi, abs=1e-12)
