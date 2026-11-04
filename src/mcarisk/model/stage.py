"""Stage 6 ``model``: T1 models, T2 survival, T3 relationships, figures and the model card.
``python -m mcarisk.model``.

Reads ``deals_resolved``, ``scorecard_links``, ``scorecards_std`` and the ``ledger`` (features, through
``mcarisk.model.features`` only), plus the label / outcome tables ``deal_targets`` (T1), ``deal_metrics``
(T2) and ``relationship_metrics`` (T3). Writes:

  model_features          one row per deal: features, split, labels, excluded_reason, lineage
  model_predictions       one row per (deal, model): score, split, labels (scored deals only)
  model_performance       long: model, trained_on, eval_target, split, metric, value
  model_coefficients      logistic coefficient tables (coef, CI, odds ratio) for ``logit`` / ``logit_naive``
  model_lift              decile lift per model and split (val, test)
  model_calibration       reliability bins per model and split (val, test)
  model_naive_comparison  economic vs naive target on the test split
  model_fairness          test-split calibration / AUC by state and industry
  survival_km, survival_cox, t3_summary
  build/figures/<model figures>.{png,plotly.json} + data/*.csv, build/model_card.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from mcarisk import paths
from mcarisk.manifest import RunManifest, StageRecorder, open_run
from mcarisk.metrics.deal import AS_OF_DEFAULT
from mcarisk.metrics.targets import t2_survival_rows, t3_relationship_rows
from mcarisk.model import figures as mfig
from mcarisk.model.evaluate import auc, decile_table, performance, reliability_bins
from mcarisk.model.features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, build_features
from mcarisk.model.survival import t2_models, t3_model
from mcarisk.model.train import (
    GBM_PARAMS,
    LOGIT_FEATURES,
    PRIMARY_MODEL,
    SPLITS,
    TARGETS,
    TEST_END,
    TEST_START,
    TRAIN_END,
    VAL_END,
    VAL_START,
    fit_all,
    model_frame,
)

FIG_DIR = paths.BUILD_DIR / "figures"
MODEL_CARD = paths.BUILD_DIR / "model_card.md"
TRAINED_ON = {
    "baseline_rule": "economic",
    "logit": "economic",
    "logit_uncalibrated": "economic",
    "gbm": "economic",
    "gbm_isotonic": "economic",
    "logit_naive": "naive",
}


@dataclass
class ModelResult:
    tables: dict[str, int]
    headline: dict[str, Any]
    figures: dict[str, dict[str, str]]
    seconds: float


def _labelled(frame: pd.DataFrame, preds: pd.DataFrame) -> pd.DataFrame:
    lab = frame[
        [
            "deal_id",
            "split",
            "excluded_reason",
            "t1_bad_economic",
            "t1_bad_naive",
            "outcome_at_horizon",
            "state",
            "industry",
        ]
    ]
    p = preds.merge(lab, on="deal_id", how="left")
    return p[p["excluded_reason"].isna()].copy()


def evaluate_all(frame: pd.DataFrame, preds: pd.DataFrame) -> dict[str, pd.DataFrame]:
    P = _labelled(frame, preds)
    perf, lift, cal = [], [], []
    for model, g in P.groupby("model", sort=False):
        for split in SPLITS:
            s = g[g["split"] == split]
            if s.empty:
                continue
            evals = ("economic", "naive") if model in ("logit", "logit_naive") else ("economic",)
            for et in evals:
                y = s[TARGETS[et]].astype(int).to_numpy()
                for k, v in performance(y, s["score"].to_numpy()).items():
                    perf.append(
                        {
                            "model": model,
                            "trained_on": TRAINED_ON[model],
                            "eval_target": et,
                            "split": split,
                            "metric": k,
                            "value": v,
                        }
                    )
            if split in ("val", "test"):
                y = s[TARGETS["economic"]].astype(int)
                lt = decile_table(pd.DataFrame({"deal_id": s["deal_id"], "y": y, "score": s["score"]}))
                lt.insert(0, "split", split)
                lt.insert(0, "model", model)
                lift.append(lt)
                rb = reliability_bins(y, s["score"])
                rb.insert(0, "split", split)
                rb.insert(0, "model", model)
                cal.append(rb)
    return {
        "model_performance": pd.DataFrame(perf),
        "model_lift": pd.concat(lift, ignore_index=True),
        "model_calibration": pd.concat(cal, ignore_index=True),
    }


NAIVE_METRICS = (
    ("auc_economic", "AUC vs economic T1"),
    ("auc_naive", "AUC vs naive D18"),
    ("recall_all_bad_topk", "Recall of economic bads in top k"),
    ("recall_distressed_refi_topk", "Recall of distressed refis in top k"),
    ("mean_pct_rank_distressed_refi", "Mean risk percentile of distressed refis"),
    ("mean_pred", "Mean predicted probability"),
    ("observed_economic_bad_rate", "Observed economic bad rate"),
    ("mean_pred_distressed_refi", "Mean predicted probability, distressed refis"),
)


def naive_comparison(frame: pd.DataFrame, preds: pd.DataFrame) -> pd.DataFrame:
    """Economic vs naive target (test split): k = number of economic-bad test deals; 'distressed refi' =
    outcome at the T1 horizon is ``refinanced_distressed`` (bad under D19, good under D18)."""
    P = _labelled(frame, preds)
    P = P[(P["split"] == "test") & P["model"].isin(["logit", "logit_naive"])]
    rows = []
    for model, g in P.groupby("model", sort=False):
        g = g.sort_values(["score", "deal_id"], ascending=[False, True]).reset_index(drop=True)
        econ = g["t1_bad_economic"].astype(int)
        k = int(econ.sum())
        top = g.index < k
        dr = g["outcome_at_horizon"] == "refinanced_distressed"
        pct = g["score"].rank(pct=True, method="average")
        vals = {
            "auc_economic": auc(econ, g["score"]),
            "auc_naive": auc(g["t1_bad_naive"].astype(int), g["score"]),
            "recall_all_bad_topk": float((top & (econ == 1)).sum() / max(k, 1)),
            "recall_distressed_refi_topk": float((top & dr).sum() / max(int(dr.sum()), 1)),
            "mean_pct_rank_distressed_refi": float(pct[dr].mean()) if dr.any() else None,
            "mean_pred": float(g["score"].mean()),
            "observed_economic_bad_rate": float(econ.mean()),
            "mean_pred_distressed_refi": float(g.loc[dr, "score"].mean()) if dr.any() else None,
        }
        for i, (key, label) in enumerate(NAIVE_METRICS):
            rows.append(
                {
                    "model": model,
                    "metric": key,
                    "metric_label": label,
                    "value": vals[key],
                    "sort_key": i,
                    "k": k,
                    "n": len(g),
                    "n_distressed_refi": int(dr.sum()),
                }
            )
    return pd.DataFrame(rows)


def fairness(frame: pd.DataFrame, preds: pd.DataFrame, model: str = PRIMARY_MODEL) -> pd.DataFrame:
    P = _labelled(frame, preds)
    P = P[(P["split"] == "test") & (P["model"] == model)]
    rows = []
    for dim in ("state", "industry"):
        for seg, g in P.groupby(P[dim].fillna("unknown")):
            y = g["t1_bad_economic"].astype(int)
            rows.append(
                {
                    "model": model,
                    "dimension": dim,
                    "segment": seg,
                    "n": len(g),
                    "observed_bad_rate": float(y.mean()),
                    "mean_pred": float(g["score"].mean()),
                    "auc": auc(y, g["score"]) if len(g) >= 20 else None,
                }
            )
    return pd.DataFrame(rows)


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _write(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> int:
    con.register("_m_tmp", df)
    try:
        con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _m_tmp')
    finally:
        con.unregister("_m_tmp")
    return len(df)


def _metric(perf: pd.DataFrame, model: str, split: str, metric: str, et: str = "economic") -> float | None:
    s = perf[
        (perf.model == model) & (perf.split == split) & (perf.metric == metric) & (perf.eval_target == et)
    ]
    return None if s.empty or pd.isna(s["value"].iloc[0]) else float(s["value"].iloc[0])


def run_model(
    db_path: Path = paths.DB_PATH,
    *,
    fig_dir: Path = FIG_DIR,
    model_card: Path | None = MODEL_CARD,
    run: RunManifest | None = None,
    as_of: date = AS_OF_DEFAULT,
) -> ModelResult:
    t0 = time.perf_counter()
    con = duckdb.connect(str(db_path))
    try:
        feats = build_features(con)
        tg = con.execute(
            "SELECT deal_id, mature, t1_bad_economic, t1_bad_naive, outcome_at_horizon FROM deal_targets"
        ).df()
        frame = model_frame(feats, tg.to_dict("records"))
        fit = fit_all(frame)
        preds = fit["predictions"]
        ev = evaluate_all(frame, preds)
        naive = naive_comparison(frame, preds)
        fair = fairness(frame, preds)

        coefs = []
        for m, t in fit["coefficients"].items():
            t = t.copy()
            t.insert(0, "model", m)
            t["term_order"] = range(len(t))
            coefs.append(t)
        coefs = pd.concat(coefs, ignore_index=True)

        dm_rows = _rows(
            con,
            "SELECT deal_id, funding_date, outcome, outcome_date, d16_default, d16_default_date, "
            "d19_bad_economic FROM deal_metrics",
        )
        surv_rows = t2_survival_rows(dm_rows, as_of=as_of)
        t2 = t2_models(frame, surv_rows, fit["medians"])
        rel_rows = _rows(
            con,
            "SELECT merchant_key, first_funding_date, r5_status, r9_relationship_bad, r10_days_to_resolution "
            "FROM relationship_metrics",
        )
        t3 = t3_model(frame, t3_relationship_rows(rel_rows, as_of=as_of), fit["medians"])

        lin = con.execute("SELECT deal_id, src_file, src_sheet, src_row FROM deals_resolved").df()
        feat_tbl = frame.merge(lin, on="deal_id", how="left")
        pred_tbl = preds.merge(
            frame[
                [
                    "deal_id",
                    "funding_date",
                    "split",
                    "mature",
                    "excluded_reason",
                    "t1_bad_economic",
                    "t1_bad_naive",
                    "outcome_at_horizon",
                ]
            ],
            on="deal_id",
            how="left",
        )
        pred_tbl.insert(2, "trained_on", pred_tbl["model"].map(TRAINED_ON))
        pred_tbl = pred_tbl.drop(columns=["target"])
        t3_tbl = pd.DataFrame([{"key": k, "value": json.dumps(v)} for k, v in t3.items()])
        cox = t2["cox"].copy()
        cox.insert(0, "concordance", t2["cox_concordance"])

        con.execute("BEGIN TRANSACTION")
        counts = {
            "model_features": _write(con, "model_features", feat_tbl),
            "model_predictions": _write(con, "model_predictions", pred_tbl),
            "model_performance": _write(con, "model_performance", ev["model_performance"]),
            "model_coefficients": _write(con, "model_coefficients", coefs),
            "model_lift": _write(con, "model_lift", ev["model_lift"]),
            "model_calibration": _write(con, "model_calibration", ev["model_calibration"]),
            "model_naive_comparison": _write(con, "model_naive_comparison", naive),
            "model_fairness": _write(con, "model_fairness", fair),
            "survival_km": _write(con, "survival_km", t2["km"]),
            "survival_cox": _write(con, "survival_cox", cox),
            "t3_summary": _write(con, "t3_summary", t3_tbl),
        }
        con.execute("COMMIT")
        figs = {name: mfig.render(name, fn(con), fig_dir) for name, fn in mfig.MODEL_DATASETS.items()}
    finally:
        con.close()

    perf = ev["model_performance"]
    excl = frame["excluded_reason"].fillna("in_sample")
    split_counts = {
        s: {r: int(((frame["split"] == s) & (excl == r)).sum()) for r in sorted(excl.unique())}
        for s in [*SPLITS, "out_of_window"]
    }
    headline = {
        "primary_model": PRIMARY_MODEL,
        "split_counts": split_counts,
        "n_train": fit["n_train"],
        "n_val": fit["n_val"],
        "test": {
            m: {
                k: _metric(perf, m, "test", k)
                for k in (
                    "n",
                    "bad_rate",
                    "auc",
                    "brier",
                    "calibration_slope",
                    "calibration_intercept",
                    "top_decile_lift",
                )
            }
            for m in TRAINED_ON
        },
        "val": {
            m: {k: _metric(perf, m, "val", k) for k in ("auc", "brier", "calibration_slope")}
            for m in TRAINED_ON
        },
        "naive_comparison": {f"{r.model}.{r.metric}": r.value for r in naive.itertuples()},
        "t2": {"n": t2["n"], "events": t2["events"], "cox_concordance": t2["cox_concordance"]},
        "t3": {k: v for k, v in t3.items() if k != "coefficients"},
    }
    if model_card is not None:
        Path(model_card).parent.mkdir(parents=True, exist_ok=True)
        Path(model_card).write_text(
            render_model_card(frame, fit, perf, coefs, naive, fair, t2, t3, headline, as_of), encoding="utf-8"
        )
    res = ModelResult(counts, headline, figs, round(time.perf_counter() - t0, 3))
    if run is not None:
        st = StageRecorder(run.data["stages"]["model"])
        for k, v in counts.items():
            st.table(k, accepted=v)
        st.extra("headline", headline)
        st.extra("figures", {k: v["png"] for k, v in figs.items()})
        st.extra("figure_metric_ids", mfig.MODEL_FIGURE_METRICS)
        st.extra("model_card", None if model_card is None else Path(model_card).as_posix())
    return res


# ------------------------------------------------------------------------------------------ model card
def _f(v, fmt: str = ".3f") -> str:
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else format(v, fmt)


def _naive_sentence(nv: pd.DataFrame) -> str:
    """Data-driven reading of the naive vs economic comparison (no claim the numbers do not support)."""

    def g(label: str, m: str) -> float | None:
        return float(nv.loc[label, m]) if label in nv.index and m in nv.columns else None

    pr_e, pr_n = (
        g("Mean risk percentile of distressed refis", "logit"),
        g("Mean risk percentile of distressed refis", "logit_naive"),
    )
    mp_e, mp_n = g("Mean predicted probability", "logit"), g("Mean predicted probability", "logit_naive")
    dr_e, dr_n = (
        g("Mean predicted probability, distressed refis", "logit"),
        g("Mean predicted probability, distressed refis", "logit_naive"),
    )
    obs = g("Observed economic bad rate", "logit")
    if None in (pr_e, pr_n, mp_e, mp_n, dr_e, dr_n, obs):
        return ""
    rank = (
        "ranks them *lower*"
        if pr_n < pr_e - 0.01
        else ("ranks them about the same" if abs(pr_n - pr_e) <= 0.01 else "ranks them *higher*")
    )
    return (
        f"On ranking, the naive model {rank} (mean risk percentile {pr_n:.2f} vs {pr_e:.2f}): distressed "
        "refinances, modifications and defaults share the same distress drivers, so a D18 model still orders "
        "them similarly. The damage is in the **level**: the naive model predicts a mean probability of "
        f"{mp_n:.1%} against an observed economic bad rate of {obs:.1%} (economic model {mp_e:.1%}), and "
        f"{dr_n:.1%} vs {dr_e:.1%} for the distressed refinances themselves. Used for pricing, limits or "
        "loss reserves, it would understate economic risk by roughly the share of distressed refinances."
    )


def render_model_card(frame, fit, perf, coefs, naive, fair, t2, t3, h, as_of) -> str:
    sc = h["split_counts"]
    tm = frame[frame["tracker_missing"]]
    tm_mature = tm[tm["mature"].astype(bool)]
    lines = [
        "# Model card: T1 economic-bad risk at origination",
        "",
        f"Generated by `make model` (stage `model`, `src/mcarisk/model/`). As of {as_of}. METRICS.md v1.2. "
        "Every number below is computed from the build; the E8 eval (`evals/reports/latest.md`) adds the "
        "ceiling AUC from the simulator's true hazard, which pipeline code may not read.",
        "",
        "## Intended use",
        "- Rank **new applications and renewals at the funding date** by the probability that the deal becomes "
        "economically bad (METRICS.md §5 **T1**: D19 within 180 Fed ACH business days of funding).",
        "- Support underwriting review queues, pricing discussions and portfolio monitoring of the *mix* of risk "
        "being written. It is a decision aid for a human underwriter, not an automated approve / decline rule.",
        "",
        "## What NOT to use it for (yet)",
        "- **Not** for automated declines or pricing without a human, and not as a probability of loss in dollars "
        "(T1 is a binary event; severity is not modelled).",
        "- **Not** for monitoring open deals after funding: features are frozen at the funding date by design. "
        "Use D13 / D14 / D16 servicing metrics for that.",
        "- **Not** as a stable absolute probability across regimes: the training window contains the "
        "2024Q3-2025Q1 deterioration, so levels drift (see Calibration). Re-fit and re-calibrate before use.",
        "- **Not** on deals with no tracker row (Q5 `tracker_missing`): they have no origination features.",
        "",
        "## Data",
        "- Source: `model_features` (features, `mcarisk.model.features`) joined to `deal_targets` (T1 labels).",
        "- **Temporal split by funding date:** train <= "
        f"{TRAIN_END}; validation {VAL_START}..{VAL_END}; test {TEST_START}..{TEST_END}. Only **mature** deals "
        "(T1 horizon on or before the as-of date) are labelled.",
        "",
        "| Split | Labelled (in sample) | Immature (excluded) | Tracker-missing (excluded) |",
        "|---|---|---|---|",
    ]
    for s in [*SPLITS, "out_of_window"]:
        c = sc[s]
        lines.append(
            f"| {s} | {c.get('in_sample', 0)} | {c.get('immature', 0)} | {c.get('tracker_missing', 0)} |"
        )
    lines += [
        "",
        f"- Deals funded after {TEST_END} (`out_of_window`) are scored but never evaluated (none is mature).",
        f"- **Tracker-missing deals ({len(tm)})** are excluded, not imputed: they have no contract terms, ISO or "
        f"underwriting fields at all. {len(tm_mature)} of them are mature; their T1 economic bad rate is "
        f"{_f(float(tm_mature['t1_bad_economic'].astype(float).mean()) if len(tm_mature) else None, '.1%')}.",
        "- Missing underwriting fields (about 6% per field, independently, in the 2025+ tracker) are imputed with "
        "the training median, with the indicator `uw_missing`.",
        "- Time in business is harmonized to months: tracker `tib_months` (integer) or scorecard `tib_years` x 12 "
        "(1 decimal of a year, so +/- 0.6 months). The model uses log(months / 24).",
        "",
        "## Features and leakage guard",
        "- Origination-only (METRICS.md §5): FICO, average monthly revenue, NSFs (90 days), positions, time in "
        "business, industry, state, contract terms, ISO, and **burden** = daily equivalent x 21 / average monthly "
        "revenue. For renewals: the prior deal's D13 pace, D14 return rate (last 20 attempts) and payment ratio "
        "**as of the funding date, with no look-ahead** (returns posting after the funding date are ignored; D17 "
        "values, which look up to 2 business days ahead, are never used).",
        "- Never used: tracker `status`, outcomes, metric / target tables, or any payment dated after funding. "
        "Tests: perturbing every post-funding ledger row, collections row, tracker status and outcome leaves the "
        "features unchanged; the feature SQL is checked for forbidden inputs (`tests/test_model_unit.py`, "
        "`tests/test_model_build.py`).",
        f"- Logistic inputs: {', '.join(f'`{x}`' for x in LOGIT_FEATURES)}.",
        "",
        "## Target choice",
        "- **T1 economic** (D19 = defaulted / settled / written off **or refinanced distressed**) at a fixed "
        "180-business-day horizon. The fixed horizon avoids the censoring bias of closed-deals-only bad rates.",
        "- The **naive** D18 target ignores distressed refinances, which are the risk renewals hide. The same "
        "logistic specification trained on D18 (`logit_naive`) is shown for contrast:",
        "",
        "| Test split | logit (economic) | logit_naive (D18) |",
        "|---|---|---|",
    ]
    nv = naive.pivot(index="metric_label", columns="model", values="value")
    for _, label in NAIVE_METRICS:
        if label in nv.index:
            lines.append(
                f"| {label} | {_f(nv.loc[label].get('logit'))} | {_f(nv.loc[label].get('logit_naive'))} |"
            )
    k = int(naive["k"].iloc[0]) if len(naive) else 0
    ndr = int(naive["n_distressed_refi"].iloc[0]) if len(naive) else 0
    lines += [
        "",
        f"k = {k} (the number of economic-bad test deals); {ndr} test deals were refinanced distressed by their "
        "horizon. The naive model is trained to treat a distressed refinance as a *good* outcome.",
        _naive_sentence(nv),
        "",
        "## Performance (mature deals; economic T1 target)",
        "",
        "| Model | Val AUC | Test AUC | Test Brier | Test calibration slope | Test intercept | Test top-decile lift |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in TRAINED_ON:
        t, v = h["test"][m], h["val"][m]
        lines.append(
            f"| `{m}` | {_f(v['auc'])} | {_f(t['auc'])} | {_f(t['brier'])} | {_f(t['calibration_slope'], '.2f')} | "
            f"{_f(t['calibration_intercept'], '.2f')} | {_f(t['top_decile_lift'], '.2f')} |"
        )
    tl = h["test"][PRIMARY_MODEL]
    lines += [
        "",
        f"- Primary model: `{PRIMARY_MODEL}`: the logistic regression (specification fixed before looking at "
        "the test split) followed by a validation-fitted logistic recalibration (below). Test n = "
        f"{_f(tl['n'], '.0f')}, bad rate {_f(tl['bad_rate'], '.1%')}. `logit_naive` is scored here against the "
        "economic target.",
        "- **Ceiling**: plan.md quotes a logistic origination-only temporal-test AUC of about 0.76 and a true-hazard "
        "ceiling of about 0.79. Those figures do not hold on this test window. E8 computes, on the same test "
        "deals, the true-hazard AUC and the AUC of the true coefficients on observable inputs only (the "
        "*observable ceiling*); the AUC gate is observable ceiling - 0.03 (user-approved 2026-11-04). See "
        "`evals/reports/latest.md`.",
        "",
        "## Calibration",
        "- Calibration slope / intercept: logistic recalibration of the outcome on logit(prediction) "
        "(slope 1 / intercept 0 = perfect).",
        f"- **`{PRIMARY_MODEL}` is recalibrated on the validation split** (Platt / logistic recalibration "
        f"p = sigmoid(a + b logit(p_raw)), a = {fit['logit_recalibration']['intercept']:+.3f}, "
        f"b = {fit['logit_recalibration']['slope']:.3f}, fit on validation only, never on test). "
        "**This method was chosen after the test split had been read** (user-approved 2026-11-04): the "
        "uncalibrated model's test slope was "
        f"{_f(_metric(perf, 'logit_uncalibrated', 'test', 'calibration_slope'), '.2f')} (validation "
        f"{_f(_metric(perf, 'logit_uncalibrated', 'val', 'calibration_slope'), '.2f')}). The uncalibrated "
        "scores are kept as `logit_uncalibrated` (report-only). Recalibration is monotone, so AUC and lift "
        "are unchanged.",
        f"- After recalibration: test slope {_f(_metric(perf, PRIMARY_MODEL, 'test', 'calibration_slope'), '.2f')}, "
        f"intercept {_f(_metric(perf, PRIMARY_MODEL, 'test', 'calibration_intercept'), '+.2f')}, "
        f"calibration-in-the-large {_f(_metric(perf, PRIMARY_MODEL, 'test', 'calibration_in_the_large'), '+.3f')}. "
        "The uncalibrated model was too spread out for later vintages (fit on about 500 deals from the "
        "2024Q3-2025Q1 deterioration). `gbm_isotonic` (isotonic on about 320 validation deals) does not "
        "transfer as well. Plot: `build/figures/model_calibration.png`. Re-calibrate on the most recent "
        "mature window before any use of the probability level.",
        "",
        "## Logistic coefficients (economic T1, training split)",
        "",
        "| Term | Coef | Odds ratio | 95% CI (OR) | p |",
        "|---|---|---|---|---|",
    ]
    for r in coefs[coefs.model == "logit"].itertuples():
        lines.append(
            f"| `{r.term}` | {r.coef:+.3f} | {r.odds_ratio:.2f} | {r.or_ci_low:.2f}-{r.or_ci_high:.2f} | {r.p_value:.3f} |"
        )
    lines += [
        "",
        "- **Burden is modelled with renewal status and term.** Overall, burden looks *inverted* (Part 5): "
        "distressed refinances are written at a lower payment, and high-burden deals are short deals that close "
        "before distress can show within the horizon. Splitting renewals by the as-of-funding prior-deal stress "
        "rule and adding log(term) recovers a positive burden effect, as in the data-generating process.",
        "",
        "## T2 survival and T3 relationships",
        f"- T2 (time to the D19 event, censored at a good close or {as_of}): Kaplan-Meier by renewal status, "
        f"positions and burden (`survival_km`, `build/figures/survival_km.png`); Cox model on the logistic inputs, "
        f"n = {t2['n']}, events = {t2['events']}, concordance {_f(t2['cox_concordance'])} (descriptive, all deals).",
        f"- T3 (relationship R9, resolved within 18 months of first funding): population {t3.get('n_population')} of "
        f"{t3.get('n_observable')} observable relationships ({t3.get('n_scored')} scored; bad rate "
        f"{_f(t3.get('bad_rate'), '.1%')}); first-deal logistic AUC train {_f(t3.get('auc_train'))}, temporal test "
        f"{_f(t3.get('auc_test'))} (n = {t3.get('n_test')}). **Survivorship caveat:** relationships still open at "
        "18 months (long chains) are not in the population, so T3 under-represents them.",
        "",
        "## Fairness considerations",
        "- No protected attributes are used or available. The only geographic / sector inputs are **state** "
        "(not used by any model; reported for monitoring) and **industry** (gradient boosting only). Test-split observed vs predicted "
        "bad rate by group (`model_fairness`):",
        "",
        "| Dimension | Segment | n | Observed | Mean predicted | AUC |",
        "|---|---|---|---|---|---|",
    ]
    for r in fair.itertuples():
        lines.append(
            f"| {r.dimension} | {r.segment} | {r.n} | {r.observed_bad_rate:.1%} | {r.mean_pred:.1%} | {_f(r.auc, '.2f')} |"
        )
    lines += [
        "",
        "- Groups are small (wide uncertainty). State can proxy for local economic conditions; review any "
        "use of state or industry with compliance before deployment.",
        "",
        "## Limits",
        "- Synthetic book with a known data-generating process; the latent merchant quality that drives much of "
        "the risk is not observable at origination, which caps achievable AUC.",
        "- Small samples: about 500 training deals; coefficient CIs are wide.",
        "- T1 counts a deal still open at its horizon as good.",
        "- The D17 stress rule used as a renewal feature is evaluated without look-ahead, so it is a slightly "
        "weaker signal than the D17 label itself.",
        f"- GBM hyperparameters are fixed (`{json.dumps(GBM_PARAMS)}`), not tuned.",
        f"- Feature table columns: numeric {len(NUMERIC_FEATURES)}, categorical {', '.join(CATEGORICAL_FEATURES)}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T1/T2/T3 risk models + model card (Part 6).")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[model] {args.db} not found; run `make metrics` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("model"):
        res = run_model(args.db, run=run)
    print(f"[model] done in {res.seconds:.2f}s -> {args.db}, {MODEL_CARD}")
    for m, v in res.headline["test"].items():
        print(f"  test {m:<14} " + "  ".join(f"{k}={_f(x)}" for k, x in v.items()))
    print(f"  T2 cox concordance {_f(res.headline['t2']['cox_concordance'])}; T3 {res.headline['t3']}")
    return 0
