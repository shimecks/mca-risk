"""Evals: the latest eval report (E1-E9) against evals/thresholds.json, incl. report-only metrics and gate changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import streamlit as st

c.page_setup("Evals")

st.title("Evals")
rep, source = c.eval_report()
thr = c.thresholds()
if not rep:
    st.error("No eval report found. Run `make eval` (then `make app-data` to refresh the snapshot).")
    st.stop()

st.caption(
    f"Source: `{source}` · run `{rep.get('timestamp')}` · git `{rep.get('git_sha')}`. Gates: "
    "`evals/thresholds.json`. Each suite compares a pipeline stage with the simulator's truth tables; only "
    "`evals/` reads them, the pipeline and this app never do."
)
overall = rep.get("overall")
(st.success if overall == "pass" else st.error)(
    f"Overall: **{overall}** ({len(rep.get('suites', []))} suites)"
)


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, list | dict):
        s = json.dumps(v)
        return s if len(s) <= 120 else s[:117] + "..."
    return str(v)


summary = []
for s in rep.get("suites", []):
    gates = s.get("gates") or {}
    summary.append(
        {
            "suite": s.get("suite"),
            "name": s.get("name"),
            "status": s.get("status"),
            "gates passed": f"{sum(bool(g.get('passed')) for g in gates.values())} / {len(gates)}",
            "plan gate": s.get("plan_gate"),
            "gate change": "yes" if (thr.get(s.get("suite"), {}).get("gate_change")) else "",
        }
    )
st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")

changes = [
    (sid, t)
    for sid, t in thr.items()
    if isinstance(t, dict) and (t.get("gate_change") or t.get("comparison_basis"))
]
if changes:
    st.subheader("User-approved gate changes and comparison bases")
    for sid, t in changes:
        if t.get("gate_change"):
            st.warning(f"**{sid} gate change.** {t['gate_change']}")
        if t.get("comparison_basis"):
            st.info(f"**{sid} comparison basis.** {t['comparison_basis']}")

st.subheader("Suites")
for s in rep.get("suites", []):
    sid = s.get("suite")
    t = thr.get(sid, {})
    with st.expander(f"{sid} {s.get('name')}: {s.get('status')}", expanded=sid in ("E6", "E8")):
        gates = s.get("gates") or {}
        if gates:
            st.markdown("**Gated metrics**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "metric": k,
                            "value": _fmt(g.get("value")),
                            "gate": f"{g.get('op')} {_fmt(g.get('target'))}",
                            "pass": "yes" if g.get("passed") else "NO",
                        }
                        for k, g in gates.items()
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        ro = [k for k in t.get("report_only", []) if k in (s.get("metrics") or {})]
        if ro:
            st.markdown("**Report-only metrics** (shown, not gated)")
            st.dataframe(
                pd.DataFrame([{"metric": k, "value": _fmt(s["metrics"][k])} for k in ro]),
                hide_index=True,
                width="stretch",
            )
        for n in s.get("notes", []):
            st.markdown(f"- {c.mdx(str(n))}")
        st.caption(f"Plan gate: {s.get('plan_gate')} · {s.get('seconds', '?')} s")
