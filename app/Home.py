"""Overview: the whole story on one screen (``make app`` / Streamlit Community Cloud main file ``app/Home.py``).

Five cards compare what the tracker says with what the cash says, plus one chart. Every value is read
from ``portfolio_metrics`` (segment ``all`` or a vintage); nothing is recomputed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c
import plotly.graph_objects as go
import streamlit as st

c.page_setup("Overview")
c.require_data()

rep = c.pm("P1", "reported_collection_rate")
adj = c.pm("P1", "adjusted_collection_rate")
gap = c.pm("P2", "renewal_masking_gap_pp")
share = c.pm("P3", "internal_payoff_share")
bad_naive = c.pm("P4", "bad_rate_naive")
bad_econ = c.pm("P4", "bad_rate_economic")
n_closed = c.pm("P4", "bad_rate_economic", "denominator")
exp_econ = c.pm("P6", "economic_exposure_cents")
exp_contr = c.pm("P6", "contractual_balance_cents")
n_open = c.pm("P6", "economic_exposure_cents", "denominator")
with_d = c.pm("P8", "bad_rate_chains_with_distressed")
without_d = c.pm("P8", "bad_rate_chains_without_distressed")

mult = {2: "double", 3: "triple"}.get(round(bad_econ / bad_naive), f"{bad_econ / bad_naive:.1f}x")

st.title("MCA portfolio risk, measured in cash")
st.markdown(
    c.mdx(
        f"#### The tracker says this book is healthy. The cash says otherwise: renewals are being counted "
        f"as collections, so the real bad-deal rate is **{mult}** what's reported "
        f"and only **{c.pct(adj, 0)}** of what was owed actually came back."
    )
)
st.caption(
    f"Synthetic MCA book as of {c.date(c.as_of_date())}. Grey = what the tracker reports · "
    "blue = what the cash says · red = losses."
)

cols = st.columns(5)
c.card(
    cols[0],
    "Cash collected",
    c.pct(adj),
    f"vs **{c.pct(rep)}** the tracker reports",
    f"Of what closed deals owed. The {c.pts(gap)} gap is renewals booked as if the merchant paid.",
    c.spec("P1", "P2", "D10", "D11"),
)
c.card(
    cols[1],
    "Deals that went bad",
    c.pct(bad_econ),
    f"vs **{c.pct(bad_naive)}** counted the usual way",
    f"Once refinances of struggling merchants count as bad. {int(n_closed or 0):,} closed deals.",
    c.spec("P4", "D18", "D19", "D17"),
)
c.card(
    cols[2],
    "Money really at risk",
    c.money(exp_econ),
    f"vs **{c.money(exp_contr)}** balance on the books",
    f"Cash paid out and not yet recovered, over {int(n_open or 0):,} open merchants. "
    "The rest of the balance is unearned margin.",
    c.spec("P6", "R6", "R7"),
)
c.card(
    cols[3],
    "Renewal “collections”",
    c.pct(share),
    "of every dollar the tracker calls collected",
    "About one in five: the old balance was paid off with the funder's own new advance.",
    c.spec("P3", "D8", "D9"),
)
ratio = (with_d / without_d) if with_d and without_d else None
c.card(
    cols[4],
    "Distressed refi chains",
    c.pct(with_d),
    f"ended in a loss, vs **{c.pct(without_d)}** of other chains",
    (f"About {ratio:.0f}x as often. " if ratio else "")
    + "Refinancing a struggling merchant delays the loss; it doesn't avoid it.",
    c.spec("P8", "R9", "D17"),
)

# ---- one chart: reported vs cash collection rate by funding quarter
v = c.portfolio()
v = v[(v.metric_id == "P1") & ~v.segment.isin(["all", "closed"])]
v = v.pivot(index="segment", columns="metric", values="value").sort_index().reset_index()
mat = c.query(
    "SELECT segment, maturity FROM target_metrics WHERE dimension = 'vintage' AND metric = 'bad_rate_economic'"
)
young = [s for s, m in zip(mat.segment, mat.maturity, strict=True) if m != "mature"]
x = [c.quarter(s) for s in v.segment]

fig = go.Figure()
if young:
    yx = [c.quarter(s) for s in young if s in set(v.segment)]
    if yx:
        fig.add_vrect(
            x0=x.index(yx[0]) - 0.5,
            x1=len(x) - 0.5,
            fillcolor="#f4f3ef",
            line_width=0,
            layer="below",
            annotation_text="Too young to judge:<br>only early closers so far",
            annotation_position="bottom left",
            annotation_font={"size": 11, "color": c.INK_2},
        )
for col, name, colr in (
    ("reported_collection_rate", "Tracker: reported collected", c.TRACKER),
    ("adjusted_collection_rate", "Cash: actually collected", c.CASH),
):
    fig.add_scatter(
        x=x,
        y=v[col],
        mode="lines+markers",
        name=name,
        line={"color": colr, "width": 2.5},
        marker={"size": 8, "color": colr},
        hovertemplate="%{x}: %{y:.1%}<extra>" + name + "</extra>",
    )
    fig.add_annotation(
        x=x[0],
        y=v[col].iloc[0],
        text=f"<b>{name.split(':')[0]}</b>",
        showarrow=False,
        xanchor="right",
        xshift=-12,
        font={"color": colr, "size": 13},
    )
c.style(
    fig,
    f"Every quarter, the tracker shows far more collected than the cash does "
    f"({c.pct(rep, 0)} vs {c.pct(adj, 0)} overall)",
    height=400,
    subtitle="Share of what closed deals owed, by funding quarter",
)
fig.update_yaxes(tickformat=".0%", range=[0, 1.05])
fig.update_xaxes(range=[-1.6, len(x) - 0.5])
c.chart(fig, "home_masking")
st.caption(
    "Each point is the deals funded in that quarter that have closed. The gap is renewal payoffs: "
    "the tracker counts them as collected, but no cash came from the merchant."
)

c.footer()
