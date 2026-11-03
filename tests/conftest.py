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
