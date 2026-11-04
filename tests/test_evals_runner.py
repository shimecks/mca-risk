from __future__ import annotations

import json
import shutil

import duckdb

from evals import run as runner
from evals.common import EvalContext
from evals.suites import discover, e1_ingestion, e2_standardization, e3_dedupe

ALL = [f"E{i}" for i in range(1, 10)]


def test_thresholds_cover_all_suites_with_plan_gates():
    th = runner.load_thresholds()
    assert list(th) == ALL
    assert th["E1"]["gates"]["rows_accounted_rate"] == {"op": ">=", "value": 1.0}
    assert th["E2"]["gates"]["field_accuracy"]["value"] == 0.995
    assert th["E3"]["gates"]["payments_count_abs_diff"] == {"op": "==", "value": 0}
    assert th["E4"]["gates"]["pairwise_f1"]["value"] == 0.98
    e5 = th["E5"]["gates"]
    got5 = tuple(
        e5[k]["value"]
        for k in (
            "renewal_link_precision",
            "renewal_link_recall",
            "l1_payoff_accuracy",
            "l3_misposting_recall",
        )
    )
    assert got5 == (0.98, 0.97, 0.99, 0.95)
    e6 = th["E6"]["gates"]
    assert tuple(e6[k]["value"] for k in ("outcome_accuracy", "d17_precision", "d17_recall")) == (
        0.97,
        0.95,
        0.85,
    )
    assert th["E7"]["gates"]["deal_metric_max_abs_err_usd"]["value"] == 1.0
    assert th["E7"]["gates"]["portfolio_metric_max_abs_err_pp"]["value"] == 0.5
    assert th["E8"]["gates"]["calibration_slope"]["value"] == [0.8, 1.2]
    assert th["E8"]["gates"]["temporal_test_auc"]["value"] == 0.70
    assert th["E9"]["optional"] is True


def test_gate_ops():
    assert runner.check_gate(1.0, {"op": ">=", "value": 1.0})
    assert not runner.check_gate(0.999, {"op": ">=", "value": 1.0})
    assert runner.check_gate(0.9, {"op": "between", "value": [0.8, 1.2]})
    assert not runner.check_gate(1.3, {"op": "between", "value": [0.8, 1.2]})
    assert runner.check_gate(True, {"op": "==", "value": True})
    assert not runner.check_gate(None, {"op": "<=", "value": 1})
    assert not runner.check_gate(float("nan"), {"op": "<=", "value": 1})


def test_unimplemented_suite_reported_not_failed():
    res = runner.run_suite("E8", runner.load_thresholds()["E8"], {}, EvalContext())
    assert res["status"] == "not_implemented"
    assert "E1" in discover()


def test_missing_metric_fails_gate():
    class Fake:
        @staticmethod
        def run(ctx):
            from evals.common import SuiteOutput

            return SuiteOutput(metrics={"rows_accounted_rate": 1.0})  # files_reconciled_rate missing

    import evals.suites as s

    orig = s.load
    s.load = lambda name: Fake
    try:
        res = runner.run_suite("E1", runner.load_thresholds()["E1"], {"E1": "x"}, EvalContext())
    finally:
        s.load = orig
    assert res["status"] == "fail"


def test_e1_passes_on_clean_ingest(raw_ingest):
    out = e1_ingestion.run(EvalContext(db_path=raw_ingest["db"]))
    assert out.metrics["rows_accounted_rate"] == 1.0
    assert out.metrics["files_reconciled_rate"] == 1.0
    assert out.metrics["value_fidelity_rate"] == 1.0
    assert out.metrics["lineage_nonnull_rate"] == 1.0


def test_e1_detects_a_dropped_row(raw_ingest, tmp_path):
    db = tmp_path / "tampered.duckdb"
    shutil.copy(raw_ingest["db"], db)
    con = duckdb.connect(str(db))
    con.execute("DELETE FROM raw_ach WHERE src_file = 'raw/ach_exports/ach_2023_03.csv' AND src_row = 10")
    con.close()
    out = e1_ingestion.run(EvalContext(db_path=db))
    assert out.metrics["rows_accounted_rate"] < 1.0
    assert out.metrics["files_reconciled_rate"] < 1.0


def test_e1_detects_a_changed_value(raw_ingest, tmp_path):
    db = tmp_path / "tampered.duckdb"
    shutil.copy(raw_ingest["db"], db)
    con = duckdb.connect(str(db))
    con.execute("UPDATE raw_tracker_2024 SET \"Purchase Price\" = '1' WHERE src_row = 2")
    con.close()
    out = e1_ingestion.run(EvalContext(db_path=db))
    assert out.metrics["value_fidelity_rate"] < 1.0


def test_runner_writes_reports_and_exit_codes(std_build, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EvalContext", lambda: EvalContext(db_path=std_build["db"]))
    assert runner.main(["--suite", "E1", "--reports-dir", str(tmp_path)]) == 0
    rep = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert rep["overall"] == "pass" and rep["suites"][0]["status"] == "pass"
    assert (tmp_path / "latest.md").exists()
    assert runner.main(["--suite", "E5", "--reports-dir", str(tmp_path)]) == 2  # requested but not built yet
    assert runner.main(["--reports-dir", str(tmp_path)]) == 0  # all: unimplemented suites don't fail


def test_e2_e3_pass_on_standardized_build(std_build):
    e2 = e2_standardization.run(EvalContext(db_path=std_build["db"]))
    assert e2.metrics["field_accuracy"] >= 0.995 and e2.metrics["unknown_deal_ids"] == 0
    e3 = e3_dedupe.run(EvalContext(db_path=std_build["db"]))
    assert e3.metrics["payments_count_abs_diff"] == 0 and e3.metrics["payments_sum_abs_diff_cents"] == 0
    assert e3.metrics["multiset_unmatched_ignoring_deal"] == 0


def test_e2_detects_a_wrong_value(std_build, tmp_path):
    db = tmp_path / "tampered.duckdb"
    shutil.copy(std_build["db"], db)
    con = duckdb.connect(str(db))
    con.execute(
        "UPDATE deals_std SET payment_cents = payment_cents + 1, term_bdays = NULL WHERE deal_id = 1001"
    )
    con.close()
    out = e2_standardization.run(EvalContext(db_path=db))
    assert out.metrics["mismatch_count"] == 2
    assert {(m["field"], m["deal_id"]) for m in out.details["mismatches"]} == {
        ("payment", 1001),
        ("term", 1001),
    }


def test_e3_detects_a_surviving_duplicate(std_build, tmp_path):
    db = tmp_path / "tampered.duckdb"
    shutil.copy(std_build["db"], db)
    con = duckdb.connect(str(db))
    con.execute(
        "INSERT INTO payments_std SELECT payment_id + 10000000, txn_date, deal_id_posted, merchant_name_raw, "
        "amount_cents, txn_type, return_code, batch_id || 'X', description, date_format_raw, 1, src_file, "
        "src_sheet, src_row FROM payments_std WHERE payment_id = 1"
    )
    con.close()
    out = e3_dedupe.run(EvalContext(db_path=db))
    assert out.metrics["payments_count_abs_diff"] == 1 and out.metrics["payments_sum_abs_diff_cents"] > 0
