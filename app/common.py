"""Shared helpers for the Streamlit app: data access, formatting and links to METRICS.md definitions.

The app reads only the data snapshot in ``app/data/`` (refreshed by ``make app-data`` from ``build/``),
plus ``METRICS.md`` and ``evals/thresholds.json`` from the repository. It opens the DuckDB file read-only,
never calls the network and needs no API key. It displays numbers from the metric tables; it does not
recompute any METRICS.md definition. Set ``MCARISK_APP_DATA`` to point it at another snapshot.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import plotly.io as pio
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
DATA_DIR = Path(os.environ.get("MCARISK_APP_DATA", APP_DIR / "data"))
METRICS_MD = ROOT / "METRICS.md"
THRESHOLDS = ROOT / "evals" / "thresholds.json"
EVAL_LATEST = ROOT / "evals" / "reports" / "latest.json"

# METRICS.md §7 worked example: relationship M0219 is merchant key MK1A8B5FBF55 in the pipeline
# (the same constant as mcarisk.analytics.findings.M0219).
M0219_KEY = "MK1A8B5FBF55"
DEFINITIONS_PAGE = "Definitions"

ID_RE = re.compile(r"\b([DRPTQL])([1-9]\d?)\b")  # not R01 / R08 (return codes)


# ---------------------------------------------------------------------------------------------- data
@st.cache_resource
def _connection(path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path, read_only=True)


def db_path() -> Path:
    return DATA_DIR / "portfolio.duckdb"


def data_available() -> bool:
    return db_path().exists()


def require_data() -> None:
    if not data_available():
        st.error(
            f"No data snapshot at `{db_path()}`. Run `make all eval app-data` to build it "
            "(or set `MCARISK_APP_DATA`)."
        )
        st.stop()


@st.cache_data(show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query on the snapshot. A cursor per call keeps the shared connection thread-safe."""
    cur = _connection(str(db_path())).cursor()
    try:
        return cur.execute(sql, list(params)).df()
    finally:
        cur.close()


@st.cache_data(show_spinner=False)
def read_text(name: str) -> str | None:
    p = DATA_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else None


@st.cache_data(show_spinner=False)
def read_json(name: str) -> Any:
    p = DATA_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def app_meta() -> dict:
    return read_json("app_data.json") or {}


def figure(name: str):
    """A Part 5 / 6 figure from its plotly JSON (built by the pipeline, copied by ``make app-data``)."""
    p = DATA_DIR / "figures" / f"{name}.plotly.json"
    if not p.exists():
        return None
    return pio.from_json(p.read_text(encoding="utf-8"))


def show_figure(name: str, caption: str | None = None) -> None:
    fig = figure(name)
    if fig is None:
        st.warning(f"Figure `{name}` is missing from the snapshot.")
        return
    st.plotly_chart(fig, width="stretch", key=f"fig_{name}")
    ids = app_meta().get("figure_metric_ids", {}).get(name, [])
    cite = f"Metrics: {links(ids)}" if ids else ""
    st.caption(" · ".join(x for x in (cite, caption) if x))


@st.cache_data(show_spinner=False)
def findings_numbers() -> dict[str, dict]:
    """The Part 5 findings registry: key -> {metric_ids, sql, value, text}. Every value is a metric-table query."""
    return {n["key"]: n for n in (read_json("findings_numbers.json") or [])}


def eval_report() -> tuple[dict | None, str]:
    """The latest eval report: ``evals/reports/latest.json`` when present, else the snapshot copy."""
    if EVAL_LATEST.exists():
        return json.loads(EVAL_LATEST.read_text(encoding="utf-8")), "evals/reports/latest.json"
    snap = read_json("eval_latest.json")
    return snap, "app/data/eval_latest.json (snapshot of evals/reports/latest.json)"


def thresholds() -> dict:
    return json.loads(THRESHOLDS.read_text(encoding="utf-8")) if THRESHOLDS.exists() else {}


@st.cache_data(show_spinner=False)
def metrics_md() -> str:
    return METRICS_MD.read_text(encoding="utf-8") if METRICS_MD.exists() else ""


# ---------------------------------------------------------------------------------------- formatting
def usd(cents: Any, digits: int = 0) -> str:
    if cents is None or pd.isna(cents):
        return "n/a"
    v = float(cents) / 100
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.{digits}f}"


def usd_m(cents: Any) -> str:
    if cents is None or pd.isna(cents):
        return "n/a"
    return f"${float(cents) / 1e8:,.2f}M"


def mdx(text: str) -> str:
    """Escape ``$`` so Streamlit markdown shows dollar amounts instead of starting LaTeX."""
    return re.sub(r"(?<!\\)\$", lambda _: "\\$", text)


def pct(x: Any, digits: int = 1) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"{float(x) * 100:.{digits}f}%"


def num(x: Any, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"{float(x):,.{digits}f}"


# ------------------------------------------------------------------------------------ definitions
def link(metric_id: str) -> str:
    """Markdown link from a metric ID to its definition on the Definitions page."""
    return f"[{metric_id}]({DEFINITIONS_PAGE}?id={metric_id}#{metric_id.lower()})"


def links(ids) -> str:
    return ", ".join(link(i) for i in ids)


def cite(text: str) -> str:
    """Turn every metric ID in ``text`` (D7, R3, P4, T1, Q5, L1 ...) into a link to its definition."""
    return ID_RE.sub(lambda m: link(m.group(0)), mdx(text))


def _clean(cell: str) -> str:
    return cell.strip().replace("**", "")


@st.cache_data(show_spinner=False)
def metric_index(md: str) -> dict[str, dict[str, str]]:
    """Every metric / rule ID defined in METRICS.md -> {name, definition, section}.

    Sources: table rows whose first cell is an ID (D1-D15, R1-R10, P1-P9, T1-T3, Q1-Q7), the bold
    D16-D19 paragraphs and the L1-L3 rules. This only indexes the document; it defines nothing.
    """
    out: dict[str, dict[str, str]] = {}
    section = ""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
        m = re.match(r"^\|\s*\**([DRPTQ]\d{1,2})\b\**\s*([^|]*)\|(.*)\|\s*$", line)
        if m:
            mid, rest, tail = m.group(1), m.group(2).strip(), m.group(3)
            cells = [c for c in tail.split("|")]
            if rest:  # "| Q1 Merchant key | definition |"
                name, definition = _clean(rest), _clean(" | ".join(cells))
            else:  # "| D7 | Cash from merchant | definition |" (T1 has extra columns)
                name = _clean(cells[0]) if cells else ""
                definition = _clean(" | ".join(cells[1:]))
            out.setdefault(mid, {"name": name, "definition": definition, "section": section})
            continue
        m = re.match(r"^\*\*(D1[6-9]): ([^*]+)\*\*\s*(.*)$", line)
        if m:
            body = m.group(3).lstrip("= ").strip()
            j = i + 1
            while j < len(lines) and lines[j].startswith("- "):
                body += " " + lines[j]
                j += 1
            name = m.group(2).strip().rstrip(".")
            out.setdefault(
                m.group(1), {"name": name, "definition": body.replace("**", ""), "section": section}
            )
            continue
        m = re.match(r"^\*\*Rule (L\d): ([^*]+)\*\*\s*(.*)$", line)
        if m:
            body = m.group(3)
            j = i + 1
            while j < len(lines) and lines[j].startswith("- "):
                body += " " + lines[j]
                j += 1
            name = m.group(2).strip().rstrip(".")
            out.setdefault(
                m.group(1), {"name": name, "definition": body.replace("**", ""), "section": section}
            )
    return out


def sidebar() -> None:
    meta = app_meta()
    with st.sidebar:
        st.caption(
            f"Snapshot: run `{meta.get('source_run_id', '?')}` · METRICS.md {meta.get('metrics_md', '?')} · "
            f"exported {meta.get('exported_at', '?')}"
        )
        st.caption("Synthetic data. Every merchant, ISO and number is fictional.")


def page_setup(title: str) -> None:
    st.set_page_config(page_title=f"{title} · MCA risk", layout="wide")
    sidebar()
