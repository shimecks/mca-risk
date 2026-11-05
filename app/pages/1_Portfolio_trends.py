"""Portfolio trends: which funding quarters went bad, money at risk, refinance quality and segments.

Values come from ``target_metrics`` (T1, fixed 180-business-day horizon), ``portfolio_metrics`` (P4, P6,
P8, P9) and the findings registry (P8 intervals). Nothing is recomputed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

c.page_setup("Portfolio trends")
c.require_data()

tm = c.query(
    "SELECT metric, dimension, segment, value, numerator, denominator, n_deals, maturity FROM target_metrics"
)
fn = c.findings_numbers()


def _t(key: str) -> str:
    return fn[key]["text"] if key in fn else "n/a"


st.title("Portfolio trends")
st.markdown(
    "Which deals went bad, how much money is really at risk, and where the risk concentrates. "
    "“Bad” counts refinances of struggling merchants, not just defaults."
)

# ---------------------------------------------------------------------------- 1. vintages
vt = tm[tm.dimension == "vintage"].pivot(
    index=["segment", "maturity", "n_deals"], columns="metric", values="value"
)
vt = vt.reset_index().sort_values("segment")
den = tm[(tm.dimension == "vintage") & (tm.metric == "bad_rate_economic")].set_index("segment").denominator
vt["mature_n"] = vt.segment.map(den)
vt["label"] = vt.segment.map(c.quarter)
mature = vt[vt.maturity == "mature"]
worst = mature.nlargest(3, "bad_rate_economic").sort_values("segment")
worst_set = set(worst.segment)

colors = [
    (c.CASH if s in worst_set else c.CASH_LIGHT) if m == "mature" else c.MUTED
    for s, m in zip(vt.segment, vt.maturity, strict=True)
]
fig = go.Figure()
fig.add_bar(
    x=vt.label,
    y=vt.bad_rate_economic.fillna(0),
    name="Went bad (cash view, incl. distressed refis)",
    marker={"color": colors, "cornerradius": 4},
    text=[
        c.pct(v, 0) if s in worst_set else ("" if m == "mature" else "too young")
        for s, v, m in zip(vt.segment, vt.bad_rate_economic, vt.maturity, strict=True)
    ],
    textposition="outside",
    textfont={"color": c.INK},
    customdata=list(zip(vt.mature_n.fillna(0).astype(int), vt.n_deals, vt.maturity, strict=True)),
    hovertemplate="%{x}: %{y:.1%} went bad<br>%{customdata[0]} of %{customdata[1]} deals old enough "
    "to judge (%{customdata[2]})<extra></extra>",
)
fig.add_scatter(
    x=mature.label,
    y=mature.bad_rate_naive,
    mode="markers",
    name="Tracker's count (defaults and write-offs only)",
    marker={
        "color": c.TRACKER,
        "size": 9,
        "symbol": "line-ew-open",
        "line": {"width": 3, "color": c.TRACKER},
    },
    hovertemplate="%{x}: %{y:.1%} by the tracker's count<extra></extra>",
)
c.style(
    fig,
    f"{c.quarter(worst.segment.iloc[0])} to {c.quarter(worst.segment.iloc[-1])} deals went bad far more often",
    height=420,
    subtitle="Share of each quarter's deals that went bad within 180 business days of funding. "
    "Grey bars: too young to judge yet.",
)
fig.update_yaxes(tickformat=".0%", rangemode="tozero")
c.chart(fig, "vintage")
others = mature[~mature.segment.isin(worst_set)]
st.caption(
    f"The three worst quarters: {', '.join(c.pct(v) for v in worst.bad_rate_economic)}; the other mature "
    f"quarters range {c.pct(others.bad_rate_economic.min(), 0)} to {c.pct(others.bad_rate_economic.max(), 0)}. "
    "The tracker's count (grey ticks) shows the same pattern at about half the level: the rest are "
    "refinances it reports as paid off."
)
with st.expander("Table: every funding quarter"):
    tbl = pd.DataFrame(
        {
            "Funding quarter": vt.label,
            "Deals funded": vt.n_deals,
            "Old enough to judge": vt.mature_n.fillna(0).astype(int),
            "Went bad (cash view)": vt.bad_rate_economic.map(c.pct),
            "Went bad (tracker's count)": vt.bad_rate_naive.map(c.pct),
            "Status": vt.maturity.map(
                {"mature": "mature", "partial": "partly mature", "immature": "too young"}
            ),
        }
    )
    st.dataframe(tbl, hide_index=True, width="stretch")
    st.caption(
        "Fixed 180-business-day horizon (spec T1). Rates on young quarters are not comparable: only fast "
        "failures and quick renewals have resolved yet."
    )

st.divider()

# ---------------------------------------------------------------------------- 2. exposure and refis
left, right = st.columns(2, gap="large")
with left:
    econ = c.pm("P6", "economic_exposure_cents")
    contr = c.pm("P6", "contractual_balance_cents")
    fig = go.Figure()
    fig.add_bar(
        y=["Balance on the books", "Cash really at risk"],
        x=[contr / 1e8, econ / 1e8],
        orientation="h",
        marker={"color": [c.TRACKER, c.CASH], "cornerradius": 4},
        text=[c.money(contr), c.money(econ)],
        textposition="outside",
        textfont={"color": c.INK, "size": 15},
        hovertemplate="%{y}: $%{x:.2f}M<extra></extra>",
        width=0.55,
    )
    c.style(
        fig,
        f"Only {c.money(econ)} of the {c.money(contr)} “outstanding” is cash at risk",
        height=300,
        subtitle=f"Open merchants ({int(c.pm('P6', 'economic_exposure_cents', 'denominator') or 0):,})",
    )
    fig.update_xaxes(visible=False, range=[0, contr / 1e8 * 1.25])
    fig.update_yaxes(showgrid=False, autorange="reversed")
    fig.update_layout(showlegend=False)
    c.chart(fig, "exposure")
    st.caption(
        "The balance is what the merchant still owes, including the funder's margin. Cash at risk is what "
        "the funder paid out and hasn't got back: the number to reserve against."
    )
    st.caption(c.mdx("Spec: P6 (R6 economic exposure vs R7 contractual balance)."))

with right:
    w = c.pm("P8", "bad_rate_chains_with_distressed")
    wo = c.pm("P8", "bad_rate_chains_without_distressed")
    fig = go.Figure()
    fig.add_bar(
        x=["With a distressed refinance", "Without one"],
        y=[w, wo],
        marker={"color": [c.LOSS, "#efb3b3"], "cornerradius": 4},
        text=[c.pct(w, 0), c.pct(wo, 0)],
        textposition="outside",
        textfont={"color": c.INK, "size": 15},
        customdata=[
            f"{_t('p8_with_k')} of {_t('p8_with_n')}; 95% range {_t('p8_with_lo')} to {_t('p8_with_hi')}",
            f"95% range {_t('p8_without_lo')} to {_t('p8_without_hi')}",
        ],
        hovertemplate="%{x}: %{y:.1%} ended in a loss<br>%{customdata}<extra></extra>",
        width=0.5,
    )
    c.style(
        fig,
        f"Chains with a distressed refi ended in a loss {w / wo:.1f}x as often",
        height=300,
        subtitle="Share of finished merchant relationships that lost money",
    )
    fig.update_yaxes(visible=False, range=[0, max(w, wo) * 1.3])
    fig.update_layout(showlegend=False)
    c.chart(fig, "refi")
    st.caption(
        f"{_t('p8_share')} of renewals ({_t('p8_share_k')} of {_t('p8_share_n')}) refinanced a merchant that "
        f"was already struggling. Separately, {c.pct(c.pm('P9', 'modification_cure_rate'), 0)} of deals with "
        "modified terms recovered."
    )
    st.caption("Spec: P8 (R9 relationship bad, D17 distressed refinance); P9 modification cure rate.")

st.divider()

# ---------------------------------------------------------------------------- 3. segments
DIMS = {
    "Other advances already on the merchant": "positions",
    "Industry": "industry",
    "Broker (ISO)": "iso",
    "Payment as a share of revenue": "burden",
}
POS = {"0": "None", "1": "1 other", "2": "2 others", "3+": "3 or more", "unknown": "Unknown"}
ORDER = {
    "positions": ["0", "1", "2", "3+", "unknown"],
    "burden": ["<15%", "15-20%", "20-25%", "25-35%", ">=35%", "unknown"],
}
dim_label = st.radio("Break bad-deal rates down by", list(DIMS), horizontal=True, key="segment_dim")
dim = DIMS[dim_label]
sg = tm[(tm.dimension == dim) & (tm.metric == "bad_rate_economic")].copy()
if dim in ORDER:
    sg["o"] = sg.segment.map({s: i for i, s in enumerate(ORDER[dim])})
    sg = sg.sort_values("o")
else:
    sg = sg.assign(u=sg.segment == "unknown").sort_values(["u", "value"], ascending=[True, False])
sg["label"] = (
    sg.segment.map(POS).fillna(sg.segment)
    if dim == "positions"
    else sg.segment.str.replace(">=", "≥", regex=False)
)
known = sg[sg.segment != "unknown"]
hi, lo = known.loc[known.value.idxmax()], known.loc[known.value.idxmin()]
fig = go.Figure()
fig.add_bar(
    y=sg.label,
    x=sg.value,
    orientation="h",
    marker={
        "color": [c.MUTED if s == "unknown" else c.CASH for s in sg.segment],
        "cornerradius": 4,
    },
    text=sg.value.map(lambda v: c.pct(v, 0)),
    textposition="outside",
    textfont={"color": c.INK},
    customdata=sg.denominator,
    hovertemplate="%{y}: %{x:.1%} went bad (%{customdata} deals)<extra></extra>",
)
title = {
    "positions": f"Merchants already carrying {str(hi.label).lower()} other advances went bad "
    f"{c.pct(hi.value, 0)} of the time, vs {c.pct(lo.value, 0)} for {str(lo.label).lower()}",
    "burden": f"Lowest-burden deals went bad most often ({c.pct(hi.value, 0)}): they are often refinances "
    "written at a lower payment",
}.get(dim, f"{hi.label} went bad most often ({c.pct(hi.value, 0)}), {lo.label} least ({c.pct(lo.value, 0)})")
c.style(
    fig,
    title,
    height=150 + 32 * len(sg),
    subtitle="Share that went bad within 180 business days, deals old enough to judge",
)
fig.update_xaxes(visible=False, range=[0, sg.value.max() * 1.18])
fig.update_yaxes(showgrid=False, autorange="reversed")
fig.update_layout(showlegend=False, bargap=0.3)
c.chart(fig, f"segments_{dim}")
st.caption(
    "Small groups swing a lot; hover a bar for its deal count. Spec: T1 economic bad by origination segment."
)

st.divider()
with st.expander("More detail: cash-back curves by funding quarter"):
    c.show_figure(
        "vintage_curves",
        "Cash returned per dollar paid out (R2/R1) by months since a merchant's first deal. Merchants "
        "typically break even (1.0) around month 12. Spec: P7.",
    )
with st.expander("Full written findings"):
    md = c.read_text("findings.md")
    st.markdown(c.cite(md) if md else "findings.md missing from the snapshot")
c.footer()
