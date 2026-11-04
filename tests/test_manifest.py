from __future__ import annotations

import json

import pytest

from mcarisk import paths
from mcarisk.manifest import REQUIRED_KEYS, metrics_md_info, open_run, start_run


def test_ingest_manifest_has_required_keys(raw_ingest):
    m = json.loads(raw_ingest["manifest"].read_text(encoding="utf-8"))
    for k in REQUIRED_KEYS:
        assert k in m, k
    assert m["metrics_md"]["version"] == "v1.3"
    assert len(m["metrics_md"]["sha256"]) == 64
    assert "sha" in m["git"] and "dirty" in m["git"]
    assert len([k for k in m["inputs"] if k.startswith("raw/ach_exports/")]) == 46
    assert all(len(v["sha256"]) == 64 and v["bytes"] > 0 for v in m["inputs"].values())
    st = m["stages"]["ingest"]
    assert st["status"] == "ok" and st["seconds"] > 0
    assert st["tables"]["raw_tracker_2023"]["rejected"] == 2
    assert st["tables"]["raw_tracker_2023"]["reasons"]["rejected"] == {
        "blank_row": 1,
        "preamble_above_header": 1,
    }
    assert st["tables"]["raw_ach"]["accepted"] > 100_000
    assert set(m["llm"]) >= {"calls", "input_tokens", "output_tokens", "cost_usd"}


def test_later_stage_appends_and_llm_usage(tmp_path):
    path = tmp_path / "m.json"
    start_run(path, inputs_dir=None)
    run = open_run(path)
    with run.stage("standardize") as st:
        st.table(
            "deals_std",
            accepted=10,
            rejected=2,
            repaired=1,
            reasons={"rejected": {"bad_date": 2}, "repaired": {"money_text": 1}},
        )
    run.add_llm_usage(model="m", input_tokens=100, output_tokens=20, cost_usd=0.001)
    run.write()
    m = json.loads(path.read_text(encoding="utf-8"))
    assert m["stages"]["standardize"]["tables"]["deals_std"]["repaired"] == 1
    assert m["llm"]["input_tokens"] == 100 and m["llm"]["models"]["m"]["calls"] == 1
    assert m["totals"]["seconds"] >= 0


def test_rejected_rows_need_reasons(tmp_path):
    run = start_run(tmp_path / "m.json", inputs_dir=None)
    with pytest.raises(ValueError), run.stage("x") as st:
        st.table("t", accepted=1, rejected=3, reasons={"rejected": {"a": 1}})
    assert json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))["stages"]["x"]["status"] == "failed"


def test_metrics_md_version():
    assert metrics_md_info(paths.METRICS_MD)["version"] == "v1.3"
