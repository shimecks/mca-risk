from __future__ import annotations

from pathlib import Path

import pytest

from mcarisk import paths
from mcarisk.ingest import ingest
from mcarisk.manifest import start_run

ROOT = paths.ROOT


@pytest.fixture(scope="session")
def raw_ingest(tmp_path_factory: pytest.TempPathFactory):
    """Ingest the real raw/ once per test session into a temp DuckDB + manifest."""
    tmp = tmp_path_factory.mktemp("ingest")
    db = tmp / "portfolio.duckdb"
    manifest = tmp / "run_manifest.json"
    run = start_run(manifest, inputs_dir=paths.RAW_DIR)
    with run.stage("ingest"):
        res = ingest(paths.RAW_DIR, db, run=run)
    return {"db": db, "manifest": manifest, "result": res}


def make_xlsx(path: Path, sheets: dict[str, list[list[object]]]) -> Path:
    """Write a workbook with the given sheets (list of rows; None = empty cell)."""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r, row in enumerate(rows, start=1):
            for c, v in enumerate(row, start=1):
                if v is not None:
                    ws.cell(row=r, column=c, value=v)
    wb.save(path)
    return path


@pytest.fixture(scope="session")
def std_build(raw_ingest, tmp_path_factory: pytest.TempPathFactory):
    """A copy of the session's ingested DB + manifest, then standardized (Part 2) in place."""
    import shutil

    from mcarisk.manifest import open_run
    from mcarisk.standardize import standardize

    tmp = tmp_path_factory.mktemp("std")
    db, manifest = tmp / "portfolio.duckdb", tmp / "run_manifest.json"
    shutil.copy(raw_ingest["db"], db)
    shutil.copy(raw_ingest["manifest"], manifest)
    run = open_run(manifest)
    with run.stage("standardize"):
        res = standardize(db, dq_report_path=tmp / "dq_report.md", run=run)
    return {"db": db, "manifest": manifest, "report": tmp / "dq_report.md", "result": res}


@pytest.fixture(scope="session")
def resolve_build(std_build, tmp_path_factory: pytest.TempPathFactory):
    """A copy of the standardized build, then resolved (Part 3) in place. Notes use the committed LLM
    cache in ``auto`` mode, which never calls the network."""
    import shutil

    from mcarisk.manifest import open_run
    from mcarisk.resolve.stage import resolve

    tmp = tmp_path_factory.mktemp("resolve")
    db, manifest = tmp / "portfolio.duckdb", tmp / "run_manifest.json"
    shutil.copy(std_build["db"], db)
    shutil.copy(std_build["manifest"], manifest)
    run = open_run(manifest)
    with run.stage("resolve"):
        res = resolve(db, dq_report_path=tmp / "dq_report.md", run=run, notes_mode="auto")
    return {"db": db, "manifest": manifest, "report": tmp / "dq_report.md", "result": res}
