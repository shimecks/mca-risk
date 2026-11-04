"""Figures for the Part 5 findings. Each ``fig_*`` takes the frame from ``mcarisk.analytics.data`` and
returns ``(matplotlib Figure, plotly Figure)``: the PNG goes in ``build/figures/<name>.png`` and the plotly
JSON in ``build/figures/<name>.plotly.json`` so the Part 7 app can render it interactively.

Every figure names the metric IDs it uses (``FIGURE_METRICS``) in its title / footnote. Colours: the
validated reference categorical palette in fixed order (slot 1 blue, 2 orange, 3 aqua), muted grey for
reference / immature marks, and a single-hue blue ramp for the ordinal vintage cohorts in P7.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from matplotlib.colors import LinearSegmentedColormap
from plotly.subplots import make_subplots

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GREY, INK, INK2, GRID = "#a8a7a2", "#0b0b0b", "#52514e", "#e6e5e1"
SURFACE = "#fcfcfb"
RAMP = LinearSegmentedColormap.from_list("blue_ramp", ["#9ec5f4", "#2a78d6", "#0d366b"])

FIGURE_METRICS: dict[str, list[str]] = {
    "masking": ["P1", "P2", "P3", "D9", "D10", "D11"],
    "bad_rate_vintage": ["T1", "D18", "D19", "P4"],
    "exposure": ["P6", "R6", "R7"],
    "vintage_curves": ["P7", "R1", "R2"],
    "refi_quality": ["P8", "P9", "P5", "R9", "D17"],
    "segments": ["T1", "D19"],
}


def _cite(name: str) -> str:
    return "Metrics: " + ", ".join(FIGURE_METRICS[name]) + " (METRICS.md v1.3)"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _fig(w: float = 9, h: float = 4.8, **kw):
    fig, ax = plt.subplots(figsize=(w, h), facecolor=SURFACE, **kw)
    return fig, ax


def _foot(fig, name: str, extra: str = "") -> None:
    fig.text(0.01, 0.01, _cite(name) + (f". {extra}" if extra else ""), fontsize=7, color=INK2)


def _plotly_layout(fig: go.Figure, title: str, name: str, yfmt: str | None = ".0%") -> go.Figure:
    fig.update_layout(
        title={"text": f"{title}<br><sup>{_cite(name)}</sup>"},
        template="plotly_white",
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font={"color": INK, "size": 12},
        legend={"orientation": "h", "y": -0.18},
        hovermode="x unified",
    )
    if yfmt:
        fig.update_yaxes(tickformat=yfmt)
    return fig


def _pct(v) -> str:
    return "" if v is None or pd.isna(v) else f"{v * 100:.1f}%"


# ------------------------------------------------------------------------------------------ 1 masking
def fig_masking(df: pd.DataFrame):
    """P1 reported vs adjusted collection rate by vintage (closed deals, RTR-weighted); P2 gap."""
    d = df[df.segment != "all"].reset_index(drop=True)
    tot = df[df.segment == "all"]
    x = np.arange(len(d))
    fig, ax = _fig()
    _style(ax)
    w = 0.38
    ax.bar(x - w / 2 - 0.01, d.reported_rate, w, color=GREY, label="Reported (D9/D2, renewal payoffs count)")
    ax.bar(x + w / 2 + 0.01, d.adjusted_rate, w, color=BLUE, label="Cash-adjusted (D7/D2)")
    ax.set_xticks(x, d.segment, rotation=45, ha="right")
    ax.set_ylim(0, 1.1)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_ylabel("Collected / RTR, closed deals", color=INK2, fontsize=9)
    sub = ""
    if len(tot):
        t = tot.iloc[0]
        sub = (
            f"All closed deals: reported {_pct(t.reported_rate)} vs adjusted {_pct(t.adjusted_rate)} "
            f"(gap {t.masking_gap_pp:.1f} pp); {_pct(t.internal_payoff_share)} of reported collections are "
            "internal payoffs"
        )
    ax.set_title(
        "Renewal masking: reported vs cash-adjusted collection rate by vintage", color=INK, loc="left", pad=20
    )
    ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=8, color=INK2)
    ax.legend(frameon=False, fontsize=8, loc="lower left")
    _foot(fig, "masking", "Recent vintages: only deals that closed already (mostly renewals) are in P1")
    fig.tight_layout(rect=(0, 0.03, 1, 1))

    pf = go.Figure()
    pf.add_bar(x=d.segment, y=d.reported_rate, name="Reported (D10)", marker_color=GREY)
    pf.add_bar(x=d.segment, y=d.adjusted_rate, name="Cash-adjusted (D11)", marker_color=BLUE)
    pf.add_scatter(
        x=d.segment, y=d.masking_gap_pp / 100, name="Masking gap (P2)", mode="lines+markers",
        line={"color": ORANGE, "width": 2},
    )  # fmt: skip
    return fig, _plotly_layout(pf, "Renewal masking by vintage", "masking")


# ------------------------------------------------------------------------------------------ 2 bad rate
def fig_bad_rate_vintage(df: pd.DataFrame):
    """T1 (D19 within 180 bd) and naive D18-at-horizon by vintage with Wilson CIs; immature vintages
    greyed; P4 closed-deals-only economic bad rate for contrast."""
    d = df.reset_index(drop=True)
    x = np.arange(len(d))
    mature = (d.maturity == "mature").to_numpy()
    fig, ax = _fig(10, 5)
    _style(ax)
    for i in range(len(d)):
        if not mature[i]:
            ax.axvspan(i - 0.5, i + 0.5, color="#f0efec", zorder=0)
            lab = "immature" if d.maturity[i] == "immature" else "partial"
            ax.text(i, 0.95, lab, ha="center", fontsize=7, color=INK2, rotation=90)
    te = d.t1_economic.astype(float).to_numpy()
    lo = np.clip(te - d.t1_economic_ci_lo.astype(float).to_numpy(), 0, None)
    hi = np.clip(d.t1_economic_ci_hi.astype(float).to_numpy() - te, 0, None)
    m = mature
    ax.errorbar(x[m], te[m], yerr=[lo[m], hi[m]], fmt="o-", color=BLUE, ms=6, lw=2, capsize=3,
                label="T1 economic bad (D19 by 180 bd), 95% Wilson CI")  # fmt: skip
    pm = ~m & ~np.isnan(te)
    if pm.any():
        ax.errorbar(x[pm], te[pm], yerr=[lo[pm], hi[pm]], fmt="o", color=GREY, ms=6, capsize=3,
                    label="T1 on the few mature deals of a partial vintage")  # fmt: skip
    ax.plot(
        x[m],
        d.t1_naive.astype(float).to_numpy()[m],
        "s--",
        color=AQUA,
        ms=5,
        lw=1.5,
        label="T1 naive (D18 by 180 bd)",
    )
    ax.plot(x, d.closed_economic.astype(float), "^:", color=ORANGE, ms=6, lw=1.5,
            label="P4 economic bad, closed deals only (biased for young vintages)")  # fmt: skip
    ax.set_xticks(x, d.vintage, rotation=45, ha="right")
    ax.set_ylim(0, 1.0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_ylabel("Share of deals bad", color=INK2, fontsize=9)
    ax.set_title("Bad rate by funding vintage: fixed 180-business-day horizon (T1) vs closed-only (P4)",
                 color=INK, loc="left")  # fmt: skip
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _foot(fig, "bad_rate_vintage", "Shaded vintages have not all reached the 180-bd horizon")
    fig.tight_layout(rect=(0, 0.03, 1, 1))

    pf = go.Figure()
    em = d[mature]
    pf.add_scatter(
        x=em.vintage, y=em.t1_economic, name="T1 economic (D19 by 180 bd)", mode="lines+markers",
        line={"color": BLUE, "width": 2},
        error_y={"type": "data", "symmetric": False, "array": (em.t1_economic_ci_hi - em.t1_economic).astype(float),
                 "arrayminus": (em.t1_economic - em.t1_economic_ci_lo).astype(float)},
        customdata=np.stack([em.t1_economic_bad, em.t1_mature_deals], axis=-1),
        hovertemplate="%{y:.1%} (%{customdata[0]} of %{customdata[1]} mature deals)",
    )  # fmt: skip
    pf.add_scatter(x=em.vintage, y=em.t1_naive, name="T1 naive (D18 by 180 bd)", mode="lines+markers",
                   line={"color": AQUA, "dash": "dash"})  # fmt: skip
    pf.add_scatter(x=d.vintage, y=d.closed_economic, name="P4 economic, closed deals only", mode="lines+markers",
                   line={"color": ORANGE, "dash": "dot"})  # fmt: skip
    young = d[~mature]
    if len(young):
        pf.add_scatter(x=young.vintage, y=[0.02] * len(young), mode="text", text=list(young.maturity),
                       name="not mature at 180 bd", textfont={"color": INK2})  # fmt: skip
    return fig, _plotly_layout(pf, "Bad rate by vintage: T1 fixed horizon vs closed-only", "bad_rate_vintage")


# ------------------------------------------------------------------------------------------ 3 exposure
def fig_exposure(df: pd.DataFrame):
    """P6: economic exposure (sum R6) vs contractual balance (sum R7) over open relationships."""
    lab = {"economic_exposure_cents": "Economic exposure (R6: cash at risk)",
           "contractual_balance_cents": "Contractual balance (R7: remaining RTR)"}  # fmt: skip
    d = df.assign(label=df.metric.map(lab), usd_m=df.cents.astype(float) / 1e8)
    d = d.set_index("metric").loc[["contractual_balance_cents", "economic_exposure_cents"]].reset_index()
    fig, ax = _fig(8, 3.2)
    _style(ax)
    ax.xaxis.grid(True, color=GRID)
    ax.yaxis.grid(False)
    ax.barh(d.label, d.usd_m, color=[GREY, BLUE], height=0.5)
    for i, v in enumerate(d.usd_m):
        ax.text(v, i, f"  ${v:,.2f}M", va="center", fontsize=9, color=INK)
    ax.set_xlim(0, d.usd_m.max() * 1.25)
    ax.set_xlabel("USD millions, open relationships", color=INK2, fontsize=9)
    ax.set_title(
        "Open exposure: what a tracker calls outstanding vs the cash still at risk", color=INK, loc="left"
    )
    _foot(fig, "exposure")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    pf = go.Figure(go.Bar(x=d.usd_m, y=d.label, orientation="h", marker_color=[GREY, BLUE],
                          hovertemplate="$%{x:,.2f}M<extra></extra>"))  # fmt: skip
    return fig, _plotly_layout(pf, "Open exposure: contractual vs economic (USD M)", "exposure", yfmt=None)


# ------------------------------------------------------------------------------------------ 4 curves
def fig_vintage_curves(df: pd.DataFrame):
    """P7: cumulative cash multiple R2/R1 by months since first funding, one line per first-funding
    quarter, censored at the as-of date. Solid while every relationship of the cohort is observed."""
    qs = sorted(df.vintage.unique())
    fig, ax = _fig(10, 5)
    _style(ax)
    ax.axhline(1.0, color=INK2, lw=1, ls="--")
    ax.text(0, 1.01, "break-even (R3 = 1)", fontsize=7, color=INK2)
    pf = go.Figure()
    for i, q in enumerate(qs):
        g = df[df.vintage == q].sort_values("month")
        c = RAMP(i / max(1, len(qs) - 1))
        hexc = matplotlib.colors.to_hex(c)
        full = g[g.full_cohort.astype(bool)]
        tail = g[g.month >= (full.month.max() if len(full) else -1)]
        ax.plot(full.month, full.cash_multiple, color=c, lw=2, label=q)
        ax.plot(tail.month, tail.cash_multiple, color=c, lw=1, ls=":", alpha=0.6)
        pf.add_scatter(x=full.month, y=full.cash_multiple, name=q, mode="lines", line={"color": hexc, "width": 2},
                       legendgroup=q)  # fmt: skip
        pf.add_scatter(x=tail.month, y=tail.cash_multiple, name=f"{q} (partial cohort)", mode="lines",
                       line={"color": hexc, "width": 1, "dash": "dot"}, legendgroup=q, showlegend=False)  # fmt: skip
    ax.set_xlabel("Months since first funding", color=INK2, fontsize=9)
    ax.set_ylabel("Cumulative cash in / cash out (R2/R1)", color=INK2, fontsize=9)
    ax.set_ylim(0, 1.5)
    ax.set_title("Vintage curves: cumulative cash multiple by first-funding quarter", color=INK, loc="left")
    ax.legend(
        frameon=False, fontsize=7, ncol=2, loc="lower right", title="First-funding quarter", title_fontsize=7
    )
    _foot(
        fig,
        "vintage_curves",
        "Dotted: only part of the cohort observed that long (censored at the as-of date)",
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    pf.add_hline(y=1.0, line_dash="dash", line_color=INK2)
    pf = _plotly_layout(pf, "Vintage curves: cumulative cash multiple (R2/R1)", "vintage_curves", yfmt=".2f")
    pf.update_layout(hovermode="closest")
    return fig, pf


# ------------------------------------------------------------------------------------------ 5 refinance
def fig_refi_quality(df: pd.DataFrame):
    """P8: R9 (relationship bad) rate of resolved chains with vs without a D17 distressed refinance,
    against all resolved relationships (P5); P9 modification cure rate."""
    rows = [
        ("P8", "bad_rate_chains_with_distressed", "all", "Chains with a distressed refi (D17)", ORANGE),
        ("P8", "bad_rate_chains_without_distressed", "all", "Chains without", BLUE),
        ("P5", "resolved_bad_share", "all", "All resolved relationships", GREY),
        ("P9", "modification_cure_rate", "all", "Modification cure rate (all modified)", AQUA),
        ("P9", "modification_cure_rate", "closed", "Modification cure rate (closed modified)", AQUA),
    ]
    recs = {(r.metric_id, r.metric, r.segment): r for r in df.itertuples(index=False)}
    sel = [(r, recs[r[:3]]) for r in rows if r[:3] in recs]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), facecolor=SURFACE, gridspec_kw={"width_ratios": [3, 2]})
    pf = make_subplots(rows=1, cols=2, subplot_titles=("Resolved relationships ending in loss (R9)",
                                                       "Modified deals that cure (P9)"))  # fmt: skip
    for ax, ids, col in ((axes[0], ("P8", "P5"), 1), (axes[1], ("P9",), 2)):
        _style(ax)
        part = [(r, v) for r, v in sel if r[0] in ids]
        labels = [r[3] for r, _ in part]
        vals = [float(v.value) for _, v in part]
        err = [[max(0.0, float(v.value - v.rate_ci_lo)) for _, v in part],
               [max(0.0, float(v.rate_ci_hi - v.value)) for _, v in part]]  # fmt: skip
        ax.bar(range(len(part)), vals, color=[r[4] for r, _ in part], width=0.6)
        ax.errorbar(range(len(part)), vals, yerr=err, fmt="none", ecolor=INK2, capsize=3, lw=1)
        for i, (_, v) in enumerate(part):
            ax.text(i, 0.02, f"{v.value * 100:.1f}%\n{int(v.numerator)}/{int(v.denominator)}", ha="center",
                    fontsize=8, color="white")  # fmt: skip
        ax.set_xticks(range(len(part)), labels, fontsize=7, rotation=15, ha="right")
        ax.set_ylim(0, 1)
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        pf.add_bar(x=labels, y=vals, marker_color=[r[4] for r, _ in part], showlegend=False, row=1, col=col,
                   error_y={"type": "data", "symmetric": False, "array": err[1], "arrayminus": err[0]},
                   customdata=[[int(v.numerator), int(v.denominator)] for _, v in part],
                   hovertemplate="%{y:.1%} (%{customdata[0]}/%{customdata[1]})<extra></extra>")  # fmt: skip
    axes[0].set_title(
        "Resolved relationships ending in loss (R9: R3 < 1)", color=INK, loc="left", fontsize=10
    )
    axes[1].set_title("Modified deals that cure (P9)", color=INK, loc="left", fontsize=10)
    _foot(fig, "refi_quality", "Error bars: 95% Wilson CI")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    return fig, _plotly_layout(pf, "Refinance quality and modification cure", "refi_quality")


# ------------------------------------------------------------------------------------------ 6 segments
SEGMENT_ORDER = {
    "positions": ["0", "1", "2", "3+", "unknown"],
    "burden": ["<15%", "15-20%", "20-25%", "25-35%", ">=35%", "unknown"],
}
SEGMENT_TITLES = {
    "industry": "Industry",
    "iso": "ISO (broker)",
    "positions": "Existing positions at funding",
    "burden": "Burden (daily payment x 21 / monthly revenue)",
}


def _seg_frame(df: pd.DataFrame, dim: str) -> pd.DataFrame:
    g = df[df.dimension == dim].copy()
    if dim in SEGMENT_ORDER:
        order = {s: i for i, s in enumerate(SEGMENT_ORDER[dim])}
        g["_o"] = g.segment.map(lambda s: order.get(s, 99))
    else:
        g["_o"] = g.t1_economic.rank(ascending=False) + (g.segment == "unknown") * 1000
    return g.sort_values("_o").drop(columns="_o").reset_index(drop=True)


def fig_segments(df: pd.DataFrame, overall: float | None = None):
    """T1 economic bad rate (mature deals) by origination segment, with 95% Wilson CIs."""
    dims = [d for d in ("industry", "iso", "positions", "burden") if (df.dimension == d).any()]
    fig, axes = plt.subplots(
        len(dims), 1, figsize=(9, 2.6 * len(dims) + 0.6), facecolor=SURFACE, squeeze=False
    )
    pf = make_subplots(rows=len(dims), cols=1, subplot_titles=[SEGMENT_TITLES[d] for d in dims],
                       vertical_spacing=0.08)  # fmt: skip
    for r, dim in enumerate(dims):
        ax = axes[r][0]
        _style(ax)
        ax.xaxis.grid(True, color=GRID)
        ax.yaxis.grid(False)
        g = _seg_frame(df, dim)
        y = np.arange(len(g))[::-1]
        v = g.t1_economic.astype(float).to_numpy()
        lo = np.clip(v - g.t1_economic_ci_lo.astype(float).to_numpy(), 0, None)
        hi = np.clip(g.t1_economic_ci_hi.astype(float).to_numpy() - v, 0, None)
        colors = [GREY if s == "unknown" else BLUE for s in g.segment]
        ax.errorbar(v, y, xerr=[lo, hi], fmt="none", ecolor=INK2, capsize=2, lw=1)
        ax.scatter(v, y, color=colors, s=36, zorder=3)
        ax.set_yticks(
            y, [f"{s}  (n={int(n)})" for s, n in zip(g.segment, g.mature_deals, strict=True)], fontsize=7
        )
        if overall is not None:
            ax.axvline(overall, color=ORANGE, lw=1, ls="--")
        ax.set_xlim(0, 1)
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.set_title(SEGMENT_TITLES[dim], color=INK, loc="left", fontsize=9)
        pf.add_scatter(
            x=v, y=g.segment, mode="markers", marker={"color": colors, "size": 9}, showlegend=False,
            error_x={"type": "data", "symmetric": False, "array": hi, "arrayminus": lo},
            customdata=np.stack([g.bad, g.mature_deals], axis=-1),
            hovertemplate="%{y}: %{x:.1%} (%{customdata[0]}/%{customdata[1]})<extra></extra>",
            row=r + 1, col=1,
        )  # fmt: skip
    fig.suptitle("T1 economic bad rate by origination segment (mature deals, 95% Wilson CI)", color=INK,
                 x=0.01, ha="left", fontsize=11)  # fmt: skip
    _foot(fig, "segments", "Dashed: portfolio T1 rate" if overall is not None else "")
    fig.tight_layout(rect=(0, 0.02, 1, 0.98))
    pf = _plotly_layout(pf, "T1 economic bad rate by segment", "segments", yfmt=None)
    pf.update_xaxes(tickformat=".0%", range=[0, 1])
    pf.update_layout(height=320 * len(dims), hovermode="closest")
    return fig, pf


FIGURES = {
    "masking": fig_masking,
    "bad_rate_vintage": fig_bad_rate_vintage,
    "exposure": fig_exposure,
    "vintage_curves": fig_vintage_curves,
    "refi_quality": fig_refi_quality,
    "segments": fig_segments,
}
