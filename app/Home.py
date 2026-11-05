"""MCA portfolio risk app: entry point (``make app`` / Streamlit Community Cloud main file ``app/Home.py``)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c
import streamlit as st

c.page_setup("Home")
c.require_data()

st.title("MCA portfolio risk, measured in cash")
st.markdown(
    'When a merchant cash advance is renewed, the old balance is "paid off" out of the new advance. '
    "Trackers record that as collected, but no cash came from the merchant. This app measures the book "
    f"**in cash, at the merchant-relationship level** ({c.links(['D7', 'D11', 'R3'])}), from the messy "
    "spreadsheets a funder actually keeps. Every number cites its METRICS.md definition; click an ID to "
    "read it."
)

pm = c.query("SELECT MAX(as_of) AS as_of FROM portfolio_metrics")
meta = c.app_meta()
fn = c.findings_numbers()


def _t(key: str) -> str:
    return fn[key]["text"] if key in fn else "n/a"


cols = st.columns(4)
cols[0].metric("As of", str(pm.as_of.iloc[0])[:10])
cols[1].metric("Masking gap (P2)", _t("p2_gap"))
cols[2].metric("Bad rate naive vs economic (P4)", f"{_t('p4_naive')} vs {_t('p4_econ')}")
cols[3].metric("Open exposure, economic (P6)", _t("p6_econ"))
st.caption(
    f"Definitions: {c.links(['P2', 'P4', 'P6'])}. Values from the findings registry (metric-table queries)."
)

st.subheader("Pages")
pages = [
    (
        "pages/1_Portfolio.py",
        "Portfolio",
        "headline cards, renewal masking, vintages, exposure, refinance quality",
    ),
    (
        "pages/2_Merchant_drilldown.py",
        "Merchant drill-down",
        "one relationship's chain, D- and R-metrics, lineage",
    ),
    ("pages/3_Definitions.py", "Definitions", "METRICS.md, with a link target for every metric ID"),
    (
        "pages/4_Data_quality.py",
        "Data quality",
        "rule hits with sample rows, the exceptions queue, the run manifest",
    ),
    ("pages/5_Model.py", "Model", "model card, calibration, lift, naive vs economic target, survival curves"),
    ("pages/6_Evals.py", "Evals", "the latest eval report against evals/thresholds.json"),
]
for path, label, what in pages:
    st.page_link(path, label=f"{label}: {what}")

st.subheader("About this snapshot")
st.markdown(
    f"- Pipeline run `{meta.get('source_run_id', '?')}` (git `{(meta.get('source_git') or {}).get('sha', '?')}`), "
    f"METRICS.md {meta.get('metrics_md', '?')}.\n"
    f"- The app reads `app/data/` (refreshed by `make app-data` from `build/`): {len(meta.get('tables', {}))} "
    f"tables in a read-only DuckDB file ({meta.get('db_bytes', 0) / 1e6:.1f} MB) and "
    f"{len(meta.get('figures', {}))} figures.\n"
    "- Rebuild everything from `raw/` with `make all` (under 2 minutes, no network), then `make eval app-data`.\n"
    "- The data is synthetic: every merchant, ISO and number is fictional."
)
