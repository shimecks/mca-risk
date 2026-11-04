"""Model figures (Part 6), same conventions as ``mcarisk.analytics.figures``: each ``fig_*(df)`` returns
``(matplotlib Figure, plotly Figure)``; the stage writes ``build/figures/<name>.png``,
``<name>.plotly.json`` and ``data/<name>.csv``. ``MODEL_DATASETS[name](con)`` rebuilds each frame from the
model tables, and ``MODEL_FIGURE_METRICS[name]`` lists the metric IDs it cites.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from mcarisk.analytics.figures import AQUA, BLUE, GREY, INK, INK2, ORANGE, SURFACE, _style

MODEL_FIGURE_METRICS: dict[str, list[str]] = {
    "model_calibration": ["T1", "D19"],
    "model_lift": ["T1", "D19"],
    "model_naive_vs_economic": ["T1", "D18", "D19", "D17"],
    "model_coefficients": ["T1", "D13", "D14"],
    "survival_km": ["T2", "D19", "D16", "D17"],
}
MODEL_COLORS = {
    "logit": BLUE,
    "logit_uncalibrated": "#9ec5f4",
    "gbm_isotonic": ORANGE,
    "baseline_rule": GREY,
    "logit_naive": AQUA,
    "gbm": INK2,
}
MODEL_LABELS = {
    "logit": "Logistic (economic T1, val-recalibrated)",
    "logit_uncalibrated": "Logistic, uncalibrated",
    "gbm_isotonic": "Gradient boosting + isotonic",
    "gbm": "Gradient boosting (raw)",
    "baseline_rule": "Baseline rule (positions + burden)",
    "logit_naive": "Logistic trained on naive D18",
}


def _cite(name: str) -> str:
    return "Metrics: " + ", ".join(MODEL_FIGURE_METRICS[name]) + " (METRICS.md v1.3)"


def _foot(fig, name: str, extra: str = "") -> None:
    fig.text(0.01, 0.01, _cite(name) + (f". {extra}" if extra else ""), fontsize=7, color=INK2)


def _layout(fig: go.Figure, title: str, name: str, yfmt: str | None = ".0%") -> go.Figure:
    fig.update_layout(
        title={"text": f"{title}<br><sup>{_cite(name)}</sup>"},
        template="plotly_white",
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font={"color": INK, "size": 12},
        legend={"orientation": "h", "y": -0.18},
    )
    if yfmt:
        fig.update_yaxes(tickformat=yfmt)
    return fig


def fig_model_calibration(df: pd.DataFrame):
    """Reliability plot on the test split: mean predicted vs observed T1 bad rate per decile of score."""
    name = "model_calibration"
    fig, ax = plt.subplots(figsize=(6.5, 5.5), facecolor=SURFACE)
    _style(ax)
    hi = float(max(df["mean_pred"].max(), df["obs_rate"].max(), 0.1)) * 1.05
    ax.plot([0, hi], [0, hi], color=GREY, linestyle="--", linewidth=1, label="Perfect calibration")
    pf = go.Figure()
    pf.add_trace(
        go.Scatter(x=[0, hi], y=[0, hi], mode="lines", name="Perfect", line={"color": GREY, "dash": "dash"})
    )
    for m, g in df.groupby("model", sort=False):
        ax.plot(
            g["mean_pred"],
            g["obs_rate"],
            marker="o",
            color=MODEL_COLORS.get(m, INK),
            label=MODEL_LABELS.get(m, m),
        )
        pf.add_trace(
            go.Scatter(
                x=g["mean_pred"],
                y=g["obs_rate"],
                mode="lines+markers",
                name=MODEL_LABELS.get(m, m),
                line={"color": MODEL_COLORS.get(m, INK)},
                customdata=g[["n"]],
                hovertemplate="pred %{x:.1%}<br>observed %{y:.1%}<br>n=%{customdata[0]}",
            )
        )
    ax.set_xlabel("Mean predicted T1 bad probability", color=INK2)
    ax.set_ylabel("Observed T1 economic bad rate", color=INK2)
    ax.set_title("Calibration on the temporal test split (decile bins)", color=INK, fontsize=11, loc="left")
    ax.legend(fontsize=7, frameon=False)
    _foot(fig, name, "Test = funded 2025-07..2026-03, mature deals only")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _layout(pf, "Calibration on the temporal test split", name)
    pf.update_xaxes(tickformat=".0%", title="Mean predicted")
    pf.update_yaxes(title="Observed")
    return fig, pf


def fig_model_lift(df: pd.DataFrame):
    """Decile lift on the test split (decile 1 = highest predicted risk)."""
    name = "model_lift"
    fig, ax = plt.subplots(figsize=(9, 4.8), facecolor=SURFACE)
    _style(ax)
    models = list(dict.fromkeys(df["model"]))
    w = 0.8 / max(len(models), 1)
    pf = go.Figure()
    for i, m in enumerate(models):
        g = df[df["model"] == m]
        ax.bar(
            g["decile"] + (i - (len(models) - 1) / 2) * w,
            g["bad_rate"],
            width=w,
            color=MODEL_COLORS.get(m, INK),
            label=MODEL_LABELS.get(m, m),
        )
        pf.add_trace(
            go.Bar(
                x=g["decile"],
                y=g["bad_rate"],
                name=MODEL_LABELS.get(m, m),
                marker_color=MODEL_COLORS.get(m, INK),
                customdata=g[["lift", "n"]],
                hovertemplate="decile %{x}<br>bad rate %{y:.1%}<br>lift %{customdata[0]:.2f}<br>n=%{customdata[1]}",
            )
        )
    g0 = df[df["model"] == models[0]] if models else df
    overall = float(g0["n_bad"].sum() / g0["n"].sum()) if len(g0) else 0.0
    ax.axhline(overall, color=GREY, linestyle="--", linewidth=1)
    ax.text(10.4, overall, f"overall {overall:.1%}", color=INK2, fontsize=7, va="bottom", ha="right")
    ax.set_xticks(range(1, 11))
    ax.set_xlabel("Score decile (1 = highest predicted risk)", color=INK2)
    ax.set_ylabel("T1 economic bad rate", color=INK2)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_title("Decile lift on the temporal test split", color=INK, fontsize=11, loc="left")
    ax.legend(fontsize=7, frameon=False)
    _foot(fig, name)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    pf.add_hline(y=overall, line_dash="dash", line_color=GREY)
    _layout(pf, "Decile lift on the temporal test split", name)
    pf.update_layout(barmode="group")
    pf.update_xaxes(title="Score decile (1 = highest risk)", dtick=1)
    return fig, pf


def fig_model_naive_vs_economic(df: pd.DataFrame):
    """Logistic trained on the economic T1 target vs the same model on the naive D18 target (test split)."""
    name = "model_naive_vs_economic"
    metrics = list(dict.fromkeys(df["metric_label"]))
    models = list(dict.fromkeys(df["model"]))
    fig, ax = plt.subplots(figsize=(9, 0.6 * len(metrics) + 1.6), facecolor=SURFACE)
    _style(ax)
    ax.yaxis.grid(False)
    ax.xaxis.grid(True, color="#e6e5e1")
    h = 0.8 / max(len(models), 1)
    pf = go.Figure()
    for i, m in enumerate(models):
        g = df[df["model"] == m].set_index("metric_label").reindex(metrics)
        ys = [j + (i - (len(models) - 1) / 2) * h for j in range(len(metrics))]
        ax.barh(ys, g["value"], height=h, color=MODEL_COLORS.get(m, INK), label=MODEL_LABELS.get(m, m))
        for y, v in zip(ys, g["value"], strict=True):
            ax.text(v, y, f" {v:.2f}", ha="left", va="center", fontsize=7, color=INK2)
        pf.add_trace(
            go.Bar(
                y=metrics,
                x=g["value"],
                orientation="h",
                name=MODEL_LABELS.get(m, m),
                marker_color=MODEL_COLORS.get(m, INK),
                text=[f"{v:.2f}" for v in g["value"]],
            )
        )
    ax.set_yticks(range(len(metrics)))
    ax.set_yticklabels(metrics, fontsize=8)
    ax.invert_yaxis()
    ax.set_title(
        "Naive (D18) vs economic (D19) target: same features, test split", color=INK, fontsize=11, loc="left"
    )
    ax.legend(fontsize=7, frameon=False, loc="lower right")
    _foot(fig, name, "Distressed refi = outcome at the T1 horizon is refinanced_distressed")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _layout(pf, "Naive (D18) vs economic (D19) target", name, yfmt=None)
    pf.update_layout(barmode="group")
    pf.update_yaxes(autorange="reversed")
    return fig, pf


def fig_model_coefficients(df: pd.DataFrame):
    """Odds ratios with 95% CIs (log scale) for the logistic model on each target."""
    name = "model_coefficients"
    d = df[df["term"] != "const"]
    terms = list(dict.fromkeys(d["term"]))
    models = list(dict.fromkeys(d["model"]))
    fig, ax = plt.subplots(figsize=(8, 0.45 * len(terms) + 1.6), facecolor=SURFACE)
    _style(ax)
    ax.xaxis.grid(True, color="#e6e5e1")
    ax.yaxis.grid(False)
    pf = go.Figure()
    for i, m in enumerate(models):
        g = d[d["model"] == m].set_index("term").reindex(terms)
        ys = [j + (i - (len(models) - 1) / 2) * 0.3 for j in range(len(terms))]
        ax.errorbar(
            g["odds_ratio"],
            ys,
            xerr=[g["odds_ratio"] - g["or_ci_low"], g["or_ci_high"] - g["odds_ratio"]],
            fmt="o",
            color=MODEL_COLORS.get(m, INK),
            label=MODEL_LABELS.get(m, m),
            capsize=2,
            markersize=4,
        )
        pf.add_trace(
            go.Scatter(
                x=g["odds_ratio"],
                y=terms,
                mode="markers",
                name=MODEL_LABELS.get(m, m),
                marker={"color": MODEL_COLORS.get(m, INK)},
                error_x={
                    "type": "data",
                    "symmetric": False,
                    "array": g["or_ci_high"] - g["odds_ratio"],
                    "arrayminus": g["odds_ratio"] - g["or_ci_low"],
                },
            )
        )
    ax.axvline(1.0, color=GREY, linestyle="--", linewidth=1)
    ax.set_xscale("log")
    ax.set_xticks([0.25, 0.5, 1, 2, 4, 8])
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_yticks(range(len(terms)))
    ax.set_yticklabels(terms, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Odds ratio (95% CI, log scale)", color=INK2)
    ax.set_title("Logistic T1 model: odds ratios", color=INK, fontsize=11, loc="left")
    ax.legend(fontsize=7, frameon=False, loc="lower right")
    _foot(fig, name)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    _layout(pf, "Logistic T1 model: odds ratios (95% CI)", name, yfmt=None)
    pf.update_xaxes(type="log", title="Odds ratio")
    pf.update_yaxes(autorange="reversed")
    return fig, pf


def fig_survival_km(df: pd.DataFrame):
    """Kaplan-Meier survival without a D19 event, by renewal status and by positions (T2)."""
    name = "survival_km"
    dims = [d for d in ("renewal_status", "positions") if d in set(df["dimension"])]
    fig, axes = plt.subplots(1, len(dims), figsize=(11, 4.6), facecolor=SURFACE, sharey=True, squeeze=False)
    pf = make_subplots(
        rows=1, cols=len(dims), shared_yaxes=True, subplot_titles=[f"By {d.replace('_', ' ')}" for d in dims]
    )
    palette = [BLUE, ORANGE, AQUA, INK2, GREY]
    for k, dim in enumerate(dims):
        ax = axes[0][k]
        _style(ax)
        g = df[df["dimension"] == dim]
        for i, (s, h) in enumerate(g.groupby("segment", sort=True)):
            c = palette[i % len(palette)]
            lab = f"{s} (n={int(h['n'].iloc[0])})"
            ax.step(h["t_bdays"], h["survival"], where="post", color=c, label=lab)
            ax.fill_between(h["t_bdays"], h["ci_low"], h["ci_high"], step="post", color=c, alpha=0.12)
            pf.add_trace(
                go.Scatter(
                    x=h["t_bdays"],
                    y=h["survival"],
                    mode="lines",
                    line={"shape": "hv", "color": c},
                    name=lab,
                    legendgroup=dim,
                ),
                row=1,
                col=k + 1,
            )
        ax.set_title(f"By {dim.replace('_', ' ')}", color=INK, fontsize=10, loc="left")
        ax.set_xlabel("Business days since funding", color=INK2)
        ax.legend(fontsize=7, frameon=False)
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    axes[0][0].set_ylabel("Share without a D19 event", color=INK2)
    fig.suptitle("T2: time to economic bad (Kaplan-Meier, 95% CI)", color=INK, fontsize=11, x=0.01, ha="left")
    _foot(fig, name, "Censored at a good close or 2026-09-30")
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    _layout(pf, "T2: time to economic bad (Kaplan-Meier)", name)
    return fig, pf


MODEL_FIGURES = {
    "model_calibration": fig_model_calibration,
    "model_lift": fig_model_lift,
    "model_naive_vs_economic": fig_model_naive_vs_economic,
    "model_coefficients": fig_model_coefficients,
    "survival_km": fig_survival_km,
}

# Frames from the persisted model tables (so the app can re-render without refitting).
MODEL_DATASET_SQL = {
    "model_calibration": "SELECT model, bin, n, mean_pred, obs_rate FROM model_calibration WHERE split = 'test' "
    "AND model IN ('logit', 'gbm_isotonic', 'baseline_rule') ORDER BY model = 'logit' DESC, model, bin",
    "model_lift": "SELECT model, decile, n, n_bad, bad_rate, lift, cum_capture FROM model_lift WHERE split = 'test' "
    "AND model IN ('logit', 'gbm_isotonic', 'baseline_rule') ORDER BY model = 'logit' DESC, model, decile",
    "model_naive_vs_economic": "SELECT model, metric, metric_label, value FROM model_naive_comparison "
    "ORDER BY model = 'logit' DESC, model, sort_key",
    "model_coefficients": "SELECT model, term, coef, odds_ratio, or_ci_low, or_ci_high, p_value FROM model_coefficients "
    "ORDER BY model = 'logit' DESC, model, term_order",
    "survival_km": "SELECT dimension, segment, t_bdays, survival, ci_low, ci_high, at_risk, n, events FROM survival_km "
    "ORDER BY dimension, segment, t_bdays",
}
MODEL_DATASETS = {k: (lambda con, _s=v: con.execute(_s).df()) for k, v in MODEL_DATASET_SQL.items()}


def render(name: str, df: pd.DataFrame, out_dir: Path) -> dict[str, str]:
    out_dir = Path(out_dir)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    mfig, pfig = MODEL_FIGURES[name](df)
    png, pj, csv = out_dir / f"{name}.png", out_dir / f"{name}.plotly.json", out_dir / "data" / f"{name}.csv"
    mfig.savefig(png, dpi=130, facecolor=mfig.get_facecolor())
    plt.close(mfig)
    pj.write_text(pfig.to_json(), encoding="utf-8")
    df.to_csv(csv, index=False, lineterminator="\n")
    return {"png": png.as_posix(), "plotly": pj.as_posix(), "csv": csv.as_posix()}
