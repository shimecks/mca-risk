"""Model performance measures: AUC, Brier, calibration slope / intercept, reliability bins, decile lift.

* **Calibration slope / intercept** (logistic recalibration): fit ``logit P(y=1) = a + b * logit(p)`` by
  maximum likelihood on the evaluation set. ``b`` = slope (1 = well spread; < 1 = predictions too
  extreme; > 1 = too timid), ``a`` = intercept. Predictions are clipped to ``[EPS, 1 - EPS]`` first.
  Calibration-in-the-large = mean(p) - mean(y).
* **Decile lift**: deals ranked by score (ties broken by deal_id, so the table is deterministic) and cut
  into 10 equal-count groups; decile 1 = highest risk. lift = bad rate in the decile / overall bad rate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

EPS = 1e-4


def logit(p) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def calibration_slope(y, p, *, max_iter: int = 100, tol: float = 1e-10) -> tuple[float, float]:
    """(slope b, intercept a) of the logistic recalibration ``logit P(y) = a + b logit(p)`` (Newton-Raphson).

    Returns (nan, nan) when the MLE does not exist (perfect separation, or a single outcome class)."""
    y = np.asarray(y, dtype=float)
    x = logit(p)
    if len(np.unique(y)) < 2:
        return float("nan"), float("nan")
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(max_iter):
        eta = np.clip(X @ beta, -700, 700)
        mu = 1 / (1 + np.exp(-eta))
        w = mu * (1 - mu)
        grad = X.T @ (y - mu)
        hess = X.T @ (X * w[:, None])
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            return float("nan"), float("nan")
        beta = beta + step
        if np.max(np.abs(step)) < tol:
            return float(beta[1]), float(beta[0])
    return float("nan"), float("nan")  # no convergence (separation)


def auc(y, p) -> float | None:
    y = np.asarray(y)
    if len(set(y.tolist())) < 2:
        return None
    return float(roc_auc_score(y, p))


def performance(y, p) -> dict[str, float | None]:
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    slope, intercept = calibration_slope(y, p)
    lift = decile_table(pd.DataFrame({"deal_id": np.arange(len(y)), "y": y, "score": p}))
    return {
        "n": float(len(y)),
        "n_bad": float(y.sum()),
        "bad_rate": float(y.mean()),
        "mean_pred": float(p.mean()),
        "auc": auc(y, p),
        "brier": float(brier_score_loss(y, p)),
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "calibration_in_the_large": float(p.mean() - y.mean()),
        "top_decile_lift": float(lift.loc[lift.decile == 1, "lift"].iloc[0]),
        "top_decile_bad_rate": float(lift.loc[lift.decile == 1, "bad_rate"].iloc[0]),
    }


def decile_table(df: pd.DataFrame, n_groups: int = 10) -> pd.DataFrame:
    """``df`` has deal_id, y, score. One row per decile (1 = highest scores)."""
    d = df.sort_values(["score", "deal_id"], ascending=[False, True]).reset_index(drop=True)
    d["decile"] = (np.arange(len(d)) * n_groups // max(len(d), 1)) + 1
    overall = d["y"].mean()
    g = (
        d.groupby("decile")
        .agg(n=("y", "size"), n_bad=("y", "sum"), mean_score=("score", "mean"))
        .reset_index()
    )
    g["bad_rate"] = g["n_bad"] / g["n"]
    g["lift"] = g["bad_rate"] / overall if overall else np.nan
    g["cum_capture"] = g["n_bad"].cumsum() / max(d["y"].sum(), 1)
    return g


def reliability_bins(y, p, n_bins: int = 10) -> pd.DataFrame:
    """Equal-count bins of predicted probability: mean prediction vs observed rate (calibration plot)."""
    d = pd.DataFrame({"y": np.asarray(y, dtype=float), "p": np.asarray(p, dtype=float)})
    d = d.sort_values("p", kind="mergesort").reset_index(drop=True)
    d["bin"] = np.arange(len(d)) * n_bins // max(len(d), 1) + 1
    g = d.groupby("bin").agg(n=("y", "size"), mean_pred=("p", "mean"), obs_rate=("y", "mean")).reset_index()
    return g
