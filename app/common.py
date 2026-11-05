"""Shared helpers for the Streamlit app: data access, plain-English formatting, the chart style and cards.

The app reads only the data snapshot in ``app/data/`` (refreshed by ``make app-data`` from ``build/``),
plus ``METRICS.md`` and ``evals/thresholds.json`` from the repository. It opens the DuckDB file read-only,
never calls the network and needs no API key. It displays numbers from the metric tables and the findings
registry; it formats, rounds and relabels them but never recomputes a METRICS.md definition. Set
``MCARISK_APP_DATA`` to point it at another snapshot.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import plotly.graph_objects as go
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
DEFINITIONS_PAGE = "Behind_the_numbers"

ID_RE = re.compile(r"\b([DRPTQL])([1-9]\d?)\b")  # not R01 / R08 (return codes)

# ------------------------------------------------------------------------------------------ colours
# One meaning per colour, on every page: grey = what the tracker / spreadsheets report, blue = what the
# cash says (the economic view), red = losses only. Light blue / light grey are quieter steps of the same.
TRACKER = "#8c8b86"
CASH = "#2a78d6"
CASH_LIGHT = "#9ec5f4"
LOSS = "#d03b3b"
MUTED = "#d6d5d0"
INK = "#1f1f1d"
INK_2 = "#5c5b57"
GRID = "#ecebe7"


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
    """Show a committed pipeline figure as is (used only inside 'more detail' expanders)."""
    fig = figure(name)
    if fig is None:
        st.warning(f"Figure `{name}` is missing from the snapshot.")
        return
    st.plotly_chart(fig, width="stretch", key=f"fig_{name}")
    if caption:
        st.caption(caption)


@st.cache_data(show_spinner=False)
def findings_numbers() -> dict[str, dict]:
    """The Part 5 findings registry: key -> {metric_ids, sql, value, text}. Every value is a metric-table query."""
    return {n["key"]: n for n in (read_json("findings_numbers.json") or [])}


def portfolio() -> pd.DataFrame:
    return query(
        "SELECT metric_id, metric, segment, value, numerator, denominator, as_of FROM portfolio_metrics"
    )


def pm(metric_id: str, metric: str, field: str = "value", segment: str = "all"):
    """One value from ``portfolio_metrics``."""
    df = portfolio()
    r = df[(df.metric_id == metric_id) & (df.metric == metric) & (df.segment == segment)]
    return None if r.empty else r[field].iloc[0]


def as_of_date():
    return query("SELECT MAX(as_of) AS d FROM portfolio_metrics").d.iloc[0]


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
def _na(x: Any) -> bool:
    return x is None or (not isinstance(x, str) and pd.isna(x))


def usd(cents: Any, digits: int = 0) -> str:
    """Exact dollars from integer cents: ``$6,648``."""
    if _na(cents):
        return "n/a"
    v = float(cents) / 100
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.{digits}f}"


def money(cents: Any) -> str:
    """Short dollars for headlines: ``$11.8M`` from a million up, else ``$33,370``."""
    if _na(cents):
        return "n/a"
    v = float(cents) / 100
    sign = "-" if v < 0 else ""
    if abs(v) >= 1e6:
        return f"{sign}${abs(v) / 1e6:,.1f}M"
    return f"{sign}${abs(v):,.0f}"


def usd_m(cents: Any) -> str:
    if _na(cents):
        return "n/a"
    return f"${float(cents) / 1e8:,.2f}M"


def mdx(text: str) -> str:
    """Escape ``$`` so Streamlit markdown shows dollar amounts instead of starting LaTeX."""
    return re.sub(r"(?<!\\)\$", lambda _: "\\$", text)


def pct(x: Any, digits: int = 1) -> str:
    if _na(x):
        return "n/a"
    return f"{float(x) * 100:.{digits}f}%"


def pts(x_pp: Any) -> str:
    """A percentage-point gap: ``21 pts``."""
    if _na(x_pp):
        return "n/a"
    return f"{float(x_pp):.0f} pts"


def num(x: Any, digits: int = 2) -> str:
    if _na(x):
        return "n/a"
    return f"{float(x):,.{digits}f}"


def date(d: Any) -> str:
    """``Sep 30, 2026``."""
    if _na(d):
        return "n/a"
    t = pd.Timestamp(d)
    return f"{t:%b} {t.day}, {t.year}"


def quarter(q: str) -> str:
    """``2024Q3`` -> ``2024-Q3``."""
    return re.sub(r"^(\d{4})Q(\d)$", r"\1-Q\2", str(q))


# ------------------------------------------------------------------------------------ definitions
def link(metric_id: str) -> str:
    """Markdown link from a metric ID to its definition on the Behind the numbers page."""
    return f"[{metric_id}]({DEFINITIONS_PAGE}?id={metric_id})"


def links(ids) -> str:
    return ", ".join(link(i) for i in ids)


def cite(text: str) -> str:
    """Turn every metric ID in ``text`` (D7, R3, P4, T1, Q5, L1 ...) into a link to its definition."""
    return ID_RE.sub(lambda m: link(m.group(0)), mdx(text))


def spec(*ids: str, note: str = "") -> str:
    """Tooltip text naming the METRICS.md IDs behind a number (IDs stay out of headings and labels)."""
    idx = metric_index(metrics_md())
    parts = [f"**{i}**: {idx[i]['name']}" if i in idx else f"**{i}**" for i in ids]
    head = (note + "\n\n") if note else ""
    return mdx(head + "Spec (METRICS.md): " + "; ".join(parts) + ". Definitions: *Behind the numbers*.")


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


# ---------------------------------------------------------------------------------------- layout
def page_setup(title: str) -> None:
    st.set_page_config(page_title=f"{title} · MCA risk", layout="wide")
    with st.sidebar:
        st.caption("Synthetic data: every merchant, broker and number is fictional.")


def card(col, label: str, value: str, compare: str, meaning: str, help: str | None = None) -> None:
    """A headline card: a plain label, one big number, what it is compared with, and what it means."""
    with col.container(border=True):
        st.metric(label, value, help=help)
        st.markdown(mdx(compare))
        st.caption(mdx(meaning))


def footer() -> None:
    meta = app_meta()
    with st.expander("About this data"):
        st.markdown(
            mdx(
                f"Synthetic MCA book (every merchant, broker and number is fictional), measured as of "
                f"**{date(as_of_date())}**. The app reads a read-only snapshot of the pipeline's metric "
                f"tables: run `{meta.get('source_run_id', '?')}`, METRICS.md {meta.get('metrics_md', '?')}, "
                f"exported {meta.get('exported_at', '?')}. Rebuild with `make all eval app-data`."
            )
        )


def style(fig: go.Figure, title: str, height: int = 360, subtitle: str | None = None) -> go.Figure:
    """The house chart style: the title states the takeaway, hairline grid, no chart junk."""
    text = f"<b>{title}</b>" + (
        f"<br><span style='font-size:13px;color:{INK_2}'>{subtitle}</span>" if subtitle else ""
    )
    fig.update_layout(
        title={"text": text, "x": 0, "xanchor": "left", "font": {"size": 17, "color": INK}},
        height=height,
        margin={"t": 78 if subtitle else 56, "l": 8, "r": 8, "b": 48 if subtitle else 8},
        plot_bgcolor="white",
        paper_bgcolor="white",
        font={"color": INK_2, "size": 13},
        # with a subtitle the legend sits under the plot, so the two never collide
        legend=(
            {"orientation": "h", "y": -0.12, "x": 0, "xanchor": "left", "yanchor": "top", "title": None}
            if subtitle
            else {
                "orientation": "h",
                "y": 1.02,
                "x": 1,
                "xanchor": "right",
                "yanchor": "bottom",
                "title": None,
            }
        ),
        hoverlabel={"bgcolor": "white"},
        bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="", title=None)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, title=None)
    return fig


def chart(fig: go.Figure, key: str) -> None:
    st.plotly_chart(fig, width="stretch", key=key, config={"displayModeBar": False})
