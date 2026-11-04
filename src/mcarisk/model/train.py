"""T1 models (METRICS.md §5): temporal split, design matrices, baseline rule, logistic, gradient boosting.

**Temporal split** (plan.md §5, by funding date): train <= 2024-12-31; validation 2025-01-01..2025-06-30;
test 2025-07-01..2026-03-31. Deals funded after 2026-03-31 are ``out_of_window``. Only **mature** deals
(T1 horizon on or before the as-of date) carry a label; immature test deals are excluded from every
measure and counted.

**Tracker-missing deals** (Q5, 44) have no contract terms, ISO or underwriting fields at all, so they are
**excluded** from fitting and scoring (``excluded_reason = tracker_missing``) and counted, rather than
imputed: an all-imputed row would only teach the model the data-entry gap.

**Missing underwriting fields** (about 6% per field in the 2025+ tracker, independently): imputed with
the *training-split* median, plus the indicator ``uw_missing`` (any of the five fields missing).

**Models** (all deterministic):

* ``baseline_rule``: points = min(positions, 3) + [burden >= 25%] + [burden >= 35%]; the probability of
  each points value is the smoothed training bad rate (k + 1) / (n + 2).
* ``logit`` (primary): statsmodels Logit on ``LOGIT_FEATURES``; coefficient table with odds ratios and
  95% Wald CIs. **Burden is modelled together with renewal status and term**: renewals are split by the
  as-of-funding prior-deal stress rule (``renewal_stressed_asof`` / ``renewal_healthy_asof``), because
  distressed refinances are written at a *lower* payment (Part 5: burden is inverted overall); and the
  contract term is the deal's exposure time within the 180-business-day horizon (high-burden deals are
  short deals that close before distress can hit). With both, burden's coefficient is positive, as in
  the data-generating process.
* ``gbm``: sklearn ``HistGradientBoostingClassifier`` (fixed hyperparameters, ``random_state=0``, no early
  stopping) on the logistic inputs plus the raw prior-deal features, contract terms and industry
  (categorical); ``gbm_isotonic`` = the same scores mapped through an isotonic regression fit on the
  **validation** split.
* ``logit_naive``: the ``logit`` specification trained on the naive target (D18 at the horizon), to show
  how it under-ranks distressed-refinance risk.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

TRAIN_END = date(2024, 12, 31)
VAL_START, VAL_END = date(2025, 1, 1), date(2025, 6, 30)
TEST_START, TEST_END = date(2025, 7, 1), date(2026, 3, 31)
SPLITS = ("train", "val", "test")
SEED = 0

TARGETS = {"economic": "t1_bad_economic", "naive": "t1_bad_naive"}
IMPUTE = ("fico", "avg_monthly_rev_cents", "nsf_90d", "positions", "tib_months", "burden", "term_bdays")

LOGIT_FEATURES = (
    "fico_per50",  # (FICO - 650) / 50
    "log_tib_24m",  # log(time in business in months / 24)
    "positions",  # number of other MCA positions at funding (capped at 5)
    "nsf_90d",  # NSFs in the last 90 days (capped at 10)
    "burden_per10pp",  # (burden - 15%) / 10 pp; burden = daily equivalent x 21 / avg monthly revenue
    "renewal_healthy_asof",  # renewal whose prior deal passes the stress rule as of funding
    "renewal_stressed_asof",  # renewal whose prior deal fails it (D13 < 0.85 or D14 >= 3/20 or mod), no look-ahead
    "log_term_120",  # log(contract term in business days / 120): exposure within the horizon
    "uw_missing",  # any underwriting field imputed
)
GBM_NUMERIC = (
    "fico",
    "tib_months",
    "positions",
    "nsf_90d",
    "burden",
    "term_bdays",
    "is_renewal",
    "chain_seq",
    "prior_pace_asof",
    "prior_return_rate_asof",
    "prior_payment_ratio",
    "prior_term_elapsed",
    "prior_stress_rule_asof",
    "factor",
    "is_weekly",
    "log_advance",
    "commission_rate",
)
GBM_PARAMS = {
    "learning_rate": 0.05,
    "max_iter": 150,
    "max_depth": 3,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": SEED,
}


def split_of(funding_date: date) -> str:
    """Temporal split by funding date (plan.md §5)."""
    if funding_date <= TRAIN_END:
        return "train"
    if VAL_START <= funding_date <= VAL_END:
        return "val"
    if TEST_START <= funding_date <= TEST_END:
        return "test"
    return "out_of_window"


def model_frame(features: list[dict[str, Any]], targets: list[dict[str, Any]]) -> pd.DataFrame:
    """Features joined to T1 labels, with ``split`` and ``excluded_reason`` (None = labelled, in a split)."""
    f = pd.DataFrame(features)
    t = pd.DataFrame(targets)[["deal_id", "mature", "t1_bad_economic", "t1_bad_naive", "outcome_at_horizon"]]
    df = f.merge(t, on="deal_id", how="left")
    df["split"] = [split_of(d) for d in df["funding_date"]]
    reason = np.where(
        df["tracker_missing"],
        "tracker_missing",
        np.where(
            df["split"] == "out_of_window",
            "out_of_window",
            np.where(~df["mature"].astype(bool), "immature", ""),
        ),
    )
    df["excluded_reason"] = pd.Series(reason, index=df.index).replace("", None)
    df["log_advance"] = np.log(df["advance_cents"].astype(float) / 1e6)
    return df.sort_values("deal_id").reset_index(drop=True)


def fit_medians(train: pd.DataFrame) -> dict[str, float]:
    return {k: float(train[k].astype(float).median()) for k in IMPUTE}


def logit_design(df: pd.DataFrame, medians: dict[str, float]) -> pd.DataFrame:
    g = {k: df[k].astype(float).fillna(medians[k]) for k in IMPUTE}
    ren = df["is_renewal"].astype(float)
    stress = df["prior_stress_rule_asof"].astype(float).fillna(0.0) * ren
    x = pd.DataFrame(
        {
            "fico_per50": (g["fico"] - 650) / 50,
            "log_tib_24m": np.log(g["tib_months"].clip(lower=1) / 24),
            "positions": g["positions"].clip(upper=5),
            "nsf_90d": g["nsf_90d"].clip(upper=10),
            "burden_per10pp": (g["burden"] - 0.15) / 0.10,
            "renewal_healthy_asof": ren - stress,
            "renewal_stressed_asof": stress,
            "log_term_120": np.log(g["term_bdays"] / 120),
            "uw_missing": df["uw_missing"].astype(float),
        },
        index=df.index,
    )
    return x[list(LOGIT_FEATURES)]


def fit_logit(train: pd.DataFrame, target: str, medians: dict[str, float]):
    X = sm.add_constant(logit_design(train, medians), has_constant="add")
    return sm.Logit(train[target].astype(float), X).fit(disp=0, maxiter=200, method="newton")


def predict_logit(res, df: pd.DataFrame, medians: dict[str, float]) -> np.ndarray:
    X = sm.add_constant(logit_design(df, medians), has_constant="add")
    return np.asarray(res.predict(X), dtype=float)


def coef_table(res) -> pd.DataFrame:
    ci = res.conf_int(alpha=0.05)
    t = pd.DataFrame(
        {
            "term": res.params.index,
            "coef": res.params.values,
            "std_err": res.bse.values,
            "z": res.tvalues.values,
            "p_value": res.pvalues.values,
            "ci_low": ci[0].values,
            "ci_high": ci[1].values,
        }
    )
    t["odds_ratio"] = np.exp(t["coef"])
    t["or_ci_low"] = np.exp(t["ci_low"])
    t["or_ci_high"] = np.exp(t["ci_high"])
    return t


# ---------------------------------------------------------------------------------------- baseline
def baseline_points(df: pd.DataFrame) -> np.ndarray:
    pos = df["positions"].astype(float).fillna(0).clip(upper=3)
    b = df["burden"].astype(float)
    return (pos + (b >= 0.25).astype(float) + (b >= 0.35).astype(float)).to_numpy()


def fit_baseline(train: pd.DataFrame, target: str) -> dict[float, float]:
    pts = baseline_points(train)
    y = train[target].astype(float).to_numpy()
    return {float(p): float((y[pts == p].sum() + 1) / ((pts == p).sum() + 2)) for p in np.unique(pts)}


def predict_baseline(table: dict[float, float], df: pd.DataFrame, prior: float) -> np.ndarray:
    return np.array([table.get(float(p), prior) for p in baseline_points(df)], dtype=float)


# ---------------------------------------------------------------------------------------- GBM
def gbm_design(df: pd.DataFrame, industries: list[str]) -> np.ndarray:
    num = df[list(GBM_NUMERIC)].astype(float).to_numpy()
    code = {k: i for i, k in enumerate(industries)}
    ind = np.array(
        [code.get(v, np.nan) if isinstance(v, str) else np.nan for v in df["industry"]], dtype=float
    )
    return np.column_stack([num, ind])


def fit_gbm(train: pd.DataFrame, target: str, industries: list[str]) -> HistGradientBoostingClassifier:
    cat = [False] * len(GBM_NUMERIC) + [True]
    m = HistGradientBoostingClassifier(categorical_features=cat, **GBM_PARAMS)
    m.fit(gbm_design(train, industries), train[target].astype(int).to_numpy())
    return m


def fit_isotonic(scores: np.ndarray, y: np.ndarray) -> IsotonicRegression:
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(scores, y.astype(float))
    return iso


# ---------------------------------------------------------------------------------------- all
MODELS = ("baseline_rule", "logit", "gbm", "gbm_isotonic", "logit_naive")
PRIMARY_MODEL = "logit"


def fit_all(frame: pd.DataFrame) -> dict[str, Any]:
    """Fit every model on the training split (isotonic on validation) and score every in-scope deal.

    Returns {"predictions": long DataFrame (deal_id, model, target, score), "coefficients": {model: table},
    "medians": ..., "baseline": ..., "gbm": fitted model}."""
    scope = frame[
        frame["excluded_reason"].isna() | (frame["excluded_reason"].isin(["immature", "out_of_window"]))
    ]
    scope = scope[~scope["tracker_missing"]].copy()
    train = scope[(scope["split"] == "train") & scope["mature"].astype(bool)]
    val = scope[(scope["split"] == "val") & scope["mature"].astype(bool)]
    med = fit_medians(train)
    econ, naive = TARGETS["economic"], TARGETS["naive"]
    industries = sorted({v for v in train["industry"] if isinstance(v, str)})

    base = fit_baseline(train, econ)
    lg = fit_logit(train, econ, med)
    lg_n = fit_logit(train, naive, med)
    gb = fit_gbm(train, econ, industries)
    gb_scores = gb.predict_proba(gbm_design(scope, industries))[:, 1]
    gb_val = gb.predict_proba(gbm_design(val, industries))[:, 1]
    iso = fit_isotonic(gb_val, val[econ].astype(int).to_numpy())

    scores = {
        ("baseline_rule", "economic"): predict_baseline(base, scope, float(train[econ].mean())),
        ("logit", "economic"): predict_logit(lg, scope, med),
        ("gbm", "economic"): gb_scores,
        ("gbm_isotonic", "economic"): iso.predict(gb_scores),
        ("logit_naive", "naive"): predict_logit(lg_n, scope, med),
    }
    preds = pd.concat(
        [
            pd.DataFrame({"deal_id": scope["deal_id"].to_numpy(), "model": m, "target": t, "score": s})
            for (m, t), s in scores.items()
        ],
        ignore_index=True,
    )
    return {
        "predictions": preds,
        "coefficients": {"logit": coef_table(lg), "logit_naive": coef_table(lg_n)},
        "medians": med,
        "baseline": base,
        "industries": industries,
        "n_train": len(train),
        "n_val": len(val),
    }
