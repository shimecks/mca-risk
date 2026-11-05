"""Model: T1 risk model performance, calibration, lift, naive vs economic target, survival curves, model card."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import common as c
import streamlit as st

c.page_setup("Model")
c.require_data()

st.title("Risk model")
st.caption(
    f"T1 target: economic bad ({c.link('D19')}) within 180 business days of funding ({c.link('T1')}); "
    f"origination-only features; temporal split. Survival: {c.link('T2')}. Relationships: {c.link('T3')}."
)

perf = c.query("SELECT model, trained_on, eval_target, split, metric, value FROM model_performance")


def pv(model: str, split: str, metric: str, target: str = "economic"):
    r = perf[
        (perf.model == model) & (perf.split == split) & (perf.metric == metric) & (perf.eval_target == target)
    ]
    return None if r.empty else float(r.value.iloc[0])


cols = st.columns(4)
cols[0].metric("Test AUC (primary logit)", c.num(pv("logit", "test", "auc"), 3))
cols[1].metric("Calibration slope (test)", c.num(pv("logit", "test", "calibration_slope"), 2))
cols[2].metric("Slope before recalibration", c.num(pv("logit_uncalibrated", "test", "calibration_slope"), 2))
cols[3].metric("Top-decile lift (test)", c.num(pv("logit", "test", "top_decile_lift"), 2))
st.info(
    "**Read with care.** The E8 AUC gate is relative to the observable ceiling (user-approved): the test AUC "
    "is compared with what the simulator's own coefficients achieve on observable inputs (see the Evals page "
    "for the ceiling). The validation-fitted recalibration was chosen **after** the test split had been read "
    "(user-approved and disclosed), so the recalibrated test slope is not a clean out-of-sample result; the "
    "uncalibrated slope is shown next to it."
)

st.subheader("Every model (economic T1 target)")
tbl = perf[(perf.eval_target == "economic") & perf.split.isin(["val", "test"])].pivot_table(
    index="model", columns=["split", "metric"], values="value", aggfunc="first"
)
keep = [
    (s, m)
    for s in ("val", "test")
    for m in (
        "n",
        "bad_rate",
        "auc",
        "brier",
        "calibration_slope",
        "calibration_in_the_large",
        "top_decile_lift",
    )
    if (s, m) in tbl.columns
]
tbl = tbl[keep]
tbl.columns = [f"{s} {m}" for s, m in tbl.columns]
order = ["baseline_rule", "logit", "logit_uncalibrated", "gbm", "gbm_isotonic", "logit_naive"]
tbl = tbl.reindex([m for m in order if m in tbl.index] + [m for m in tbl.index if m not in order])
st.dataframe(tbl.style.format(precision=3), width="stretch")
st.caption(
    "`logit` is the primary model (validation-recalibrated); `logit_naive` is trained on the naive D18 target."
)

t_cal, t_lift, t_naive, t_coef, t_surv = st.tabs(
    ["Calibration", "Lift", "Naive vs economic target", "Coefficients", "Survival (T2) and T3"]
)
with t_cal:
    c.show_figure("model_calibration", "Reliability on the temporal test split.")
    cal = c.query("SELECT * FROM model_calibration WHERE model = 'logit' ORDER BY split, bin")
    st.dataframe(cal, hide_index=True, width="stretch")
with t_lift:
    c.show_figure("model_lift", "Bad rate by score decile on the temporal test split.")
with t_naive:
    c.show_figure("model_naive_vs_economic")
    nv = c.query(
        "SELECT model, metric_label, value, k, n, n_distressed_refi, sort_key FROM model_naive_comparison"
    )
    wide = nv.pivot_table(
        index=["sort_key", "metric_label"], columns="model", values="value", aggfunc="first"
    )
    wide = wide.reset_index(level=0, drop=True)
    st.dataframe(wide.style.format(precision=3), width="stretch")
    st.markdown(
        "On this book a model trained on the **naive** target ranks deals about as well as the economic one "
        "(similar AUC and the same recall of distressed refinances in the top k). The difference is the "
        "**level**: the naive model under-predicts economic risk, most on the distressed refinances that "
        f"renewals hide ({c.links(['D17', 'D18', 'D19'])})."
    )
with t_coef:
    c.show_figure("model_coefficients")
    co = c.query(
        "SELECT model, term, coef, std_err, p_value, odds_ratio, or_ci_low, or_ci_high FROM model_coefficients "
        "ORDER BY model, term_order"
    )
    st.dataframe(co.style.format(precision=3), hide_index=True, width="stretch")
with t_surv:
    c.show_figure("survival_km", "Kaplan-Meier, time to the D19 event, by segment.")
    cox = c.query("SELECT * FROM survival_cox")
    if not cox.empty:
        st.markdown(
            f"**Cox proportional hazards** (descriptive, all deals), concordance {c.num(cox.concordance.iloc[0], 3)}"
        )
        st.dataframe(
            cox.drop(columns=["concordance"]).style.format(precision=3), hide_index=True, width="stretch"
        )
    t3 = c.query("SELECT key, value FROM t3_summary")
    if not t3.empty:
        d = {k: v for k, v in zip(t3.key, t3.value, strict=True)}
        st.markdown(
            f"**T3** (relationship R9 within 18 months; survivorship caveat): {d.get('n_population')} relationships "
            f"resolved within the window, bad rate {c.pct(float(d.get('bad_rate', 'nan')))}, first-deal logistic "
            f"AUC train {c.num(float(d.get('auc_train', 'nan')), 3)} / temporal test {c.num(float(d.get('auc_test', 'nan')), 3)}."
        )
        with st.expander("T3 coefficients"):
            st.json(json.loads(d.get("coefficients", "{}")))

fair = c.query("SELECT * FROM model_fairness")
if not fair.empty:
    with st.expander("Segment monitoring (state, industry, ...): observed vs predicted on test"):
        st.dataframe(fair.style.format(precision=3), hide_index=True, width="stretch")

st.subheader("Model card")
card = c.read_text("model_card.md")
st.markdown(c.cite(card) if card else "model_card.md missing from the snapshot")
