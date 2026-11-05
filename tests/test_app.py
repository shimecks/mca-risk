"""Part 7: Streamlit app smoke tests (``streamlit.testing.v1.AppTest``) on every page, plus the app's data
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
    "portfolio": APP / "pages" / "1_Portfolio.py",
    "drilldown": APP / "pages" / "2_Merchant_drilldown.py",
    "definitions": APP / "pages" / "3_Definitions.py",
    "data_quality": APP / "pages" / "4_Data_quality.py",
    "model": APP / "pages" / "5_Model.py",
    "evals": APP / "pages" / "6_Evals.py",
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
def test_home():
    at = run("home")
    assert at.title[0].value.startswith("MCA portfolio risk")
    assert len(at.metric) == 4
    assert len(at.get("page_link")) == 6
    assert "Definitions?id=P2" in all_markdown(at) + " ".join(c.value for c in at.caption)


def test_portfolio_cards_charts_and_vintage_maturity():
    at = run("portfolio")
    labels = [m.label for m in at.metric]
    for want in (
        "Reported vs cash-based collection rate",
        "Renewal masking gap",
        "Bad rate: naive vs economic",
        "Open exposure: economic vs contractual",
        "Internal-payoff share of 'collections'",
    ):
        assert want in labels
    con = duckdb.connect(str(DATA / "portfolio.duckdb"), read_only=True)
    gap = con.execute(
        "SELECT value FROM portfolio_metrics WHERE metric_id='P2' AND segment='all'"
    ).fetchone()[0]
    con.close()
    assert next(m.value for m in at.metric if m.label == "Renewal masking gap") == f"{gap:.1f} pp"
    md = all_markdown(at)
    for mid in ("P1", "P2", "P3", "P4", "P6", "P8"):
        assert f"Definitions?id={mid}" in md, mid
    assert (
        len(at.get("plotly_chart")) >= 6
    )  # masking, bad-rate-by-vintage, vintage curves, exposure, refi, segments
    vint = at.dataframe[0].value
    vint = vint.data if hasattr(vint, "data") else vint
    assert {"immature", "partial", "mature"} <= set(vint["maturity"])


def test_drilldown_defaults_to_m0219_with_both_deals_note_and_lineage():
    at = run("drilldown")
    assert at.selectbox(key="merchant").value == M0219
    md = all_markdown(at)
    assert "deal 1102" in md and "deal 1150" in md
    for mid in ("D6", "D7", "D8", "D10", "D11", "D17", "D19", "R1", "R3", "R8"):
        assert f"Definitions?id={mid}" in md, mid
    warn = " ".join(w.value for w in at.warning)
    assert "lower bounds" in warn and "upper bound" in warn and "1102" in warn
    pops = at.get("popover")
    labels = [p.proto.popover.label for p in pops]
    assert any("deal 1102" in lab for lab in labels) and any("deal 1150" in lab for lab in labels)
    assert any("payoff: internal" in lab for lab in labels)
    assert any("write-off" in lab for lab in labels)
    lineage = []
    for p in pops:
        for df in p.dataframe:
            v = df.value
            if {"src_file", "src_sheet", "src_row"} <= set(v.columns):
                lineage += list(zip(v.src_file, v.src_sheet, v.src_row, strict=True))
    assert ("raw/deal_tracker_2024.xlsx", "2024 Deals", 31) in lineage  # deal 1150's tracker row
    assert (
        "raw/ach_exports/ach_2023_11.csv",
        "(csv)",
        1090,
    ) in lineage  # deal 1102 (no tracker row): ACH funding
    assert ("raw/collections_log.xlsx", "Log", 100) in lineage  # 1150's write-off
    assert len(at.get("plotly_chart")) == 1  # the chain timeline


def test_drilldown_other_merchant_by_deal_query_param():
    con = duckdb.connect(str(DATA / "portfolio.duckdb"), read_only=True)
    deal, mk = con.execute(
        "SELECT deal_id, merchant_key FROM deal_metrics WHERE merchant_key <> ? AND d16_default ORDER BY deal_id LIMIT 1",
        [M0219],
    ).fetchone()
    con.close()
    at = run("drilldown", {"deal": str(deal)})
    assert at.selectbox(key="merchant").value == mk
    assert f"deal {deal}" in all_markdown(at)
    assert not any("METRICS.md §7 data note" in w.value for w in at.warning)


def test_definitions_anchor_per_metric_and_lookup():
    at = run("definitions", {"id": "D17"})
    assert "Distressed refinance" in at.success[0].value
    subs = [s.value for s in at.subheader]
    for mid in ["D1", "D7", "D16", "D17", "D19", "R3", "R10", "P2", "P9", "T1", "T3", "L1", "L3", "Q5", "Q7"]:
        assert any(s.startswith(f"{mid}: ") for s in subs), mid
    assert sum(1 for s in subs if re.match(r"^[DRPTLQ]\d+: ", s)) == 51
    anchors = {s.proto.anchor for s in at.subheader}
    assert {"d7", "r3", "p4", "t1", "l1", "q5"} <= anchors
    assert any("MCA Risk Metric Definitions" in m.value for m in at.markdown)  # METRICS.md rendered
    bad = run("definitions", {"id": "X9"})
    assert any("not a metric ID" in w.value for w in bad.warning)


def test_data_quality_rules_samples_exceptions_manifest():
    at = run("data_quality")
    rules = at.dataframe[0].value
    assert {"Q1", "Q5", "Q6", "Q7", "L1", "L2", "L3"} <= set(rules.rule_id)
    exp = [e.label for e in at.expander]
    assert any(lab.startswith("L3 ") for lab in exp) and any(lab.startswith("Q5 ") for lab in exp)
    assert any(lab.startswith("Full manifest") for lab in exp)
    assert at.multiselect(key="exc_rules").value  # exceptions queue filter has the queued rules
    assert any(df.value.shape[0] > 0 and "exception_id" in df.value.columns for df in at.dataframe)
    assert any("stage" in df.value.columns for df in at.dataframe)  # run manifest stages
    assert len(at.json) >= 1


def test_model_page():
    at = run("model")
    labels = [m.label for m in at.metric]
    assert "Test AUC (primary logit)" in labels and "Calibration slope (test)" in labels
    con = duckdb.connect(str(DATA / "portfolio.duckdb"), read_only=True)
    auc = con.execute(
        "SELECT value FROM model_performance WHERE model='logit' AND split='test' AND metric='auc' "
        "AND eval_target='economic'"
    ).fetchone()[0]
    con.close()
    assert next(m.value for m in at.metric if m.label == "Test AUC (primary logit)") == f"{auc:,.3f}"
    assert len(at.get("plotly_chart")) == 5  # calibration, lift, naive vs economic, coefficients, survival
    assert any(
        "after" in i.value and "test split" in i.value for i in at.info
    )  # calibration-after-test disclosure
    assert any("Model card" in s.value for s in at.subheader)


def test_evals_page_shows_all_suites_and_gate_changes():
    at = run("evals")
    exp = [e.label for e in at.expander]
    for sid in [f"E{i}" for i in range(1, 10)]:
        assert any(lab.startswith(f"{sid} ") for lab in exp), sid
    assert any("E8 gate change" in w.value for w in at.warning)
    assert any("E7 comparison basis" in i.value for i in at.info)
    assert at.success or at.error
    md = " ".join(df.value.to_csv() for df in at.dataframe)
    assert "observable_ceiling_auc" in md and "calibration_slope_uncalibrated" in md  # report-only metrics


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_runs_without_exception(page):
    run(page)
