"""Portfolio: headline P-metrics, renewal masking, vintages (with maturity marked), exposure, refinances."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common as c
import streamlit as st

c.page_setup("Portfolio")
c.require_data()

pm = c.query(
    "SELECT metric_id, metric, value, numerator, denominator FROM portfolio_metrics WHERE segment = 'all'"
)
tm = c.query(
    "SELECT metric, dimension, segment, value, numerator, denominator, n_deals, maturity FROM target_metrics"
)


def p(metric_id: str, metric: str, field: str = "value"):
    r = pm[(pm.metric_id == metric_id) & (pm.metric == metric)]
    return None if r.empty else r[field].iloc[0]


def t1(metric: str, field: str = "value"):
    r = tm[(tm.dimension == "all") & (tm.metric == metric)]
    return None if r.empty else r[field].iloc[0]


as_of = c.query("SELECT MAX(as_of) AS d FROM portfolio_metrics").d.iloc[0]
st.title("Portfolio")
st.caption(
    f"As of {str(as_of)[:10]}. Every card reads `portfolio_metrics` / `target_metrics` (segment `all`); "
    "click a metric ID for its definition."
)


def card(col, label: str, value: str, ids: list[str], note: str = "") -> None:
    with col.container(border=True):
        st.metric(label, value)
        st.markdown(f"{c.links(ids)}" + (f" · {note}" if note else ""))


r1 = st.columns(3)
card(
    r1[0],
    "Reported vs cash-based collection rate",
    f"{c.pct(p('P1', 'reported_collection_rate'))} vs {c.pct(p('P1', 'adjusted_collection_rate'))}",
    ["P1", "D10", "D11"],
    "closed deals, RTR-weighted",
)
card(
    r1[1],
    "Renewal masking gap",
    f"{c.num(p('P2', 'renewal_masking_gap_pp'), 1)} pp",
    ["P2"],
    "reported minus cash-based",
)
card(
    r1[2],
    "Internal-payoff share of 'collections'",
    c.pct(p("P3", "internal_payoff_share")),
    ["P3", "D8", "D9"],
    "sum D8 / sum D9, all deals",
)

r2 = st.columns(3)
card(
    r2[0],
    "Bad rate: naive vs economic",
    f"{c.pct(p('P4', 'bad_rate_naive'))} vs {c.pct(p('P4', 'bad_rate_economic'))}",
    ["P4", "D18", "D19"],
    f"{int(p('P4', 'bad_rate_economic', 'denominator') or 0):,} closed deals",
)
card(
    r2[1],
    "Open exposure: economic vs contractual",
    f"{c.usd_m(p('P6', 'economic_exposure_cents'))} vs {c.usd_m(p('P6', 'contractual_balance_cents'))}",
    ["P6", "R6", "R7"],
    f"{int(p('P6', 'economic_exposure_cents', 'denominator') or 0):,} open relationships",
)
card(
    r2[2],
    "Chains ending in loss: with vs without a distressed refi",
    f"{c.pct(p('P8', 'bad_rate_chains_with_distressed'))} vs {c.pct(p('P8', 'bad_rate_chains_without_distressed'))}",
    ["P8", "R9", "D17"],
    f"distressed share of renewals {c.pct(p('P8', 'distressed_share_of_renewals'))} "
    f"({int(p('P8', 'distressed_share_of_renewals', 'numerator') or 0)} of "
    f"{int(p('P8', 'distressed_share_of_renewals', 'denominator') or 0)})",
)

r3 = st.columns(3)
card(
    r3[0],
    "T1: economic vs naive bad within 180 business days",
    f"{c.pct(t1('bad_rate_economic'))} vs {c.pct(t1('bad_rate_naive'))}",
    ["T1", "D19", "D18"],
    f"{int(t1('bad_rate_economic', 'denominator') or 0):,} mature of {int(t1('bad_rate_economic', 'n_deals') or 0):,} deals",
)
card(
    r3[1],
    "Resolved relationships: share bad, capital-weighted loss",
    f"{c.pct(p('P5', 'resolved_bad_share'))}, {c.pct(p('P5', 'capital_weighted_loss_rate'))}",
    ["P5", "R9", "R8"],
    f"cash multiple {c.num(p('P5', 'resolved_cash_multiple'))} ({c.link('R3')})",
)
card(
    r3[2],
    "Modification cure rate",
    c.pct(p("P9", "modification_cure_rate")),
    ["P9"],
    f"{int(p('P9', 'modification_cure_rate', 'numerator') or 0)} of "
    f"{int(p('P9', 'modification_cure_rate', 'denominator') or 0)} modified deals",
)

st.info(
    "**Why the economic bad rate (27.9% closed, P4) is below the simulator's 29.3% (METRICS.md §4):** "
    f"{c.link('D17')} is a rule on observable ledger stress. Its recall against the simulator's hidden "
    "distressed labels is about 0.89 (E6, Evals page), so some distressed refinances look healthy in the "
    "data and the pipeline's economic rate is a lower bound on the simulator's."
)

tab_mask, tab_vint, tab_exp, tab_refi, tab_seg = st.tabs(
    ["Renewal masking", "Vintages", "Exposure", "Refinance quality", "Segments"]
)
with tab_mask:
    c.show_figure(
        "masking", "Reported (D10) vs cash-based (D11) collection rate by funding vintage, closed deals."
    )
with tab_vint:
    st.subheader("Bad rate by vintage (immature vintages marked)")
    c.show_figure(
        "bad_rate_vintage",
        "T1 on the fixed 180-business-day horizon vs the closed-deals-only P4 rate. Partial and immature "
        "vintages are marked: their T1 rate is not comparable.",
    )
    # pivot (not pivot_table): immature vintages have no T1 value and must stay in the table
    v = tm[(tm.dimension == "vintage")].pivot(
        index=["segment", "maturity", "n_deals"], columns="metric", values="value"
    )
    den = (
        tm[(tm.dimension == "vintage") & (tm.metric == "bad_rate_economic")].set_index("segment").denominator
    )
    v = v.reset_index().rename(columns={"segment": "vintage", "n_deals": "funded deals"})
    v.insert(3, "mature deals", v.vintage.map(den).astype("Int64"))
    v["T1 economic"] = v.pop("bad_rate_economic").map(c.pct)
    v["T1 naive"] = v.pop("bad_rate_naive").map(c.pct)
    p4 = c.query(
        "SELECT segment AS vintage, value, denominator FROM portfolio_metrics "
        "WHERE metric_id = 'P4' AND metric = 'bad_rate_economic' AND segment NOT IN ('all', 'closed')"
    )
    v = v.merge(p4.rename(columns={"value": "p4", "denominator": "closed deals"}), on="vintage", how="left")
    v["P4 economic, closed only"] = v.pop("p4").map(c.pct)

    def _style(row):
        colr = {"immature": "color: #a8a7a2", "partial": "color: #eb6834"}.get(row.maturity, "")
        return [colr] * len(row)

    st.dataframe(v.style.apply(_style, axis=1), hide_index=True, width="stretch")
    st.caption(
        f"{c.links(['T1', 'P4'])}. Grey = immature (no deal has reached its horizon), orange = partial. "
        "Closed-only rates of young vintages are biased upward: only fast failures and quick renewals have closed."
    )
    st.subheader("Vintage curves")
    c.show_figure(
        "vintage_curves",
        "Cumulative cash multiple R2/R1 by months since first funding; dotted = partly observed.",
    )
with tab_exp:
    c.show_figure(
        "exposure", "Remaining RTR (what a tracker calls outstanding) vs cash out not yet recovered."
    )
with tab_refi:
    c.show_figure("refi_quality", "Wilson 95% intervals on stored counts.")
with tab_seg:
    c.show_figure(
        "segments", "T1 economic bad rate by origination segment, mature deals, Wilson 95% intervals."
    )

fn = c.findings_numbers()
with st.expander("Findings (build/findings.md)"):
    md = c.read_text("findings.md")
    st.markdown(c.cite(md) if md else "findings.md missing from the snapshot")
    st.caption(f"{len(fn)} registered numbers; each is a metric-table query re-checked by `check_findings`.")
