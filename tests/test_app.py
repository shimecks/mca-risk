"""Part 7 (UI redesign): Streamlit app smoke tests (``streamlit.testing.v1.AppTest``) on every page, plus the app's data
snapshot (``app/data/``) and the deployability rules (no pipeline import, no network, no API key)."""

from __future__ import annotations

import json
import re

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from mcarisk import paths
from mcarisk.appdata import APP_TABLES, REQUIRED_FIGURES

APP = paths.ROOT / "app"
DATA = APP / "data"
PAGES = {
    "home": APP / "Home.py",
    "trends": APP / "pages" / "1_Portfolio_trends.py",
    "merchant": APP / "pages" / "2_Merchant_story.py",
    "model": APP / "pages" / "3_Risk_model.py",
    "behind": APP / "pages" / "4_Behind_the_numbers.py",
}
M0219 = "MK1A8B5FBF55"
TIMEOUT = 120


def run(page: str, query: dict[str, str] | None = None) -> AppTest:
    at = AppTest.from_file(str(PAGES[page]), default_timeout=TIMEOUT)
    for k, v in (query or {}).items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def all_markdown(at) -> str:
    return "\n".join(m.value for m in at.markdown)


# ------------------------------------------------------------------------------------- the snapshot
def test_every_page_file_exists_and_pages_dir_has_no_others():
    for p in PAGES.values():
        assert p.exists(), p
    assert sorted(p.name for p in (APP / "pages").glob("*.py")) == sorted(
        p.name for k, p in PAGES.items() if k != "home"
    )


def test_snapshot_has_every_table_and_figure_and_is_small():
    db = DATA / "portfolio.duckdb"
    assert db.exists(), "run `make all eval app-data`"
    assert db.stat().st_size < 20e6  # committed to git: keep it small
    con = duckdb.connect(str(db), read_only=True)
    try:
        have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        assert set(APP_TABLES) <= have
        for t, cols in APP_TABLES.items():
            got = [r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall()]
            if cols is not None:
                assert got == cols, t
            assert con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] > 0 or t in {"std_rejects"}, t
        # M0219 is in the snapshot with both deals and their lineage
        rows = con.execute(
            "SELECT deal_id, src_file, src_row FROM deals_resolved WHERE merchant_key = ? ORDER BY deal_id",
            [M0219],
        ).fetchall()
        assert [r[0] for r in rows] == [1102, 1150]
    finally:
        con.close()
    for name in REQUIRED_FIGURES:
        assert (DATA / "figures" / f"{name}.plotly.json").exists(), name
    meta = json.loads((DATA / "app_data.json").read_text(encoding="utf-8"))
    assert set(meta["tables"]) == set(APP_TABLES)
    assert set(meta["figure_metric_ids"]) >= set(REQUIRED_FIGURES)
    for f in (
        "findings.md",
        "findings_numbers.json",
        "model_card.md",
        "dq_report.md",
        "run_manifest.json",
        "eval_latest.json",
    ):
        assert (DATA / f).exists(), f


def test_app_is_deployable_without_pipeline_network_or_keys():
    """The app imports only streamlit / duckdb / pandas / plotly (app/requirements.txt), never mcarisk,
    never an HTTP / LLM client, and never reads an API key."""
    allowed = {
        "streamlit",
        "duckdb",
        "pandas",
        "plotly",
        "common",
        "__future__",
        "json",
        "os",
        "re",
        "sys",
        "pathlib",
        "typing",
    }
    for p in APP.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        mods = {m.split(".")[0] for m in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, flags=re.M)}
        assert mods <= allowed, (p, mods - allowed)
        assert "OPENROUTER" not in text and "API_KEY" not in text, p
    req = (APP / "requirements.txt").read_text(encoding="utf-8")
    pkgs = {line.split("==")[0] for line in req.splitlines() if line and not line.startswith("#")}
    assert {"streamlit", "duckdb", "pandas", "plotly"} <= pkgs
    assert (paths.ROOT / ".streamlit" / "config.toml").exists()


def test_static_guard_scans_app_dir():
    """tests/test_no_ground_truth.py scans app/ (code and data snapshot); make sure that scan is not vacuous."""
    from tests.test_no_ground_truth import PIPELINE_DIRS, scan

    assert "app" in PIPELINE_DIRS
    assert any(APP.rglob("*.py")) and any(DATA.iterdir())
    assert scan() == []


# ------------------------------------------------------------------------------------------- pages
def _db_value(sql: str, params: list | None = None):
    con = duckdb.connect(str(DATA / "portfolio.duckdb"), read_only=True)
    try:
        return con.execute(sql, params or []).fetchone()[0]
    finally:
        con.close()


def _frames(at) -> list:
    out = []
    for df in at.dataframe:
        v = df.value
        out.append(v.data if hasattr(v, "data") else v)
    return out


def test_home_tells_the_story_in_five_cards_and_one_chart():
    at = run("home")
    assert at.title[0].value.startswith("MCA portfolio risk")
    labels = [m.label for m in at.metric]
    assert labels == [
        "Cash collected",
        "Deals that went bad",
        "Money really at risk",
        "Renewal “collections”",
        "Distressed refi chains",
    ]
    adj = _db_value(
        "SELECT value FROM portfolio_metrics WHERE metric='adjusted_collection_rate' AND segment='all'"
    )
    econ = _db_value("SELECT value FROM portfolio_metrics WHERE metric='bad_rate_economic' AND segment='all'")
    exp = _db_value(
        "SELECT value FROM portfolio_metrics WHERE metric='economic_exposure_cents' AND segment='all'"
    )
    vals = {m.label: m.value for m in at.metric}
    assert vals["Cash collected"] == f"{adj * 100:.1f}%"
    assert vals["Deals that went bad"] == f"{econ * 100:.1f}%"
    assert vals["Money really at risk"] == f"${exp / 1e8:.1f}M"
    # spec IDs live in tooltips, not in labels or headings
    assert all(not re.search(r"\b[DRPT]\d+\b", lab) for lab in labels)
    assert "P1" in at.metric[0].proto.help and "P6" in at.metric[2].proto.help
    assert len(at.get("plotly_chart")) == 1
    assert not at.get("page_link")  # no navigation list in the main view
    md = all_markdown(at)
    assert "92.2%" in md and "double" in md


def test_portfolio_trends_charts_and_vintage_maturity():
    at = run("trends")
    assert len(at.get("plotly_chart")) == 5  # vintages, exposure, refis, segments + curves in an expander
    vint = _frames(at)[0]
    assert {"mature", "partly mature", "too young"} <= set(vint["Status"])
    assert "2024-Q3" in set(vint["Funding quarter"])
    assert not any(re.fullmatch(r"[a-z_]+", col) for col in vint.columns)  # friendly headers only
    at.radio(key="segment_dim").set_value("Industry").run()
    assert not at.exception


def test_merchant_story_defaults_to_m0219_with_both_deals_verdict_note_and_lineage():
    at = run("merchant")
    assert at.selectbox(key="merchant").value == M0219
    labels = [m.label for m in at.metric]
    assert labels == ["Cash out", "Cash back", "Net result"]
    verdict = " ".join(e.value for e in at.error)
    assert "Lost \\$6,648" in verdict and "deal 1102" in verdict and "distressed refinance" in verdict
    note = " ".join(i.value for i in at.info)
    assert "floor" in note and "1102" in note and "10,072" in note
    frames = _frames(at)
    summary = frames[0]
    assert list(summary["Deal"]) == ["Deal 1102", "Deal 1150"]
    assert list(summary["Cash actually collected"]) == ["44%", "27%"]
    lineage = []
    for v in frames:
        if {"Source file", "Sheet", "Row"} <= set(v.columns):
            lineage += list(zip(v["Source file"], v["Sheet"], v["Row"], strict=True))
    assert ("raw/deal_tracker_2024.xlsx", "2024 Deals", 31) in lineage  # deal 1150's tracker row
    assert ("raw/ach_exports/ach_2023_11.csv", "(csv)", 1090) in lineage  # deal 1102: ACH funding row
    assert ("raw/collections_log.xlsx", "Log", 100) in lineage  # 1150's write-off
    assert any(e.label.startswith("Where these numbers come from") for e in at.expander)
    assert len(at.get("plotly_chart")) == 1  # the timeline
    assert not any("src_file" in f.columns for f in frames[:1])  # the summary uses friendly headers


def test_merchant_story_other_merchant_by_deal_query_param():
    con = duckdb.connect(str(DATA / "portfolio.duckdb"), read_only=True)
    deal, mk = con.execute(
        "SELECT deal_id, merchant_key FROM deal_metrics WHERE merchant_key <> ? AND d16_default "
        "ORDER BY deal_id LIMIT 1",
        [M0219],
    ).fetchone()
    con.close()
    at = run("merchant", {"deal": str(deal)})
    assert at.selectbox(key="merchant").value == mk
    assert f"Deal {deal}" in set(_frames(at)[0]["Deal"])
    assert not any("10,072" in i.value for i in at.info)


def test_risk_model_page():
    at = run("model")
    labels = [m.label for m in at.metric]
    assert labels == [
        "How well it ranks risky deals",
        "Can its probabilities be trusted?",
        "Riskiest 10% of deals",
    ]
    auc = _db_value(
        "SELECT value FROM model_performance WHERE model='logit' AND split='test' AND metric='auc' "
        "AND eval_target='economic'"
    )
    assert at.metric[0].value == f"{auc:,.3f}"
    md = all_markdown(at)
    assert "0.681" in md  # the observable ceiling from the E8 report
    assert "picked" in " ".join(c.value for c in at.caption)  # calibration chosen after the test read
    assert "What not to use it for" in md and md.count("- Not") >= 3
    assert len(at.get("plotly_chart")) == 5  # lift, calibration, naive vs economic + 2 in expanders
    assert any(e.label == "Full model card" for e in at.expander)


def test_behind_the_numbers_data_quality_tests_definitions_and_run():
    at = run("behind", {"id": "D17"})
    assert [t.label for t in at.tabs] == ["Data quality", "Tests", "Definitions", "Run details"]
    rules = next(f for f in _frames(at) if "Check" in f.columns and "Spec" in f.columns)
    assert {"Q1", "Q5", "Q6", "Q7", "L1", "L2", "L3"} <= set(rules["Spec"])
    assert {m.label: m.value for m in at.metric}["Misposted payments fixed"] == "227"
    assert at.multiselect(key="exc_rules").value
    assert any(f.shape[0] > 0 and "exception_id" in f.columns for f in _frames(at))
    # tests (evals)
    exp = [e.label for e in at.expander]
    for sid in [f"E{i}" for i in range(1, 10)]:
        assert any(lab.startswith(f"{sid} ") for lab in exp), sid
    assert any("E8 gate change" in w.value for w in at.warning)
    assert any("E7 comparison basis" in i.value for i in at.info)
    assert any("test suites pass" in s.value for s in at.success)
    ro = " ".join(f.to_csv() for f in _frames(at))
    assert "observable_ceiling_auc" in ro and "calibration_slope_uncalibrated" in ro
    # definitions: lookup and one anchored entry per ID
    assert any("Distressed refinance" in s.value for s in at.success)
    subs = [s.value for s in at.subheader]
    assert sum(1 for s in subs if re.match(r"^[DRPTLQ]\d+: ", s)) == 51
    assert {"d7", "r3", "p4", "t1", "l1", "q5"} <= {s.proto.anchor for s in at.subheader}
    assert any("MCA Risk Metric Definitions" in m.value for m in at.markdown)  # METRICS.md rendered
    # run details
    assert any("Stage" in f.columns for f in _frames(at))
    assert len(at.json) >= 1
    bad = run("behind", {"id": "X9"})
    assert any("not a metric ID" in w.value for w in bad.warning)


def test_formatting_helpers():
    import sys

    sys.path.insert(0, str(APP))
    import common as c

    assert c.money(1_183_871_205) == "$11.8M"
    assert c.money(664_752) == "$6,648"
    assert c.usd(-664_752) == "-$6,648"
    assert c.pct(0.70877) == "70.9%"
    assert c.pts(21.276) == "21 pts"
    assert c.date("2026-09-30") == "Sep 30, 2026"
    assert c.quarter("2024Q3") == "2024-Q3"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_runs_without_exception(page):
    run(page)
