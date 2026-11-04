"""T2 (time to the D19 event) and T3 (relationship R9) models, METRICS.md §5.

* **T2**: Kaplan-Meier survival (no D19 event) by origination segment, and a Cox proportional-hazards
  model (lifelines, small ridge penalty 0.01 for stability) on the logistic design (``train.LOGIT_FEATURES``)
  over every scored deal, durations in Fed ACH business days (``metrics.targets.t2_survival_rows``).
  The Cox model is descriptive (fit on all deals); the T1 models carry the temporal validation.
* **T3**: logistic regression of relationship R9 on the merchant's **first** deal's origination features,
  for relationships resolved within 18 months of first funding (``metrics.targets.t3_relationship_rows``),
  temporal split by first funding (train <= ``T3_TRAIN_END``). Survivorship caveat: relationships still
  open at 18 months are not in the population.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import statsmodels.api as sm
from lifelines import CoxPHFitter, KaplanMeierFitter

from mcarisk.model.evaluate import auc
from mcarisk.model.train import LOGIT_FEATURES, logit_design

KM_GRID = tuple(range(0, 501, 10))  # business days
COX_PENALIZER = 0.01
T3_TRAIN_END = date(2024, 6, 30)
T3_FEATURES = ("fico_per50", "log_tib_24m", "positions", "nsf_90d", "burden_per10pp", "log_term_120")


def renewal_status(df: pd.DataFrame) -> pd.Series:
    ren = df["is_renewal"].astype(int)
    st = df["prior_stress_rule_asof"].fillna(0).astype(int)
    return pd.Series(
        np.where(ren == 0, "new", np.where(st == 1, "renewal_stressed_asof", "renewal_healthy_asof")),
        index=df.index,
    )


def km_table(surv: pd.DataFrame, segments: dict[str, pd.Series]) -> pd.DataFrame:
    """Kaplan-Meier S(t) with 95% CIs on ``KM_GRID`` for every (dimension, segment)."""
    rows = []
    for dim, seg in segments.items():
        for s in sorted(seg.dropna().unique()):
            d = surv[seg == s]
            if len(d) < 5:
                continue
            km = KaplanMeierFitter().fit(d["duration_bdays"], d["event"].astype(bool))
            sf = km.survival_function_at_times(list(KM_GRID)).to_numpy()
            ci = km.confidence_interval_survival_function_
            lo = np.interp(KM_GRID, ci.index.to_numpy(), ci.iloc[:, 0].to_numpy())
            hi = np.interp(KM_GRID, ci.index.to_numpy(), ci.iloc[:, 1].to_numpy())
            for i, t in enumerate(KM_GRID):
                rows.append(
                    {
                        "dimension": dim,
                        "segment": str(s),
                        "t_bdays": t,
                        "survival": float(sf[i]),
                        "ci_low": float(lo[i]),
                        "ci_high": float(hi[i]),
                        "at_risk": int((d["duration_bdays"] >= t).sum()),
                        "n": len(d),
                        "events": int(d["event"].sum()),
                    }
                )
    return pd.DataFrame(rows)


def fit_cox(surv: pd.DataFrame, X: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    d = X.copy()
    d["duration_bdays"] = surv["duration_bdays"].to_numpy()
    d["event"] = surv["event"].astype(int).to_numpy()
    d = d[d["duration_bdays"] > 0]
    cph = CoxPHFitter(penalizer=COX_PENALIZER).fit(d, duration_col="duration_bdays", event_col="event")
    s = cph.summary.reset_index().rename(columns={"covariate": "term"})
    out = pd.DataFrame(
        {
            "term": s["term"],
            "coef": s["coef"],
            "hazard_ratio": s["exp(coef)"],
            "hr_ci_low": s["exp(coef) lower 95%"],
            "hr_ci_high": s["exp(coef) upper 95%"],
            "p_value": s["p"],
        }
    )
    return out, float(cph.concordance_index_)


def t2_models(frame: pd.DataFrame, surv_rows: list[dict], medians: dict[str, float]) -> dict:
    """KM by segment + Cox on the scored (non tracker-missing) deals."""
    sv = pd.DataFrame(surv_rows)
    df = frame[~frame["tracker_missing"]].merge(sv[["deal_id", "duration_bdays", "event"]], on="deal_id")
    pos = df["positions"].apply(lambda v: "unknown" if pd.isna(v) else ("3+" if v >= 3 else str(int(v))))
    bur = df["burden"].apply(
        lambda b: (
            "unknown"
            if pd.isna(b)
            else ("<15%" if b < 0.15 else "15-25%" if b < 0.25 else "25-35%" if b < 0.35 else ">=35%")
        )
    )
    km = km_table(df, {"renewal_status": renewal_status(df), "positions": pos, "burden": bur})
    cox, conc = fit_cox(df, logit_design(df, medians)[list(LOGIT_FEATURES)])
    return {"km": km, "cox": cox, "cox_concordance": conc, "n": len(df), "events": int(df["event"].sum())}


def t3_model(frame: pd.DataFrame, t3_rows: list[dict], medians: dict[str, float]) -> dict:
    t3 = pd.DataFrame(t3_rows)
    pop = t3[t3["in_population"]]
    first = frame.sort_values(["funding_date", "deal_id"]).groupby("merchant_key", as_index=False).first()
    d = pop.merge(first, on="merchant_key", how="inner")
    d = d[~d["tracker_missing"]]
    d["t3_split"] = np.where(d["first_funding_date"] <= T3_TRAIN_END, "train", "test")
    X = sm.add_constant(logit_design(d, medians)[list(T3_FEATURES)], has_constant="add")
    y = d["t3_bad"].astype(float)
    tr = d["t3_split"] == "train"
    out = {
        "n_relationships": len(t3),
        "n_observable": int(t3["observable"].sum()),
        "n_population": len(pop),
        "n_scored": len(d),
        "n_train": int(tr.sum()),
        "n_test": int((~tr).sum()),
        "bad_rate": float(y.mean()) if len(y) else None,
    }
    if tr.sum() >= 30 and y[tr].nunique() == 2:
        res = sm.Logit(y[tr], X[tr]).fit(disp=0, maxiter=200)
        p = np.asarray(res.predict(X), dtype=float)
        out["auc_train"] = auc(y[tr], p[tr.to_numpy()])
        out["auc_test"] = auc(y[~tr], p[(~tr).to_numpy()])
        out["coefficients"] = {k: float(v) for k, v in res.params.items()}
    return out
