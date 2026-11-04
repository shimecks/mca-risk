"""E8 Model (plan.md §4): temporal-test AUC, Brier, calibration slope, decile lift, coefficient signs vs
``ground_truth/dgp.json``, leakage check, and the true-hazard ceiling on the same test deals.

Gates (``evals/thresholds.json``) apply to the **primary model** fixed in the pipeline before the test split
was scored (``mcarisk.model.train.PRIMARY_MODEL`` = ``logit``):

* ``temporal_test_auc`` >= 0.70: AUC of the economic T1 target on mature test deals (funded
  2025-07..2026-03), read from ``model_performance``.
* ``calibration_slope`` in [0.8, 1.2]: logistic recalibration slope on the same deals.
* ``main_driver_signs_correct``: every main driver in ``dgp.json`` ``coef`` that maps to a logistic term has
  the DGP's sign. Mapping (term: DGP coefficient, orientation): ``fico_per50``: ``fico_per_50_below_650`` and
  ``fico_below_600``, inverted (they rise as FICO falls); ``log_tib_24m``: ``log_tib_over_24m``;
  ``positions``: ``positions_at_funding``; ``nsf_90d``: ``nsf_90d``; ``burden_per10pp``:
  ``burden_minus_0_15_x4``; ``renewal_stressed_asof``: ``is_distressed_refi``; ``renewal_healthy_asof``:
  ``is_healthy_renewal``. Latent quality, industry and the macro vintage term have no logistic counterpart.

Report-only: Brier, decile lift, **ceiling AUC** (the simulator's true log-hazard ``hazard_lin`` from
``ground_truth/deals.csv`` ranked against the pipeline's T1 labels on the same test deals), the same
ceiling without the unobservable latent quality term (``hazard_lin + 0.65 z``: the best any origination
model could do with the true coefficients), every model's val / test numbers, the naive-vs-economic
comparison, a diagnostic validation-fitted recalibration of the primary model (not a pipeline model), and
``leakage_tests_passed`` (perturbation check on a copy of the build database).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from evals.common import EvalContext, SuiteOutput
from mcarisk.model.evaluate import auc, calibration_slope, logit
from mcarisk.model.features import build_features
from mcarisk.model.leakage import feature_diff, perturb_post_funding
from mcarisk.model.train import PRIMARY_MODEL

SUITE_ID = "E8"

# logistic term -> (DGP coefficient names, orientation: +1 same sign, -1 inverted)
SIGN_MAP = {
    "fico_per50": (("fico_per_50_below_650", "fico_below_600"), -1),
    "log_tib_24m": (("log_tib_over_24m",), 1),
    "positions": (("positions_at_funding",), 1),
    "nsf_90d": (("nsf_90d",), 1),
    "burden_per10pp": (("burden_minus_0_15_x4",), 1),
    "renewal_stressed_asof": (("is_distressed_refi",), 1),
    "renewal_healthy_asof": (("is_healthy_renewal",), 1),
}


def sign_check(coefs: pd.DataFrame, dgp: dict) -> list[dict]:
    rows = []
    c = coefs.set_index("term")
    for term, (names, orient) in SIGN_MAP.items():
        vals = [float(dgp["coef"][n]) for n in names if isinstance(dgp["coef"].get(n), (int, float))]
        if not vals or term not in c.index:
            continue
        expected = int(np.sign(orient * np.sign(vals[0])))
        if any(int(np.sign(orient * np.sign(v))) != expected for v in vals):
            raise ValueError(f"DGP coefficients {names} disagree in sign")
        got = float(c.loc[term, "coef"])
        rows.append(
            {
                "term": term,
                "dgp": {n: dgp["coef"][n] for n in names},
                "expected_sign": expected,
                "coef": got,
                "ci": [float(c.loc[term, "ci_low"]), float(c.loc[term, "ci_high"])],
                "p_value": float(c.loc[term, "p_value"]),
                "sign_ok": int(np.sign(got)) == expected,
            }
        )
    return rows


def leakage_check(db_path: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "perturbed.duckdb"
        shutil.copy(db_path, tmp)
        con = duckdb.connect(str(tmp))
        try:
            before = build_features(con)
            info = perturb_post_funding(con)
            after = build_features(con)
        finally:
            con.close()
    diff = feature_diff(before, after)
    return {
        "passed": not diff,
        "n_diffs": len(diff),
        "first_diffs": [list(map(str, d)) for d in diff[:5]],
        **info,
    }


def run(ctx: EvalContext) -> SuiteOutput:
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        perf = con.execute("SELECT * FROM model_performance").df()
        coefs = con.execute("SELECT * FROM model_coefficients WHERE model = ?", [PRIMARY_MODEL]).df()
        preds = con.execute(
            "SELECT deal_id, model, split, score, t1_bad_economic FROM model_predictions WHERE excluded_reason IS NULL"
        ).df()
        lift = con.execute(
            "SELECT model, decile, n, n_bad, bad_rate, lift, cum_capture FROM model_lift WHERE split = 'test' "
            "ORDER BY model, decile"
        ).df()
        naive = con.execute("SELECT model, metric, value FROM model_naive_comparison").df()
        t3 = {r[0]: json.loads(r[1]) for r in con.execute("SELECT key, value FROM t3_summary").fetchall()}
        conc = con.execute("SELECT MAX(concordance) FROM survival_cox").fetchone()[0]
    finally:
        con.close()

    def m(model: str, split: str, metric: str, et: str = "economic"):
        s = perf[
            (perf.model == model) & (perf.split == split) & (perf.metric == metric) & (perf.eval_target == et)
        ]
        return None if s.empty else float(s["value"].iloc[0])

    dgp = json.loads((ctx.ground_truth_dir / "dgp.json").read_text(encoding="utf-8"))
    signs = sign_check(coefs, dgp)

    truth = pd.read_csv(ctx.ground_truth_dir / "deals.csv", usecols=["deal_id", "merchant_id", "hazard_lin"])
    z = pd.read_csv(ctx.ground_truth_dir / "merchants.csv", usecols=["merchant_id", "z"])
    truth = truth.merge(z, on="merchant_id")
    zc = float(dgp["coef"]["latent_quality_z"])
    truth["hazard_lin_observable"] = truth["hazard_lin"] - zc * truth["z"]
    ceil = {}
    for split in ("val", "test"):
        d = preds[(preds.model == PRIMARY_MODEL) & (preds.split == split)].merge(truth, on="deal_id")
        y = d["t1_bad_economic"].astype(int)
        ceil[split] = {
            "n": len(d),
            "true_hazard_auc": auc(y, d["hazard_lin"]),
            "observable_true_coef_auc": auc(y, d["hazard_lin_observable"]),
            "primary_model_auc": auc(y, d["score"]),
        }

    # diagnostic only: recalibrate the primary model on validation, apply to test
    pv = preds[(preds.model == PRIMARY_MODEL) & (preds.split == "val")]
    pt = preds[(preds.model == PRIMARY_MODEL) & (preds.split == "test")]
    b, a = calibration_slope(pv["t1_bad_economic"].astype(int), pv["score"])
    p_rec = 1 / (1 + np.exp(-(a + b * logit(pt["score"]))))
    rec_slope, rec_int = calibration_slope(pt["t1_bad_economic"].astype(int), p_rec)

    leak = leakage_check(ctx.db_path)
    models = sorted(perf["model"].unique())
    metrics = {
        "temporal_test_auc": m(PRIMARY_MODEL, "test", "auc"),
        "calibration_slope": m(PRIMARY_MODEL, "test", "calibration_slope"),
        "main_driver_signs_correct": bool(signs) and all(s["sign_ok"] for s in signs),
        "brier": m(PRIMARY_MODEL, "test", "brier"),
        "decile_lift": {
            "top_decile_lift": m(PRIMARY_MODEL, "test", "top_decile_lift"),
            "table": lift[lift.model == PRIMARY_MODEL].drop(columns="model").to_dict("records"),
        },
        "ceiling_auc": ceil["test"]["true_hazard_auc"],
        "leakage_tests_passed": leak["passed"],
    }
    details = {
        "primary_model": PRIMARY_MODEL,
        "models": {
            mod: {
                split: {
                    k: m(mod, split, k)
                    for k in (
                        "n",
                        "bad_rate",
                        "mean_pred",
                        "auc",
                        "brier",
                        "calibration_slope",
                        "calibration_intercept",
                        "top_decile_lift",
                    )
                }
                for split in ("train", "val", "test")
            }
            for mod in models
        },
        "ceiling": ceil,
        "signs": signs,
        "naive_vs_economic_test": {f"{r.model}.{r.metric}": r.value for r in naive.itertuples()},
        "diagnostic_val_recalibrated_primary": {
            "val_slope": b,
            "val_intercept": a,
            "test_slope": rec_slope,
            "test_intercept": rec_int,
        },
        "leakage": leak,
        "t2_cox_concordance": conc,
        "t3": t3,
    }
    tl = details["models"][PRIMARY_MODEL]
    notes = [
        f"primary `{PRIMARY_MODEL}` test: AUC {metrics['temporal_test_auc']:.3f}, Brier {metrics['brier']:.3f}, "
        f"slope {metrics['calibration_slope']:.2f}, top-decile lift {metrics['decile_lift']['top_decile_lift']:.2f} "
        f"(n={tl['test']['n']:.0f}); val AUC {tl['val']['auc']:.3f}, val slope {tl['val']['calibration_slope']:.2f}",
        f"ceiling on the same test deals: true hazard AUC {ceil['test']['true_hazard_auc']:.3f}; observable terms "
        f"with true coefficients {ceil['test']['observable_true_coef_auc']:.3f} (val: {ceil['val']['true_hazard_auc']:.3f} / "
        f"{ceil['val']['observable_true_coef_auc']:.3f})",
        "test AUC by model: "
        + ", ".join(f"{k} {v['test']['auc']:.3f}" for k, v in details["models"].items()),
        f"signs: {sum(s['sign_ok'] for s in signs)}/{len(signs)} main drivers match dgp.json",
        f"diagnostic: validation-fitted recalibration of `{PRIMARY_MODEL}` gives test slope {rec_slope:.2f}",
        f"leakage perturbation: {leak['post_funding_ledger_rows']} post-funding ledger rows scrambled, "
        f"{leak['fake_events']} fake events added -> {leak['n_diffs']} feature differences",
    ]
    return SuiteOutput(metrics=metrics, details=details, notes=notes)
