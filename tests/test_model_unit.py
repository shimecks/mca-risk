"""Part 6 unit tests on hand-built inputs: split boundaries, calibration slope, as-of-renewal prior-deal
features (no look-ahead), the static leakage guard, TIB harmonization, decile lift, T2 / T3 rows."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import statsmodels.api as sm
from hypothesis import given, settings
from hypothesis import strategies as st

from mcarisk.calendar import add_business_days
from mcarisk.metrics.deal import Deal, Event, distressed_refinance
from mcarisk.metrics.schedule import cal_index
from mcarisk.metrics.targets import t2_survival_rows, t3_relationship_rows
from mcarisk.model import features as F
from mcarisk.model import train as T
from mcarisk.model.evaluate import calibration_slope, decile_table, performance

CAL = cal_index()


# ------------------------------------------------------------------------------------------ split
@pytest.mark.parametrize(
    ("d", "split"),
    [
        (date(2023, 1, 3), "train"),
        (date(2024, 12, 31), "train"),
        (date(2025, 1, 1), "val"),
        (date(2025, 6, 30), "val"),
        (date(2025, 7, 1), "test"),
        (date(2026, 3, 31), "test"),
        (date(2026, 4, 1), "out_of_window"),
        (date(2026, 9, 30), "out_of_window"),
    ],
)
def test_split_boundaries(d, split):
    assert T.split_of(d) == split


def test_model_frame_exclusions():
    feats = [
        {"deal_id": 1, "funding_date": date(2024, 5, 1), "tracker_missing": False, "advance_cents": 100},
        {"deal_id": 2, "funding_date": date(2026, 1, 20), "tracker_missing": False, "advance_cents": 100},
        {"deal_id": 3, "funding_date": date(2025, 2, 1), "tracker_missing": True, "advance_cents": None},
        {"deal_id": 4, "funding_date": date(2026, 6, 1), "tracker_missing": False, "advance_cents": 100},
    ]
    tg = [
        {
            "deal_id": 1,
            "mature": True,
            "t1_bad_economic": True,
            "t1_bad_naive": False,
            "outcome_at_horizon": "x",
        },
        {
            "deal_id": 2,
            "mature": False,
            "t1_bad_economic": None,
            "t1_bad_naive": None,
            "outcome_at_horizon": None,
        },
        {
            "deal_id": 3,
            "mature": True,
            "t1_bad_economic": False,
            "t1_bad_naive": False,
            "outcome_at_horizon": "x",
        },
        {
            "deal_id": 4,
            "mature": False,
            "t1_bad_economic": None,
            "t1_bad_naive": None,
            "outcome_at_horizon": None,
        },
    ]
    f = T.model_frame(feats, tg).set_index("deal_id")
    assert f.loc[1, "excluded_reason"] is None and f.loc[1, "split"] == "train"
    assert f.loc[2, "excluded_reason"] == "immature" and f.loc[2, "split"] == "test"
    assert f.loc[3, "excluded_reason"] == "tracker_missing"
    assert f.loc[4, "excluded_reason"] == "out_of_window"


# ------------------------------------------------------------------------------------------ calibration
def test_calibration_slope_known_inputs():
    rng = np.random.default_rng(7)
    p = 1 / (1 + np.exp(-rng.normal(-1, 1.2, 200_000)))
    y = (rng.random(p.size) < p).astype(int)
    b, a = calibration_slope(y, p)
    assert b == pytest.approx(1.0, abs=0.03) and a == pytest.approx(0.0, abs=0.03)
    lg = np.log(p / (1 - p))
    b2, _ = calibration_slope(y, 1 / (1 + np.exp(-2 * lg)))  # predictions twice too extreme
    assert b2 == pytest.approx(0.5, abs=0.02)
    b3, _ = calibration_slope(y, 1 / (1 + np.exp(-0.5 * lg)))  # too timid
    assert b3 == pytest.approx(2.0, abs=0.06)


def test_calibration_slope_equals_statsmodels():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.05, 0.9, 500)
    y = (rng.random(500) < p**1.3).astype(int)
    b, a = calibration_slope(y, p)
    x = np.log(p / (1 - p))
    res = sm.Logit(y, sm.add_constant(x)).fit(disp=0)
    assert b == pytest.approx(res.params[1], rel=1e-8) and a == pytest.approx(res.params[0], rel=1e-8)


def test_decile_table_and_lift():
    df = pd.DataFrame({"deal_id": range(100), "y": [1] * 10 + [0] * 90, "score": np.linspace(1, 0, 100)})
    t = decile_table(df)
    assert list(t["n"]) == [10] * 10
    assert t.loc[0, "bad_rate"] == 1.0 and t.loc[0, "lift"] == pytest.approx(10.0)
    assert t["cum_capture"].iloc[-1] == 1.0
    perf = performance(df["y"], df["score"].clip(0.01, 0.99))
    assert perf["auc"] == 1.0 and perf["top_decile_lift"] == pytest.approx(10.0)
    assert np.isnan(perf["calibration_slope"])  # perfect separation: the recalibration MLE does not exist


def test_decile_ties_are_deterministic():
    df = pd.DataFrame({"deal_id": [5, 3, 9, 1], "y": [1, 0, 0, 1], "score": [0.5] * 4})
    a, b = decile_table(df, 2), decile_table(df.sample(frac=1, random_state=1), 2)
    pd.testing.assert_frame_equal(a, b)


# ------------------------------------------------------------------------------------------ prior features
def _prior_and_events(n_debits: int = 30):
    f0 = date(2024, 3, 1)
    prior = Deal(1, "MK", f0, 2_000_000, 2_600_000, 50_000, 0.08, 20_000, "daily")
    evs, eid, d = [], 1, f0
    for _ in range(n_debits):
        d = add_business_days(d, 1)
        evs.append(Event(eid, 1, d, "debit", 20_000))
        eid += 1
    return prior, evs, eid


def test_prior_features_ignore_returns_posted_after_funding():
    prior, evs, eid = _prior_and_events()
    t = evs[-1].event_date  # renewal funds on the day of the last debit
    new = Deal(2, "MK", t, 2_500_000, 3_200_000, 50_000, 0.08, 15_000, "daily")
    base = F.prior_features(new, prior, prior.rtr_cents, evs, CAL)
    # three of the last debits bounce, but the returns post 1-2 business days AFTER the funding date
    late = [
        Event(eid + i, 1, add_business_days(t, 2), "return", -20_000, matched_event_id=evs[-1 - i].event_id)
        for i in range(3)
    ]
    with_late = F.prior_features(new, prior, prior.rtr_cents, evs + late, CAL)
    assert with_late == base
    assert base["prior_return_rate_asof"] == 0.0 and base["prior_stress_rule_asof"] == 0
    # the D17 'attempt' basis (v1.2) would count them: that look-ahead is why D17 values are not features
    att = distressed_refinance(
        prior, evs + late, prior.rtr_cents, t, new, CAL, basis="attempt", as_of=date(2026, 9, 30)
    )
    assert att["d14_returns"] == 3 and att["distressed"]
    # the same returns posted ON the funding date do count
    on_time = [
        Event(e.event_id, 1, t, "return", e.amount_cents, matched_event_id=e.matched_event_id) for e in late
    ]
    hit = F.prior_features(new, prior, prior.rtr_cents, evs + on_time, CAL)
    assert hit["prior_return_rate_asof"] == pytest.approx(3 / 20) and hit["prior_stress_rule_asof"] == 1


def test_prior_features_pace_and_payment_ratio():
    prior, evs, _ = _prior_and_events(10)
    t = evs[-1].event_date
    new = Deal(2, "MK", t, 2_500_000, 3_200_000, 50_000, 0.08, 15_000, "daily")
    f = F.prior_features(new, prior, prior.rtr_cents, evs, CAL, prior_term_bdays=130)
    assert f["prior_pace_asof"] == pytest.approx(1.0)
    assert f["prior_payment_ratio"] == pytest.approx(0.75)
    assert f["prior_term_elapsed"] == pytest.approx(10 / 130)


later_event = st.builds(
    lambda k, kind, amt, lag: (k, kind, amt, lag),
    st.integers(0, 29),
    st.sampled_from(["debit", "return", "modification", "early_payoff_cash", "settlement"]),
    st.integers(-500_000, 500_000),
    st.integers(1, 40),
)


@settings(max_examples=60, deadline=None)
@given(st.lists(later_event, max_size=25))
def test_prior_features_invariant_to_any_post_funding_event(extra):
    """Leakage guard (pure): any events dated after the funding date leave the features unchanged."""
    prior, evs, eid = _prior_and_events()
    t = evs[19].event_date
    new = Deal(2, "MK", t, 2_500_000, 3_200_000, 50_000, 0.08, 15_000, "daily")
    base = F.prior_features(new, prior, prior.rtr_cents, evs, CAL)
    noise = [
        Event(
            eid + i,
            1,
            t + timedelta(days=lag),
            kind,
            amt,
            matched_event_id=evs[k].event_id,
            new_payment_cents=1,
        )
        for i, (k, kind, amt, lag) in enumerate(extra)
    ]
    assert F.prior_features(new, prior, prior.rtr_cents, evs + noise, CAL) == base


def test_compute_features_end_to_end_small():
    prior, evs, _ = _prior_and_events(25)
    t = evs[-1].event_date
    base = {
        "merchant_key": "MK",
        "tracker_missing": False,
        "advance_cents": 2_000_000,
        "factor": 1.3,
        "rtr_cents": 2_600_000,
        "fee_cents": 50_000,
        "commission_rate": 0.08,
        "term_bdays": 130,
        "frequency": "daily",
        "iso_name": "ISO",
        "link_method": None,
        "fico_tracker": None,
        "rev_tracker": None,
        "tib_months_tracker": None,
        "positions_tracker": None,
        "nsf_tracker": None,
        "industry_tracker": None,
        "state_tracker": None,
        "scorecard_app_date": prior.funding_date - timedelta(days=3),
        "fico_scorecard": 640,
        "rev_scorecard": 4_000_000,
        "tib_years_scorecard": 2.5,
        "positions_scorecard": 1,
        "nsf_scorecard": 0,
        "industry_scorecard": "Retail",
        "state_scorecard": "NY",
    }
    rows = [
        {
            **base,
            "deal_id": 1,
            "funding_date": prior.funding_date,
            "payment_cents": 20_000,
            "prior_deal_id": None,
        },
        {
            **base,
            "deal_id": 2,
            "funding_date": t,
            "payment_cents": 15_000,
            "prior_deal_id": 1,
            "fico_tracker": 700,
            "tib_months_tracker": 40,
        },
    ]
    out = {r["deal_id"]: r for r in F.compute_features(rows, evs, CAL)}
    assert out[1]["is_renewal"] == 0 and out[1]["chain_seq"] == 1 and out[1]["prior_pace_asof"] is None
    assert (
        out[1]["tib_months"] == 30.0 and out[1]["tib_source"] == "scorecard_years"
    )  # 2.5 years -> 30 months
    assert out[2]["fico"] == 700 and out[2]["tib_months"] == 40.0 and out[2]["tib_source"] == "tracker_months"
    assert out[2]["is_renewal"] == 1 and out[2]["chain_seq"] == 2
    assert out[1]["burden"] == pytest.approx(20_000 * 21 / 4_000_000)
    assert out[2]["prior_payment_ratio"] == pytest.approx(0.75)


# ------------------------------------------------------------------------------------------ static guard
def test_feature_inputs_never_name_forbidden_columns():
    for sql in (F.FEATURE_INPUT_SQL, F.PRIOR_EVENT_SQL):
        low = sql.lower()
        for bad in F.FORBIDDEN_INPUTS:
            assert bad not in low, f"forbidden input {bad!r} in feature SQL"
    assert "select *" not in F.FEATURE_INPUT_SQL.lower().replace("\n", " ")


def test_model_feature_lists_are_origination_only():
    names = [*F.NUMERIC_FEATURES, *F.CATEGORICAL_FEATURES, *T.LOGIT_FEATURES, *T.GBM_NUMERIC]
    for n in names:
        for bad in ("status", "outcome", "d16", "d17", "d18", "d19", "t1_", "payoff", "write", "default"):
            assert bad not in n.lower(), n


# ------------------------------------------------------------------------------------------ T2 / T3
def _dm(i, fd, outcome, od=None, d16=False, d16d=None, bad=False):
    return {
        "deal_id": i,
        "funding_date": fd,
        "outcome": outcome,
        "outcome_date": od,
        "d16_default": d16,
        "d16_default_date": d16d,
        "d19_bad_economic": bad,
    }


def test_t2_event_and_censoring():
    fd = date(2025, 1, 2)
    rows = t2_survival_rows(
        [
            _dm(1, fd, "written_off", date(2025, 9, 1), True, date(2025, 3, 3), True),
            _dm(2, fd, "refinanced_distressed", date(2025, 4, 1), bad=True),
            _dm(3, fd, "paid_in_full", date(2025, 6, 2)),
            _dm(4, fd, "active"),
        ],
        as_of=date(2026, 9, 30),
    )
    r = {x["deal_id"]: x for x in rows}
    assert r[1]["event"] and r[1]["end_date"] == date(2025, 3, 3)  # D16 default date, not the write-off
    assert r[2]["event"] and r[2]["end_date"] == date(2025, 4, 1)
    assert not r[3]["event"] and r[3]["end_date"] == date(2025, 6, 2)
    assert not r[4]["event"] and r[4]["end_date"] == date(2026, 9, 30)
    assert r[3]["duration_bdays"] > 0


def test_t3_population_window():
    def rel(k, first, status, days, bad):
        return {
            "merchant_key": k,
            "first_funding_date": first,
            "r5_status": status,
            "r10_days_to_resolution": days,
            "r9_relationship_bad": bad,
        }

    rows = {
        r["merchant_key"]: r
        for r in t3_relationship_rows(
            [
                rel("a", date(2024, 1, 15), "resolved", 300, True),
                rel("b", date(2024, 1, 15), "resolved", 600, False),  # resolved after 18 months
                rel("c", date(2024, 1, 15), "open", None, None),
                rel("d", date(2025, 6, 1), "resolved", 100, False),  # window not yet elapsed
            ],
            as_of=date(2026, 9, 30),
        )
    }
    assert rows["a"]["in_population"] and rows["a"]["t3_bad"] is True
    assert not rows["b"]["in_population"] and not rows["c"]["in_population"]
    assert not rows["d"]["observable"] and not rows["d"]["in_population"]
    assert rows["a"]["window_end"] == date(2025, 7, 15)
