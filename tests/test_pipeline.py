"""Part 7: the whole pipeline (``python -m mcarisk.pipeline``, what ``make all`` runs) completes every stage
from raw/ within the 2-minute budget, writes build/report.md, and its output exports to the app snapshot."""

from __future__ import annotations

import duckdb
import pytest

from mcarisk import paths
from mcarisk.appdata import APP_TABLES, REQUIRED_FIGURES, export_app_data
from mcarisk.pipeline import STAGES, TIME_BUDGET_SECONDS, run_all


@pytest.fixture(scope="module")
def full_build(tmp_path_factory):
    out = tmp_path_factory.mktemp("full_build")
    res = run_all(out, raw_dir=paths.RAW_DIR, eval_latest=out / "no_eval_report.json", log=lambda *_: None)
    return out, res


def test_all_stages_complete_within_budget(full_build):
    import json

    out, res = full_build
    assert list(res.seconds) == list(STAGES)
    assert res.total_seconds < TIME_BUDGET_SECONDS, res.seconds
    man = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert [s for s in man["stages"]] == list(STAGES)
    assert all(s["status"] == "ok" for s in man["stages"].values())
    for f in (
        "portfolio.duckdb",
        "dq_report.md",
        "findings.md",
        "findings_numbers.json",
        "model_card.md",
        "report.md",
    ):
        assert (out / f).exists(), f


def test_report_links_documents_and_quotes_registry(full_build):
    out, _ = full_build
    rep = (out / "report.md").read_text(encoding="utf-8")
    for s in (
        "## Documents",
        "## Headline (from the findings registry)",
        "## Pipeline stages",
        "## Data quality",
        "## Evals",
    ):
        assert s in rep
    for link in ("(findings.md)", "(model_card.md)", "(dq_report.md)", "(run_manifest.json)"):
        assert link in rep
    assert "No eval report yet" in rep  # this build has no eval report next to it
    import json

    nums = {
        n["key"]: n["text"] for n in json.loads((out / "findings_numbers.json").read_text(encoding="utf-8"))
    }
    assert f"**{nums['p1_rep']} vs {nums['p1_adj']}**" in rep and f"**{nums['p2_gap']}**" in rep


def test_app_data_export_copies_tables_row_for_row(full_build, tmp_path):
    out, _ = full_build
    meta = export_app_data(out, tmp_path / "data", eval_latest=None)
    assert set(meta["figures"]) == set(REQUIRED_FIGURES)
    src = duckdb.connect(str(out / "portfolio.duckdb"), read_only=True)
    dst = duckdb.connect(str(tmp_path / "data" / "portfolio.duckdb"), read_only=True)
    try:
        for t, cols in APP_TABLES.items():
            sel = "*" if cols is None else ", ".join(f'"{c}"' for c in cols)
            a = src.execute(f'SELECT {sel} FROM "{t}" ORDER BY ALL').fetchall()
            b = dst.execute(f'SELECT * FROM "{t}" ORDER BY ALL').fetchall()
            assert a == b, t
    finally:
        src.close()
        dst.close()
