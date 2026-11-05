"""Behind the numbers: data-quality checks, the eval report, metric definitions and the run manifest.

Data quality reads ``data_quality`` / ``exceptions`` / accounting tables; tests read the latest eval report
against ``evals/thresholds.json``; definitions index ``METRICS.md`` (``?id=D17`` opens one).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import streamlit as st

c.page_setup("Behind the numbers")

st.title("Behind the numbers")
st.markdown(
    "How the raw spreadsheets were cleaned, how every number was checked, and exactly what each metric means."
)

qid = str(st.query_params.get("id", "")).upper()
t_dq, t_ev, t_def, t_run = st.tabs(["Data quality", "Tests", "Definitions", "Run details"])

# ---------------------------------------------------------------------------- data quality
RULE = {
    "Q1": "Same merchant matched across files",
    "Q2": "Renewals linked to the deal they replaced",
    "Q3": "Underwriting scorecards matched to deals",
    "Q4": "Collections entries matched to deals",
    "Q5": "Deals with bank activity but no tracker row",
    "Q6": "Tracker status out of date",
    "Q7": "Every raw row accounted for",
    "L1": "Payoffs: cash vs paid by a renewal",
    "L2": "Duplicate bank rows removed",
    "L3": "Payments posted to the wrong deal, fixed",
}
with t_dq:
    if not c.data_available():
        st.error("No data snapshot. Run `make all eval app-data`.")
    else:
        dq = c.query(
            "SELECT rule_id, rule, owner_part, status, hits, denominator, detail, sample_rows FROM data_quality"
        )
        dq = dq.sort_values("rule_id", key=lambda s: s.map(lambda r: ("QLS".index(r[0]), int(r[1:]))))
        exc = c.query("SELECT * FROM exceptions ORDER BY exception_id")
        by = dq.set_index("rule_id")

        def hits(rid: str) -> int:
            return int(by.hits.get(rid, 0) or 0)

        l2 = str(by.detail.get("L2", ""))
        trap = re.search(r"(\d[\d,]*) of them pair", l2)
        n_files = len(c.query("SELECT DISTINCT src_file FROM raw_sources"))
        cols = st.columns(3)
        c.card(
            cols[0],
            "Raw rows accounted for",
            f"{int(by.denominator.get('Q7', 0) or 0):,}",
            f"from **{n_files}** files",
            "Every row is loaded, rejected with a reason, or repaired with a log entry.",
            c.spec("Q7"),
        )
        c.card(
            cols[1],
            "Misposted payments fixed",
            f"{hits('L3'):,}",
            "debits posted to a paid-off deal",
            "Moved to the live renewal they belong to.",
            c.spec("L3"),
        )
        c.card(
            cols[2],
            "Duplicate bank rows removed",
            f"{hits('L2'):,}",
            f"**{trap.group(1)}** would fool a naive dedupe" if trap else "overlapping exports",
            "Overlapping exports; some switch date format mid-stream (MM/DD/YYYY vs YYYY-MM-DD).",
            c.spec("L2"),
        )
        cols = st.columns(3)
        c.card(
            cols[0],
            "Deals missing from the tracker",
            f"{hits('Q5'):,}",
            "rebuilt from bank activity",
            "Their terms and commission are unknown, so their losses are a floor.",
            c.spec("Q5"),
        )
        c.card(
            cols[1],
            "Tracker statuses out of date",
            f"{hits('Q6'):,}",
            f"of **{int(by.denominator.get('Q6', 0) or 0):,}** tracker deals",
            "The status typed in the tracker disagrees with what the cash shows happened.",
            c.spec("Q6"),
        )
        c.card(
            cols[2],
            "Queued for human review",
            f"{len(exc):,}",
            "matches the pipeline wasn't sure about",
            "Every automatic link stores how it was found and how confident it is.",
            c.spec("L1", "Q2"),
        )

        st.markdown("**All checks**")
        view = pd.DataFrame(
            {
                "Check": [RULE.get(r, t) for r, t in zip(dq.rule_id, dq.rule, strict=True)],
                "Found": dq.hits.map(lambda x: "" if pd.isna(x) else f"{int(x):,}"),
                "Out of": dq.denominator.map(lambda x: "" if pd.isna(x) else f"{int(x):,}"),
                "Spec": dq.rule_id,
            }
        )
        st.dataframe(view, hide_index=True, width="stretch", key="dq_rules")
        st.caption("S1-S6 are consistency checks on the cleaned tables: counted, values unchanged.")

        with st.expander("Details and sample rows for each check"):
            for r in dq.itertuples():
                samples = json.loads(r.sample_rows) if r.sample_rows else []
                st.markdown(f"**{r.rule_id} · {RULE.get(r.rule_id, r.rule)}**")
                st.caption(c.mdx(str(r.detail)))
                if samples:
                    st.dataframe(pd.DataFrame(samples), hide_index=True, width="stretch")
        with st.expander(f"Human review queue ({len(exc)})"):
            rules = sorted(exc.rule_id.unique()) if not exc.empty else []
            pick = st.multiselect("Check", rules, default=rules, key="exc_rules")
            st.dataframe(exc[exc.rule_id.isin(pick)], hide_index=True, width="stretch")
        with st.expander("Row accounting"):
            a1, a2 = st.columns(2)
            with a1:
                st.markdown("**Raw → cleaned**")
                st.dataframe(c.query("SELECT * FROM std_accounting"), hide_index=True, width="stretch")
                st.markdown("**Rejected raw rows**")
                st.dataframe(
                    c.query("SELECT src_file, src_sheet, src_row, reason, detail FROM raw_rejects"),
                    hide_index=True,
                    width="stretch",
                )
            with a2:
                st.markdown("**Cleaned → cash ledger**")
                st.dataframe(c.query("SELECT * FROM ledger_accounting"), hide_index=True, width="stretch")
                st.markdown("**Rejected ledger rows**")
                st.dataframe(c.query("SELECT * FROM ledger_rejects"), hide_index=True, width="stretch")
            st.markdown("**Raw files ingested (with SHA-256)**")
            st.dataframe(
                c.query("SELECT * FROM raw_sources ORDER BY src_file"), hide_index=True, width="stretch"
            )
        with st.expander("Full data-quality report"):
            txt = c.read_text("dq_report.md")
            st.markdown(c.cite(txt) if txt else "dq_report.md missing from the snapshot")

# ---------------------------------------------------------------------------- tests
PLAIN = {
    "E1": "Every raw row accounted for",
    "E2": "Fields read correctly from messy files",
    "E3": "No duplicate or missing payments",
    "E4": "Same merchant matched across files",
    "E5": "Renewals and payoffs linked correctly",
    "E6": "Deal outcomes classified correctly",
    "E7": "Metrics match an independent calculation",
    "E8": "Risk model quality",
    "E9": "Optional notes reader (LLM)",
}


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, list | dict):
        s = json.dumps(v)
        return s if len(s) <= 120 else s[:117] + "..."
    return str(v)


with t_ev:
    rep, source = c.eval_report()
    thr = c.thresholds()
    if not rep:
        st.error("No eval report found. Run `make eval` (then `make app-data` to refresh the snapshot).")
    else:
        suites = rep.get("suites", [])
        n_pass = sum(s.get("status") == "pass" for s in suites)
        (st.success if rep.get("overall") == "pass" else st.error)(
            f"**{n_pass} of {len(suites)} test suites pass.** Each one compares a pipeline stage with the "
            "simulator's hidden answers, which only the test harness can read."
        )
        rows = []
        for s in suites:
            gates = s.get("gates") or {}
            rows.append(
                {
                    "Test": PLAIN.get(s.get("suite"), s.get("name")),
                    "Result": "pass" if s.get("status") == "pass" else "FAIL",
                    "Gates met": f"{sum(bool(g.get('passed')) for g in gates.values())} of {len(gates)}",
                    "Target": s.get("plan_gate"),
                    "Suite": s.get("suite"),
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", key="eval_summary")
        st.caption(f"Run {rep.get('timestamp')} · git {rep.get('git_sha')} · source `{source}`.")

        changes = [
            (sid, t)
            for sid, t in thr.items()
            if isinstance(t, dict) and (t.get("gate_change") or t.get("comparison_basis"))
        ]
        if changes:
            with st.expander("Test targets the user changed, and why"):
                for sid, t in changes:
                    if t.get("gate_change"):
                        st.warning(c.mdx(f"**{sid} gate change.** {t['gate_change']}"))
                    if t.get("comparison_basis"):
                        st.info(c.mdx(f"**{sid} comparison basis.** {t['comparison_basis']}"))
        for s in suites:
            sid = s.get("suite")
            t = thr.get(sid, {})
            with st.expander(f"{sid} {PLAIN.get(sid, s.get('name'))}: {s.get('status')}"):
                gates = s.get("gates") or {}
                if gates:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Measure": k,
                                    "Value": _fmt(g.get("value")),
                                    "Needs": f"{g.get('op')} {_fmt(g.get('target'))}",
                                    "Met": "yes" if g.get("passed") else "NO",
                                }
                                for k, g in gates.items()
                            ]
                        ),
                        hide_index=True,
                        width="stretch",
                    )
                ro = [k for k in t.get("report_only", []) if k in (s.get("metrics") or {})]
                if ro:
                    st.markdown("Reported, not gated:")
                    st.dataframe(
                        pd.DataFrame([{"Measure": k, "Value": _fmt(s["metrics"][k])} for k in ro]),
                        hide_index=True,
                        width="stretch",
                    )
                for n in s.get("notes", []):
                    st.markdown(f"- {c.mdx(str(n))}")

# ---------------------------------------------------------------------------- definitions
with t_def:
    md = c.metrics_md()
    if not md:
        st.error("METRICS.md not found next to the app.")
    else:
        idx = c.metric_index(md)
        order = sorted(idx, key=lambda k: ("DRPTLQ".index(k[0]), int(k[1:])))
        version = md.splitlines()[0].split("(")[-1].rstrip(")") if md.startswith("#") else "?"
        st.caption(
            f"METRICS.md {version} is the spec for every number in this app. The short codes (D7, P2...) "
            "in tooltips refer to it: D = one deal, R = one merchant relationship, P = the portfolio, "
            "T = model targets, L = linking rules, Q = data-quality rules."
        )
        sel = st.selectbox(
            "Look up a metric",
            order,
            index=order.index(qid) if qid in idx else None,
            format_func=lambda k: f"{k}: {idx[k]['name']}",
            placeholder="Pick a metric or rule",
            key="lookup",
        )
        if sel:
            e = idx[sel]
            st.success(c.mdx(f"**{sel}: {e['name']}**  \n{e['definition']}  \n*METRICS.md, {e['section']}*"))
        elif qid:
            st.warning(f"`{qid}` is not a metric ID in METRICS.md.")
        with st.expander(f"All {len(order)} definitions"):
            for k in order:
                e = idx[k]
                st.subheader(f"{k}: {e['name']}", anchor=k.lower())
                st.markdown(c.mdx(e["definition"]) + f"  \n*{e['section']}*")
        with st.expander("METRICS.md, full text"):
            st.markdown(c.mdx(md))

# ---------------------------------------------------------------------------- run details
with t_run:
    man = c.read_json("run_manifest.json") or {}
    meta = c.app_meta()
    if man:
        git = man.get("git", {})
        st.markdown(
            c.mdx(
                f"Pipeline run `{man.get('run_id')}` · git `{str(git.get('sha'))[:10]}`"
                f"{' (uncommitted changes)' if git.get('dirty') else ''} · METRICS.md "
                f"{man.get('metrics_md', {}).get('version')} · {len(man.get('inputs', {}))} input files hashed · "
                f"{man.get('totals', {}).get('seconds')} s end to end, no network."
            )
        )
        stages = []
        for name, s in man.get("stages", {}).items():
            tables = s.get("tables", {})
            stages.append(
                {
                    "Stage": name,
                    "Status": s.get("status"),
                    "Seconds": s.get("seconds"),
                    "Tables": len(tables),
                    "Rows accepted": sum((t.get("accepted") or 0) for t in tables.values()),
                    "Rows rejected": sum((t.get("rejected") or 0) for t in tables.values()),
                    "Rows repaired": sum((t.get("repaired") or 0) for t in tables.values()),
                }
            )
        st.dataframe(pd.DataFrame(stages), hide_index=True, width="stretch", key="stages")
        llm = man.get("llm", {})
        st.caption(
            f"Optional notes LLM: {llm.get('calls', 0)} calls, {llm.get('cache_hits', 0)} cache hits, "
            f"${llm.get('cost_usd', 0)} in this run. The app itself never calls an API."
        )
        with st.expander("Full run manifest (JSON)"):
            st.json(man, expanded=1)
    else:
        st.warning("run_manifest.json missing from the snapshot.")
    st.caption(
        c.mdx(
            f"App snapshot: {len(meta.get('tables', {}))} tables ({meta.get('db_bytes', 0) / 1e6:.1f} MB, "
            f"read-only), {len(meta.get('figures', {}))} figures, exported {meta.get('exported_at', '?')}."
        )
    )
