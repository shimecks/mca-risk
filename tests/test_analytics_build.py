"""Part 5 on the real build: T1 / attributes Python == SQL, the T1 maturity flags at the horizon boundary,
the analytics stage outputs, and every findings.md number equal to the metric-table query behind it
(including detection of a tampered table and of an unregistered number)."""

from __future__ import annotations

import json
import random
import shutil
from datetime import date, timedelta

import duckdb
import pandas as pd
import plotly.io as pio
import pytest

from mcarisk.analytics import figures
from mcarisk.analytics.findings import check_findings, fmt_value
from mcarisk.analytics.stage import run_analytics
from mcarisk.calendar import add_business_days
from mcarisk.metrics.deal import AS_OF_DEFAULT, deal_metrics
from mcarisk.metrics.schedule import cal_index
from mcarisk.metrics.stage import compute_targets, install_sql, load_model
from mcarisk.metrics.targets import T1_HORIZON_BDAYS
from tests.test_metrics_build import assert_frames_equal


@pytest.fixture(scope="module")
def analytics_build(metrics_build, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("analytics")
    db = tmp / "portfolio.duckdb"
    shutil.copy(metrics_build["db"], db)
    res = run_analytics(
        db,
        fig_dir=tmp / "figures",
        findings_md=tmp / "findings.md",
        findings_json=tmp / "findings_numbers.json",
    )
    return {"db": db, "dir": tmp, "result": res}


@pytest.fixture(scope="module")
def con(metrics_build):
    c = duckdb.connect(str(metrics_build["db"]), read_only=True)
    yield c
    c.close()


def df(con, sql):
    return con.execute(sql).df()


# ------------------------------------------------------------------------------------------ Python == SQL
@pytest.mark.parametrize(
    ("table", "view", "key"),
    [
        ("deal_attributes", "v_deal_attributes", ["deal_id"]),
        ("deal_targets", "v_deal_targets", ["deal_id"]),
        ("target_metrics", "v_target_metrics", ["metric", "dimension", "segment"]),
    ],
)
def test_t1_python_equals_sql_on_the_build(con, table, view, key):
    assert_frames_equal(df(con, f"SELECT * FROM {table}"), df(con, f"SELECT * FROM {view}"), key)


def test_t1_python_equals_sql_other_as_of_and_basis(metrics_build, tmp_path):
    db = tmp_path / "p.duckdb"
    shutil.copy(metrics_build["db"], db)
    c = duckdb.connect(str(db))
    try:
        install_sql(c)
        for as_of, basis in ((date(2025, 6, 30), "asof"), (date(2024, 12, 31), "attempt")):
            _, tg, tm = compute_targets(c, as_of=as_of, d17_basis=basis)
            flag = "true" if basis == "attempt" else "false"
            for rows, mac, key in (
                (tg, "t1_targets_sql", ["deal_id"]),
                (tm, "t1_metrics_sql", ["metric", "dimension", "segment"]),
            ):
                sql = c.execute(f"SELECT * FROM {mac}(DATE '{as_of}', {flag}, {T1_HORIZON_BDAYS})").df()
                py = pd.DataFrame(rows, columns=sql.columns)
                for col in sql.columns:
                    if sql[col].dtype.kind in "iu" and py[col].isna().any():
                        sql[col] = sql[col].astype("Int64")
                assert_frames_equal(py, sql, key)
    finally:
        c.close()


def test_deal_metrics_at_horizon_equals_python_per_deal(con):
    """deal_metrics_at_sql(.., 180) evaluates each deal as of its own horizon date, as the Python does."""
    m = load_model(con)
    by_id = {d.deal_id: d for d in m["deals"]}
    sql = df(con, f"SELECT * FROM deal_metrics_at_sql(DATE '{AS_OF_DEFAULT}', true, {T1_HORIZON_BDAYS})")
    rnd = random.Random(5)
    ids = rnd.sample(sorted(sql.deal_id), 60)
    ev = [e for e in m["events"] if e.deal_id in set(ids)]
    st = [s for s in m["status"] if s.deal_id in set(ids)]
    cal = cal_index()
    s = sql.set_index("deal_id")
    for i in ids:
        h = add_business_days(by_id[i].funding_date, T1_HORIZON_BDAYS)
        py = deal_metrics(
            by_id[i], [e for e in ev if e.deal_id == i], [x for x in st if x.deal_id == i], as_of=h,
            successors=by_id, cal=cal,
        )  # fmt: skip
        a = s.loc[i, "as_of"]
        assert (a.date() if isinstance(a, pd.Timestamp) else a) == h
        for col in ("outcome", "d7_cash_from_merchant_cents", "d19_bad_economic", "d18_bad_naive"):
            assert py[col] == s.loc[i, col], (i, col)


# ------------------------------------------------------------------------------------------ T1 maturity
def test_t1_maturity_flags_at_the_horizon_boundary(con):
    tg = df(con, "SELECT * FROM deal_targets")
    as_of = AS_OF_DEFAULT
    for r in tg.itertuples():
        f = r.funding_date.date() if hasattr(r.funding_date, "date") else r.funding_date
        h = add_business_days(f, T1_HORIZON_BDAYS)
        assert (r.horizon_date.date() if hasattr(r.horizon_date, "date") else r.horizon_date) == h
        assert bool(r.mature) == (h <= as_of)
        assert pd.isna(r.t1_bad_economic) == (not r.mature)
    # the boundary deals: the last funding date that is mature and the first that is not
    mature_f = max(r for r, m in zip(tg.funding_date, tg.mature, strict=True) if m)
    young_f = min(r for r, m in zip(tg.funding_date, tg.mature, strict=True) if not m)
    assert add_business_days(mature_f.date(), T1_HORIZON_BDAYS) <= as_of
    assert add_business_days(young_f.date(), T1_HORIZON_BDAYS) > as_of
    # vintage flags: mature vintages have every deal mature; immature ones none
    v = df(con, "SELECT * FROM target_metrics WHERE dimension = 'vintage' AND metric = 'bad_rate_economic'")
    for r in v.itertuples():
        assert r.maturity == (
            "mature" if r.denominator == r.n_deals else "immature" if r.denominator == 0 else "partial"
        )
    mat = dict(zip(v.segment, v.maturity, strict=True))
    assert mat["2025Q4"] == "mature" and mat["2026Q1"] == "partial" and mat["2026Q3"] == "immature"


def test_t1_events_after_horizon_do_not_matter(metrics_build, tmp_path):
    """Deleting every ledger / status event dated after a deal's horizon leaves its T1 unchanged."""
    db = tmp_path / "h.duckdb"
    shutil.copy(metrics_build["db"], db)
    c = duckdb.connect(str(db))
    try:
        before = df(c, "SELECT deal_id, t1_bad_economic, t1_bad_naive, outcome_at_horizon FROM deal_targets")
        for t in ("ledger", "deal_status_events"):
            c.execute(
                f"DELETE FROM {t} x USING deal_targets t WHERE x.deal_id = t.deal_id AND t.mature "
                "AND x.event_date > t.horizon_date"
            )
        _, tg, _ = compute_targets(c)
        after = pd.DataFrame(tg)[before.columns]
        assert_frames_equal(before, after, ["deal_id"])
    finally:
        c.close()


# ------------------------------------------------------------------------------------------ stage outputs
def test_analytics_writes_every_figure(analytics_build):
    out = analytics_build["result"].figures
    assert set(out) == set(figures.FIGURES)
    fdir = analytics_build["dir"] / "figures"
    for name in figures.FIGURES:
        assert (fdir / f"{name}.png").stat().st_size > 10_000
        fig = pio.from_json((fdir / f"{name}.plotly.json").read_text(encoding="utf-8"))
        assert len(fig.data) > 0
        assert len(pd.read_csv(fdir / "data" / f"{name}.csv")) > 0


def test_findings_numbers_equal_metric_table_queries(analytics_build):
    c = duckdb.connect(str(analytics_build["db"]), read_only=True)
    try:
        md = analytics_build["dir"] / "findings.md"
        js = analytics_build["dir"] / "findings_numbers.json"
        chk = check_findings(c, md, js)
        assert chk["mismatches"] == [] and chk["unregistered_tokens"] == []
        assert chk["match_rate"] == 1.0 and chk["numbers"] > 100
        recs = {r["key"]: r for r in json.loads(js.read_text(encoding="utf-8"))}
        text = md.read_text(encoding="utf-8")
        # the headline numbers, queried independently here
        for key, sql, fmt in (
            ("p1_rep", "SELECT value FROM portfolio_metrics WHERE metric_id='P1' AND metric='reported_collection_rate' AND segment='all'", "pct"),
            ("p1_adj", "SELECT value FROM portfolio_metrics WHERE metric_id='P1' AND metric='adjusted_collection_rate' AND segment='all'", "pct"),
            ("p2_gap", "SELECT value FROM portfolio_metrics WHERE metric_id='P2' AND segment='all'", "pp"),
            ("p4_econ", "SELECT value FROM portfolio_metrics WHERE metric_id='P4' AND metric='bad_rate_economic' AND segment='all'", "pct"),
            ("p4_naive", "SELECT value FROM portfolio_metrics WHERE metric_id='P4' AND metric='bad_rate_naive' AND segment='all'", "pct"),
            ("p6_econ", "SELECT value FROM portfolio_metrics WHERE metric_id='P6' AND metric='economic_exposure_cents'", "usd_m"),
            ("p6_contr", "SELECT value FROM portfolio_metrics WHERE metric_id='P6' AND metric='contractual_balance_cents'", "usd_m"),
            ("t1_econ", "SELECT value FROM target_metrics WHERE metric='bad_rate_economic' AND dimension='all'", "pct"),
            ("t1e_2025Q1", "SELECT value FROM target_metrics WHERE metric='bad_rate_economic' AND dimension='vintage' AND segment='2025Q1'", "pct"),
            ("p8_with", "SELECT value FROM portfolio_metrics WHERE metric_id='P8' AND metric='bad_rate_chains_with_distressed'", "pct"),
            ("p9_all", "SELECT value FROM portfolio_metrics WHERE metric_id='P9' AND segment='all'", "pct"),
        ):  # fmt: skip
            v = c.execute(sql).fetchone()[0]
            assert recs[key]["value"] == v and recs[key]["text"] == fmt_value(v, fmt)
            assert recs[key]["text"] in text, key
        # every number cites metric IDs
        assert all(r["metric_ids"] for r in recs.values())
    finally:
        c.close()


def test_findings_check_detects_tampering(analytics_build, tmp_path):
    db = tmp_path / "t.duckdb"
    shutil.copy(analytics_build["db"], db)
    md = tmp_path / "findings.md"
    js = tmp_path / "findings_numbers.json"
    shutil.copy(analytics_build["dir"] / "findings.md", md)
    shutil.copy(analytics_build["dir"] / "findings_numbers.json", js)
    c = duckdb.connect(str(db))
    try:
        c.execute(
            "UPDATE portfolio_metrics SET value = value + 0.001 WHERE metric_id = 'P2' AND segment = 'all'"
        )
        chk = check_findings(c, md, js)
        assert any(m.startswith("p2_gap") for m in chk["mismatches"])
        c.execute(
            "UPDATE portfolio_metrics SET value = value - 0.001 WHERE metric_id = 'P2' AND segment = 'all'"
        )
        md.write_text(
            md.read_text(encoding="utf-8").replace("## 2.", "Also 12.3% of something.\n\n## 2."), "utf-8"
        )
        assert "12.3%" in check_findings(c, md, js)["unregistered_tokens"]
    finally:
        c.close()


def test_findings_story_and_caveats(analytics_build):
    text = (analytics_build["dir"] / "findings.md").read_text(encoding="utf-8")
    order = [text.index(s) for s in ("## 1. Renewal masking", "## 2. Bad rate", "## 3. Exposure", "## 4. Vintage",
                                     "## 5. Refinance", "## 6. Segments", "## Caveats")]  # fmt: skip
    assert order == sorted(order)
    for s in ("Synthetic data", "lower bound", "M0219", "immature"):
        assert s in text
    assert "ground" + "_truth" not in text


def test_analytics_manifest_stage(metrics_build, tmp_path):
    from mcarisk.manifest import open_run

    db, man = tmp_path / "m.duckdb", tmp_path / "run_manifest.json"
    shutil.copy(metrics_build["db"], db)
    shutil.copy(metrics_build["manifest"], man)
    run = open_run(man)
    with run.stage("analytics"):
        run_analytics(db, fig_dir=tmp_path / "f", findings_md=tmp_path / "f.md", findings_json=tmp_path / "f.json",
                      run=run)  # fmt: skip
    run.write()
    st = json.loads(man.read_text(encoding="utf-8"))["stages"]["analytics"]
    assert st["status"] == "ok"
    assert set(st["extra"]["figures"]) == set(figures.FIGURES)
    assert st["extra"]["findings_check"]["match_rate"] == 1.0


def test_vintage_horizon_is_180_business_days():
    # 180 Fed ACH business days is about 8.5 calendar months: the as-of date makes 2025Q4 the last mature vintage
    h = add_business_days(date(2025, 12, 31), T1_HORIZON_BDAYS)
    assert timedelta(days=250) < h - date(2025, 12, 31) < timedelta(days=265)
    assert h <= AS_OF_DEFAULT
