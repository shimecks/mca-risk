"""Part 6 on the real build: leakage perturbation, determinism, split boundaries in the persisted tables,
as-of-renewal features, outputs for the Part 7 app, model card, manifest."""

from __future__ import annotations

import json
import shutil

import duckdb
import numpy as np
import pandas as pd
import pytest

from mcarisk.metrics.deal import Event
from mcarisk.metrics.schedule import cal_index
from mcarisk.model.features import _deal as feature_deal
from mcarisk.model.features import build_features, load_feature_inputs, prior_features
from mcarisk.model.figures import MODEL_DATASETS, MODEL_FIGURE_METRICS
from mcarisk.model.leakage import DROP_TABLES, feature_diff, perturb_post_funding
from mcarisk.model.train import TEST_END, TEST_START, TRAIN_END, VAL_END, VAL_START, fit_all, model_frame


@pytest.fixture(scope="module")
def con(model_build):
    c = duckdb.connect(str(model_build["db"]), read_only=True)
    yield c
    c.close()


def test_leakage_perturbation_leaves_features_unchanged(model_build, tmp_path):
    """Scramble every post-funding ledger row, add look-ahead returns / modifications, overwrite tracker
    status and payoff fields, delete collections servicing rows, drop every outcome / metric / target
    table: the features are identical."""
    db = tmp_path / "p.duckdb"
    shutil.copy(model_build["db"], db)
    c = duckdb.connect(str(db))
    try:
        before = build_features(c)
        info = perturb_post_funding(c)
        tables = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert not (set(DROP_TABLES) & tables)
        assert c.execute("SELECT COUNT(*) FROM deal_status_events").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(DISTINCT status_norm) FROM deals_resolved").fetchone()[0] == 1
        after = build_features(c)
        assert info["post_funding_ledger_rows"] > 50_000 and info["fake_events"] > 50_000
        assert feature_diff(before, after) == []
        # control: a change on or before the cutoff IS seen (the check is not vacuous)
        c.execute("UPDATE ledger SET amount_cents = amount_cents * 2 WHERE kind = 'debit'")
        assert len(feature_diff(before, build_features(c))) > 100
    finally:
        c.close()


def test_features_match_stored_table(con):
    cols = ["deal_id", "prior_pace_asof", "burden", "tib_months"]
    stored = con.execute(f"SELECT {', '.join(cols)} FROM model_features ORDER BY deal_id").df()
    fresh = pd.DataFrame(build_features(con))[cols]
    pd.testing.assert_frame_equal(
        stored.reset_index(drop=True), fresh.reset_index(drop=True), check_dtype=False
    )


def test_as_of_renewal_features_ignore_later_returns(con):
    """For every renewal, the prior-deal features equal those computed from events cut at the funding
    date, and real renewals do have returns of pre-renewal debits that post later (so the cut matters)."""
    rows, events = load_feature_inputs(con)
    by_id = {r["deal_id"]: r for r in rows}
    ev: dict[int, list[Event]] = {}
    for e in events:
        ev.setdefault(e.deal_id, []).append(e)
    feats = {f["deal_id"]: f for f in build_features(con)}
    cal = cal_index()
    n_lookahead = 0
    renewals = [r for r in rows if r["prior_deal_id"] is not None and r["prior_deal_id"] in by_id]
    assert len(renewals) > 700
    for r in renewals:
        t = r["funding_date"]
        p = by_id[r["prior_deal_id"]]
        cut = [e for e in ev.get(p["deal_id"], []) if e.event_date <= t]
        f = prior_features(
            feature_deal(r), feature_deal(p), p["rtr_cents"], cut, cal, prior_term_bdays=p["term_bdays"]
        )
        for k, v in f.items():
            assert feats[r["deal_id"]][k] == v, (r["deal_id"], k)
        debit_ids = {e.event_id for e in cut if e.kind == "debit"}
        late = [e for e in ev.get(p["deal_id"], []) if e.kind == "return" and e.event_date > t]
        n_lookahead += any(e.matched_event_id in debit_ids for e in late)
    assert n_lookahead > 0


def test_split_boundaries_in_tables(con):
    p = con.execute(
        "SELECT funding_date, split, excluded_reason, mature FROM model_predictions WHERE model = 'logit'"
    ).df()
    p["funding_date"] = pd.to_datetime(p["funding_date"]).dt.date
    assert p[p.split == "train"].funding_date.max() <= TRAIN_END
    v = p[p.split == "val"]
    assert v.funding_date.min() >= VAL_START and v.funding_date.max() <= VAL_END
    t = p[p.split == "test"]
    assert t.funding_date.min() >= TEST_START and t.funding_date.max() <= TEST_END
    assert p[p.excluded_reason.isna()].mature.all()  # every labelled deal is mature
    assert (t.excluded_reason == "immature").sum() > 0  # immature test deals are excluded and counted
    assert set(p.excluded_reason.dropna()) <= {"immature", "out_of_window"}
    n_tm = con.execute(
        "SELECT COUNT(*) FROM model_features WHERE excluded_reason = 'tracker_missing'"
    ).fetchone()[0]
    assert n_tm == 44
    scored_tm = con.execute(
        "SELECT COUNT(*) FROM model_predictions m JOIN model_features f USING (deal_id) WHERE f.tracker_missing"
    ).fetchone()[0]
    assert scored_tm == 0


def test_deterministic_predictions(con):
    tg = con.execute(
        "SELECT deal_id, mature, t1_bad_economic, t1_bad_naive, outcome_at_horizon FROM deal_targets"
    ).df()
    a = fit_all(model_frame(build_features(con), tg.to_dict("records")))["predictions"]
    b = fit_all(model_frame(build_features(con), tg.to_dict("records")))["predictions"]
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    stored = con.execute("SELECT deal_id, model, score FROM model_predictions ORDER BY model, deal_id").df()
    mine = a[["deal_id", "model", "score"]].sort_values(["model", "deal_id"]).reset_index(drop=True)
    np.testing.assert_array_equal(stored["score"].to_numpy(), mine["score"].to_numpy())


def test_performance_tables(con):
    perf = con.execute("SELECT * FROM model_performance").df()
    assert set(perf.model) == {
        "baseline_rule",
        "logit",
        "logit_uncalibrated",
        "gbm",
        "gbm_isotonic",
        "logit_naive",
    }
    for metric in ("auc", "brier", "calibration_slope", "top_decile_lift"):
        assert not perf[(perf.split == "test") & (perf.metric == metric)].empty
    lift = con.execute(
        "SELECT * FROM model_lift WHERE split = 'test' AND model = 'logit' ORDER BY decile"
    ).df()
    assert list(lift.decile) == list(range(1, 11)) and lift.cum_capture.iloc[-1] == pytest.approx(1.0)
    coefs = con.execute("SELECT * FROM model_coefficients WHERE model = 'logit'").df()
    assert {"odds_ratio", "or_ci_low", "or_ci_high", "p_value"} <= set(coefs.columns)
    assert (coefs.or_ci_low <= coefs.odds_ratio).all() and (coefs.odds_ratio <= coefs.or_ci_high).all()
    nv = con.execute("SELECT DISTINCT model FROM model_naive_comparison").df()
    assert set(nv.model) == {"logit", "logit_naive"}
    km = con.execute("SELECT * FROM survival_km").df()
    assert set(km.dimension) >= {"renewal_status", "positions"}
    assert ((km.survival >= 0) & (km.survival <= 1)).all()
    assert con.execute("SELECT COUNT(*) FROM survival_cox").fetchone()[0] > 0
    t3 = dict(con.execute("SELECT key, value FROM t3_summary").fetchall())
    assert json.loads(t3["n_population"]) > 100


def test_figures_and_card(model_build, con):
    d = model_build["dir"] / "figures"
    for name in MODEL_FIGURE_METRICS:
        assert (d / f"{name}.png").stat().st_size > 1000
        js = json.loads((d / f"{name}.plotly.json").read_text(encoding="utf-8"))
        assert any(m in js["layout"]["title"]["text"] for m in MODEL_FIGURE_METRICS[name])
        assert (d / "data" / f"{name}.csv").exists()
        assert not MODEL_DATASETS[name](con).empty
    card = (model_build["dir"] / "model_card.md").read_text(encoding="utf-8")
    for h in (
        "## Intended use",
        "## What NOT to use it for",
        "## Data",
        "## Target choice",
        "## Performance",
        "## Calibration",
        "## Fairness considerations",
        "## Limits",
    ):
        assert h in card
    assert "tracker-missing" in card.lower() and "no protected attributes" in card.lower()


def test_manifest_stage(model_build):
    m = json.loads(model_build["manifest"].read_text(encoding="utf-8"))
    st = m["stages"]["model"]
    assert st["status"] == "ok" and st["tables"]["model_predictions"]["accepted"] > 0
    assert st["extra"]["headline"]["primary_model"] == "logit"


def test_logit_recalibrated_on_validation_only(con):
    """The primary logit = validation-fitted logistic recalibration of logit_uncalibrated (monotone)."""
    from mcarisk.model.evaluate import calibration_slope, logit

    p = con.execute(
        "SELECT deal_id, model, split, score, t1_bad_economic FROM model_predictions "
        "WHERE model IN ('logit', 'logit_uncalibrated') ORDER BY deal_id"
    ).df()
    raw = p[p.model == "logit_uncalibrated"].set_index("deal_id")
    cal = p[p.model == "logit"].set_index("deal_id")
    v = raw[(raw.split == "val") & raw.t1_bad_economic.notna()]
    b, a = calibration_slope(v.t1_bad_economic.astype(int), v.score)
    expect = 1 / (1 + np.exp(-(a + b * logit(raw.score.to_numpy()))))
    np.testing.assert_allclose(cal.loc[raw.index, "score"].to_numpy(), expect, rtol=1e-9)
    vc = cal.loc[v.index]
    assert calibration_slope(vc.t1_bad_economic.astype(int), vc.score)[0] == pytest.approx(1.0, abs=1e-6)
