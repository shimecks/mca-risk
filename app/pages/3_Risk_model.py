"""Risk model: how well the origination model ranks deals, whether its probabilities can be trusted,
and what not to use it for.

Model numbers come from ``model_performance`` / ``model_lift`` / ``model_calibration`` /
``model_naive_comparison``; the observable-ceiling AUC is the E8 eval's report-only metric
(``eval_latest.json``). The "what not to use it for" list is quoted from ``model_card.md``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import plotly.graph_objects as go
import streamlit as st

c.page_setup("Risk model")
c.require_data()

perf = c.query("SELECT model, trained_on, eval_target, split, metric, value FROM model_performance")


def pv(model: str, metric: str, split: str = "test", target: str = "economic"):
    r = perf[
        (perf.model == model) & (perf.split == split) & (perf.metric == metric) & (perf.eval_target == target)
    ]
    return None if r.empty else float(r.value.iloc[0])


rep, _ = c.eval_report()
e8 = next((s for s in (rep or {}).get("suites", []) if s.get("suite") == "E8"), {})
ceiling = (e8.get("metrics") or {}).get("observable_ceiling_auc")
slope_raw = (e8.get("metrics") or {}).get("calibration_slope_uncalibrated") or pv(
    "logit_uncalibrated", "calibration_slope"
)

auc = pv("logit", "auc")
slope = pv("logit", "calibration_slope")
lift = pv("logit", "top_decile_lift")
base = pv("logit", "bad_rate")
top_rate = pv("logit", "top_decile_bad_rate")
n_test = pv("logit", "n")

st.title("Risk model")
st.markdown(
    "A score for **new deals at the funding date**: how likely a deal is to go bad within 180 business "
    "days, using only what an underwriter knows when funding (credit score, revenue, other advances, "
    "time in business, terms). Tested on deals funded after the ones it learned from."
)

cols = st.columns(3)
close = ceiling is not None and auc >= ceiling - 0.03
c.card(
    cols[0],
    "How well it ranks risky deals",
    c.num(auc, 3),
    f"vs **{c.num(ceiling, 3)}**, the best this data allows" if ceiling else "AUC on later deals",
    ("Close to the ceiling. " if close else "")
    + "AUC: 0.5 is a coin flip, 1.0 is perfect. Much of the risk isn't visible at funding, which caps it.",
    c.spec(
        "T1",
        note="Ranking quality (AUC) on the temporal test split. The ceiling is what the simulator's own "
        "coefficients reach on the same observable inputs (eval E8).",
    ),
)
c.card(
    cols[1],
    "Can its probabilities be trusted?",
    c.num(slope, 2),
    "calibration slope; **1.00** is perfect",
    f"Mostly, after re-tuning ({c.num(slope_raw, 2)} before). Caveat: the re-tuning method was picked "
    "after seeing the test results, and predictions still run high.",
    c.spec("T1", note="Calibration slope on the test split after a validation-fitted recalibration."),
)
c.card(
    cols[2],
    "Riskiest 10% of deals",
    f"{c.num(lift, 1)}x",
    f"as likely to go bad: **{c.pct(top_rate, 0)}** vs **{c.pct(base, 0)}** on average",
    f"Useful for sending the riskiest applications to a closer review. {int(n_test or 0)} test deals.",
    c.spec("T1", note="Top-decile lift on the test split."),
)

# ---------------------------------------------------------------------------- what not to use it for
card_md = c.read_text("model_card.md") or ""
m = re.search(r"## What NOT to use it for \(yet\)\n(.*?)(?=\n## )", card_md, flags=re.S)
bullets = [b[2:].strip() for b in (m.group(1).strip().splitlines() if m else []) if b.startswith("- ")]
with st.container(border=True):
    st.markdown("**What not to use it for (yet)**")
    for b in bullets:
        b = re.sub(r"\*\*Not\*\*", "Not", b).replace("`", "")
        st.markdown(f"- {c.mdx(b)}")
    st.caption(
        "From the model card. It is a decision aid for a human underwriter, not an approve / decline rule."
    )

st.divider()

# ---------------------------------------------------------------------------- charts
left, right = st.columns(2, gap="large")
with left:
    lt = c.query(
        "SELECT decile, bad_rate, n FROM model_lift WHERE model = 'logit' AND split = 'test' ORDER BY decile"
    )
    fig = go.Figure()
    fig.add_bar(
        x=lt.decile.astype(str),
        y=lt.bad_rate,
        marker={"color": [c.CASH if d == 1 else c.CASH_LIGHT for d in lt.decile], "cornerradius": 4},
        text=[c.pct(v, 0) if d == 1 else "" for d, v in zip(lt.decile, lt.bad_rate, strict=True)],
        textposition="outside",
        textfont={"color": c.INK},
        customdata=lt.n,
        hovertemplate="Risk group %{x}: %{y:.0%} went bad (%{customdata} deals)<extra></extra>",
    )
    fig.add_hline(
        y=base,
        line={"color": c.TRACKER, "width": 1.5},
        annotation_text=f"average {c.pct(base, 0)}",
        annotation_position="top right",
        annotation_font={"color": c.INK_2},
    )
    c.style(
        fig,
        f"Deals it scores riskiest went bad {c.pct(top_rate, 0)} of the time",
        height=340,
        subtitle="Test deals in 10 equal groups, riskiest score first",
    )
    fig.update_yaxes(tickformat=".0%", range=[0, max(lt.bad_rate) * 1.2])
    fig.update_xaxes(title="Risk group (1 = riskiest 10%)", type="category")
    fig.update_layout(showlegend=False)
    c.chart(fig, "lift")

with right:
    cal = c.query(
        "SELECT bin, mean_pred, obs_rate, n FROM model_calibration WHERE model = 'logit' AND split = 'test' ORDER BY bin"
    )
    top = float(max(cal.mean_pred.max(), cal.obs_rate.max())) * 1.1
    fig = go.Figure()
    fig.add_scatter(
        x=[0, top],
        y=[0, top],
        mode="lines",
        name="Perfect",
        line={"color": c.MUTED, "width": 1.5},
        hoverinfo="skip",
    )
    fig.add_scatter(
        x=cal.mean_pred,
        y=cal.obs_rate,
        mode="markers",
        name="Model",
        marker={"color": c.CASH, "size": 10},
        customdata=cal.n,
        hovertemplate="Predicted %{x:.0%}, observed %{y:.0%} (%{customdata} deals)<extra></extra>",
    )
    fig.add_annotation(
        x=top * 0.82, y=top * 0.9, text="perfect", showarrow=False, font={"color": c.INK_2, "size": 12}
    )
    c.style(
        fig,
        "Predictions follow the real rate but run a bit high",
        height=340,
        subtitle="Test deals in 10 groups: predicted vs observed share that went bad",
    )
    fig.update_xaxes(tickformat=".0%", range=[0, top], title="Predicted", showgrid=True, gridcolor=c.GRID)
    fig.update_yaxes(tickformat=".0%", range=[0, top], title="Observed")
    fig.update_layout(showlegend=False)
    c.chart(fig, "calibration")

nv = c.query("SELECT model, metric, value FROM model_naive_comparison")
g = {(r.model, r.metric): r.value for r in nv.itertuples()}
obs = g.get(("logit", "observed_economic_bad_rate"))
fig = go.Figure()
rows = [
    ("Trained on the tracker's view", g.get(("logit_naive", "mean_pred")), c.TRACKER),
    ("Trained on the cash view", g.get(("logit", "mean_pred")), c.CASH),
]
fig.add_bar(
    y=[r[0] for r in rows],
    x=[r[1] for r in rows],
    orientation="h",
    marker={"color": [r[2] for r in rows], "cornerradius": 4},
    text=[c.pct(r[1], 0) for r in rows],
    textposition="outside",
    textfont={"color": c.INK, "size": 14},
    hovertemplate="%{y}: predicts %{x:.1%} on average<extra></extra>",
    width=0.5,
)
fig.add_vline(
    x=obs,
    line={"color": c.INK, "width": 1.5},
    annotation_text=f"actually went bad: {c.pct(obs, 0)}",
    annotation_position="top",
    annotation_font={"color": c.INK},
)
c.style(
    fig,
    "A model trained on the tracker's view under-predicts risk",
    height=250,
    subtitle="Average predicted chance of going bad, test deals. Both models rank deals about equally well.",
)
fig.update_xaxes(visible=False, range=[0, max(r[1] for r in rows) * 1.3])
fig.update_yaxes(showgrid=False, autorange="reversed")
fig.update_layout(showlegend=False, margin={"t": 90})
c.chart(fig, "naive")
st.caption(
    "The tracker's view treats a distressed refinance as a good outcome, so a model trained on it learns "
    f"too little risk: on distressed refinances it predicts {c.pct(g.get(('logit_naive', 'mean_pred_distressed_refi')), 0)} "
    f"vs {c.pct(g.get(('logit', 'mean_pred_distressed_refi')), 0)}. Used for pricing or reserves it would "
    "understate risk. Spec: D18 vs D19 target."
)

# ---------------------------------------------------------------------------- detail
st.divider()
with st.expander("Every model compared (test and validation)"):
    tbl = perf[(perf.eval_target == "economic") & perf.split.isin(["val", "test"])].pivot_table(
        index="model", columns=["split", "metric"], values="value", aggfunc="first"
    )
    names = {
        "auc": "AUC",
        "brier": "Brier",
        "calibration_slope": "calibration slope",
        "top_decile_lift": "top-10% lift",
        "n": "deals",
        "bad_rate": "bad rate",
    }
    keep = [(s, k) for s in ("test", "val") for k in names if (s, k) in tbl.columns]
    tbl = tbl[keep]
    tbl.columns = [f"{'Test' if s == 'test' else 'Validation'} {names[k]}" for s, k in tbl.columns]
    label = {
        "logit": "Logistic (the model above)",
        "logit_uncalibrated": "Logistic, before re-tuning",
        "logit_naive": "Logistic on the tracker's view",
        "gbm": "Gradient boosting",
        "gbm_isotonic": "Gradient boosting, re-tuned",
        "baseline_rule": "Simple rule (positions + burden)",
    }
    tbl = tbl.reindex([m for m in label if m in tbl.index])
    tbl.index = [label[m] for m in tbl.index]
    st.dataframe(tbl.style.format(precision=3), width="stretch")
with st.expander("What drives the score (odds ratios)"):
    c.show_figure("model_coefficients", "Above 1 raises risk, below 1 lowers it; lines are 95% ranges.")
with st.expander("Time to going bad (survival curves) and merchant-level model"):
    c.show_figure("survival_km", "Share of deals not yet bad, by days since funding (spec T2).")
    t3 = c.query("SELECT key, value FROM t3_summary")
    if not t3.empty:
        d = dict(zip(t3.key, t3.value, strict=True))
        st.markdown(
            f"**Merchant-level model (spec T3):** {d.get('n_population')} merchants finished within 18 months, "
            f"{c.pct(float(d.get('bad_rate', 'nan')))} lost money; a first-deal model ranks them with AUC "
            f"{c.num(float(d.get('auc_test', 'nan')), 3)} on later merchants."
        )
        coefs = json.loads(d.get("coefficients", "{}"))
        st.caption("T3 coefficients: " + ", ".join(f"{k} {v:+.2f}" for k, v in coefs.items()))
with st.expander("Full model card"):
    st.markdown(c.cite(card_md) if card_md else "model_card.md missing from the snapshot")
c.footer()
