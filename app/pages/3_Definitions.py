"""Definitions: METRICS.md rendered, plus a link target (``?id=D7``, anchor ``#d7``) for every metric ID."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import streamlit as st

c.page_setup("Definitions")

md = c.metrics_md()
if not md:
    st.error("METRICS.md not found next to the app.")
    st.stop()
idx = c.metric_index(md)
order = sorted(idx, key=lambda k: ("DRPTLQ".index(k[0]), int(k[1:])))
version = md.splitlines()[0].split("(")[-1].rstrip(")") if md.startswith("#") else "?"

st.title("Definitions")
st.caption(
    f"METRICS.md {version}: the single source of truth for every number in this app. Every metric ID on the "
    "other pages links here (`Definitions?id=D7`)."
)

qid = str(st.query_params.get("id", "")).upper()
sel = st.selectbox(
    "Look up a metric or rule",
    order,
    index=order.index(qid) if qid in idx else None,
    format_func=lambda k: f"{k}: {idx[k]['name']}",
    placeholder="Pick an ID (D1-D19, R1-R10, P1-P9, T1-T3, L1-L3, Q1-Q7)",
    key="lookup",
)
if sel:
    e = idx[sel]
    st.success(f"**{sel}: {e['name']}**  \n{c.cite(e['definition'])}  \n*METRICS.md, {e['section']}*")
elif qid:
    st.warning(f"`{qid}` is not a metric ID in METRICS.md.")

st.markdown("Jump to: [METRICS.md full text](#metrics-md-full-text) · [Metric index](#metric-index)")
st.header("Metric index", anchor="metric-index", divider=True)
st.caption("One entry (and one URL anchor, e.g. `#d7`) per ID, quoted from METRICS.md.")
for k in order:
    e = idx[k]
    st.subheader(f"{k}: {e['name']}", anchor=k.lower())
    st.markdown(c.cite(e["definition"]) + f"  \n*{e['section']}*")

st.header("METRICS.md full text", anchor="metrics-md-full-text", divider=True)
st.markdown(c.mdx(md))
