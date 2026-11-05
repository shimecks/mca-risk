"""Data quality: rule hits with sample rows, the exceptions (human review) queue, row accounting, run manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import streamlit as st

c.page_setup("Data quality")
c.require_data()

st.title("Data quality")
st.caption(
    "Every raw row is loaded, rejected with a reason, or repaired with a log entry (Q7). Every link stores a "
    "method and a confidence. What the pipeline could not resolve automatically goes to the exceptions queue."
)

dq = c.query(
    "SELECT rule_id, rule, owner_part, status, hits, denominator, detail, sample_rows FROM data_quality"
)
dq = dq.sort_values("rule_id", key=lambda s: s.map(lambda r: ("QLS".index(r[0]), int(r[1:]))))
exc = c.query("SELECT * FROM exceptions ORDER BY exception_id")

cols = st.columns(4)
cols[0].metric("Rules evaluated", f"{len(dq)}")
cols[1].metric("Exceptions queued", f"{len(exc)}")
rej = c.query("SELECT COUNT(*) AS n FROM raw_rejects").n.iloc[0]
cols[2].metric("Raw rows rejected (with reason)", f"{rej}")
l3 = dq[dq.rule_id == "L3"].hits
cols[3].metric("Mispostings repaired (L3)", f"{int(l3.iloc[0]) if len(l3) else 0}")

st.subheader("Rule hits")
view = dq[["rule_id", "rule", "hits", "denominator", "owner_part", "detail"]].copy()
view["rate"] = (view.hits / view.denominator).map(lambda x: c.pct(x, 2))
st.dataframe(view, hide_index=True, width="stretch")
st.markdown(
    "Definitions: "
    + c.links([r for r in dq.rule_id if r[0] in "QL"])
    + ". S1-S6 are consistency checks on the "
    "standardized tables (counted, values unchanged)."
)

st.subheader("Sample rows per rule")
for r in dq.itertuples():
    samples = json.loads(r.sample_rows) if r.sample_rows else []
    label = f"{r.rule_id} · {r.rule} · {int(r.hits) if pd.notna(r.hits) else 'n/a'} hits · {len(samples)} sample rows"
    with st.expander(label):
        st.markdown(c.cite(str(r.detail)))
        if samples:
            st.dataframe(pd.DataFrame(samples), hide_index=True, width="stretch")
        else:
            st.caption("No sample rows for this rule (no hits, or a rule that summarises a whole stage).")

st.subheader("Exceptions queue (human review)")
rules = sorted(exc.rule_id.unique()) if not exc.empty else []
pick = st.multiselect("Rule", rules, default=rules, key="exc_rules")
st.dataframe(exc[exc.rule_id.isin(pick)], hide_index=True, width="stretch")
st.caption(f"{len(exc)} exceptions. Each keeps the source row it came from (src_file, src_sheet, src_row).")

st.subheader("Row accounting")
a1, a2 = st.columns(2)
with a1:
    st.markdown("**Raw -> standardized** (`std_accounting`)")
    st.dataframe(c.query("SELECT * FROM std_accounting"), hide_index=True, width="stretch")
    st.markdown("**Rejected raw rows** (`raw_rejects`)")
    st.dataframe(
        c.query("SELECT src_file, src_sheet, src_row, reason, detail FROM raw_rejects"),
        hide_index=True,
        width="stretch",
    )
with a2:
    st.markdown("**Standardized -> ledger** (`ledger_accounting`)")
    st.dataframe(c.query("SELECT * FROM ledger_accounting"), hide_index=True, width="stretch")
    st.markdown("**Rejected ledger rows** (`ledger_rejects`)")
    st.dataframe(c.query("SELECT * FROM ledger_rejects"), hide_index=True, width="stretch")
with st.expander("Raw files ingested (`raw_sources`, with SHA-256)"):
    st.dataframe(c.query("SELECT * FROM raw_sources ORDER BY src_file"), hide_index=True, width="stretch")

st.subheader("Run manifest")
man = c.read_json("run_manifest.json") or {}
if man:
    git = man.get("git", {})
    st.markdown(
        f"Run `{man.get('run_id')}` · git `{git.get('sha')}`{' (dirty)' if git.get('dirty') else ''} · "
        f"METRICS.md {man.get('metrics_md', {}).get('version')} · {len(man.get('inputs', {}))} input files hashed · "
        f"total {man.get('totals', {}).get('seconds')} s"
    )
    stages = []
    for name, s in man.get("stages", {}).items():
        tables = s.get("tables", {})
        stages.append(
            {
                "stage": name,
                "status": s.get("status"),
                "seconds": s.get("seconds"),
                "tables": len(tables),
                "rows accepted": sum((t.get("accepted") or 0) for t in tables.values()),
                "rows rejected": sum((t.get("rejected") or 0) for t in tables.values()),
                "rows repaired": sum((t.get("repaired") or 0) for t in tables.values()),
            }
        )
    st.dataframe(pd.DataFrame(stages), hide_index=True, width="stretch")
    llm = man.get("llm", {})
    st.caption(
        f"LLM (optional notes extractor): {llm.get('calls', 0)} calls, {llm.get('cache_hits', 0)} cache hits, "
        f"cost {llm.get('cost_usd', 0)} USD in this run. The app itself never calls an API."
    )
    with st.expander("Full manifest (JSON)"):
        st.json(man, expanded=1)
else:
    st.warning("run_manifest.json missing from the snapshot.")

with st.expander("Data-quality report (build/dq_report.md)"):
    txt = c.read_text("dq_report.md")
    st.markdown(c.cite(txt) if txt else "dq_report.md missing from the snapshot")
