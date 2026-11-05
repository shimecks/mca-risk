"""``make app-data``: refresh the app's committed data snapshot in ``app/data/`` from ``build/``.

The Streamlit app (``app/``) reads only this snapshot, so it runs on Streamlit Community Cloud without
running the pipeline, without network access and without any API key. The snapshot holds:

  app/data/portfolio.duckdb      the tables the app reads (APP_TABLES; a column subset where noted),
                                 copied row for row from build/portfolio.duckdb, so every number the app
                                 shows is a metric-table value
  app/data/figures/*.plotly.json the figures (interactive JSON only; the PNGs stay in build/)
  app/data/*.md, *.json          findings, findings registry, model card, DQ report, run manifest,
                                 the latest eval report (``evals/reports/latest.json``)
  app/data/app_data.json         what was exported: source run id, git SHA, tables and row counts,
                                 files, and the metric IDs each figure cites

Nothing here computes a metric; it copies.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from mcarisk import paths

APP_DATA_DIR = paths.ROOT / "app" / "data"
EVAL_LATEST = paths.ROOT / "evals" / "reports" / "latest.json"
BLOCK_SIZE = 16384

# table -> columns to keep (None = all). Large source tables keep only what the app displays.
APP_TABLES: dict[str, list[str] | None] = {
    "portfolio_metrics": None,
    "target_metrics": None,
    "relationship_metrics": None,
    "deal_metrics": None,
    "merchants": [
        "merchant_key",
        "canonical_name",
        "n_deals",
        "deal_ids",
        "first_funding_date",
        "last_funding_date",
    ],
    "deals_resolved": [
        "deal_id",
        "merchant_key",
        "source_family",
        "merchant_name_raw",
        "funding_date",
        "advance_cents",
        "factor",
        "rtr_cents",
        "payment_cents",
        "frequency",
        "term_bdays",
        "fee_cents",
        "commission_rate",
        "iso_name",
        "status_raw",
        "status_norm",
        "notes_raw",
        "tracker_missing",
        "prior_deal_id",
        "link_method",
        "link_confidence",
        "next_deal_id",
        "src_file",
        "src_sheet",
        "src_row",
    ],
    "deal_links": None,
    "ledger": [
        "event_id",
        "deal_id",
        "merchant_key",
        "event_date",
        "kind",
        "amount_cents",
        "is_cash",
        "source",
        "successor_deal_id",
        "new_payment_cents",
        "return_code",
        "deal_id_posted",
        "src_file",
        "src_sheet",
        "src_row",
    ],
    "deal_status_events": None,
    "payoff_classes": [
        "payment_id",
        "deal_id",
        "txn_date",
        "amount_cents",
        "description",
        "payoff_class",
        "successor_deal_id",
        "method",
        "confidence",
        "src_file",
        "src_sheet",
        "src_row",
    ],
    "misposting_repairs": None,
    "data_quality": None,
    "exceptions": None,
    "std_accounting": None,
    "ledger_accounting": None,
    "raw_sources": [
        "src_file",
        "src_sheet",
        "table_name",
        "status",
        "status_reason",
        "header_row",
        "n_physical_rows",
        "n_loaded",
        "n_rejected",
        "file_sha256",
    ],
    "raw_rejects": None,
    "ledger_rejects": None,
    "model_performance": None,
    "model_coefficients": None,
    "model_calibration": None,
    "model_lift": None,
    "model_naive_comparison": None,
    "model_fairness": None,
    "survival_km": None,
    "survival_cox": None,
    "t3_summary": None,
}

# build/<name> -> app/data/<name>
APP_FILES = ("findings.md", "findings_numbers.json", "model_card.md", "dq_report.md", "run_manifest.json")

REQUIRED_FIGURES = (
    "masking",
    "bad_rate_vintage",
    "exposure",
    "vintage_curves",
    "refi_quality",
    "segments",
    "model_calibration",
    "model_lift",
    "model_naive_vs_economic",
    "model_coefficients",
    "survival_km",
)


def figure_metric_ids() -> dict[str, list[str]]:
    from mcarisk.analytics.figures import FIGURE_METRICS
    from mcarisk.model.figures import MODEL_FIGURE_METRICS

    return {**FIGURE_METRICS, **MODEL_FIGURE_METRICS}


def export_app_data(
    build_dir: Path = paths.BUILD_DIR,
    out_dir: Path = APP_DATA_DIR,
    *,
    eval_latest: Path | None = EVAL_LATEST,
) -> dict:
    build_dir, out_dir = Path(build_dir), Path(out_dir)
    src_db = build_dir / "portfolio.duckdb"
    if not src_db.exists():
        raise FileNotFoundError(f"{src_db} not found; run `make all` first")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "figures").mkdir(exist_ok=True)

    tmp_db = out_dir / "portfolio.duckdb.tmp"
    tmp_db.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    # 16 KiB blocks instead of the 256 KiB default: most app tables are small, so this keeps the
    # committed file at a few MB.
    con = duckdb.connect(str(tmp_db), config={"default_block_size": str(BLOCK_SIZE)})
    try:
        con.execute(f"ATTACH '{src_db.as_posix()}' AS src (READ_ONLY)")
        for t, cols in APP_TABLES.items():
            sel = "*" if cols is None else ", ".join(f'"{c}"' for c in cols)
            order = (
                " ORDER BY ALL" if t != "ledger" else " ORDER BY merchant_key, deal_id, event_date, event_id"
            )
            con.execute(f'CREATE TABLE main."{t}" AS SELECT {sel} FROM src."{t}"{order}')
            counts[t] = con.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
        con.execute("DETACH src")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    db = out_dir / "portfolio.duckdb"
    tmp_db.replace(db)

    figs = {}
    for name in REQUIRED_FIGURES:
        src = build_dir / "figures" / f"{name}.plotly.json"
        if not src.exists():
            raise FileNotFoundError(f"{src} not found; run `make all` first")
        shutil.copyfile(src, out_dir / "figures" / src.name)
        figs[name] = f"figures/{src.name}"
    files = []
    for f in APP_FILES:
        shutil.copyfile(build_dir / f, out_dir / f)
        files.append(f)
    if eval_latest is not None and Path(eval_latest).exists():
        shutil.copyfile(eval_latest, out_dir / "eval_latest.json")
        files.append("eval_latest.json")

    manifest = json.loads((build_dir / "run_manifest.json").read_text(encoding="utf-8"))
    meta = {
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_run_id": manifest.get("run_id"),
        "source_git": manifest.get("git"),
        "metrics_md": manifest.get("metrics_md", {}).get("version"),
        "db_bytes": db.stat().st_size,
        "tables": counts,
        "files": files,
        "figures": figs,
        "figure_metric_ids": figure_metric_ids(),
    }
    (out_dir / "app_data.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refresh app/data/ (the app's data snapshot) from build/.")
    ap.add_argument("--build", type=Path, default=paths.BUILD_DIR)
    ap.add_argument("--out", type=Path, default=APP_DATA_DIR)
    args = ap.parse_args(argv)
    meta = export_app_data(args.build, args.out)
    print(
        f"[app-data] {len(meta['tables'])} tables, {len(meta['figures'])} figures, "
        f"{meta['db_bytes'] / 1e6:.1f} MB DB -> {args.out} (run {meta['source_run_id']})"
    )
    if "eval_latest.json" not in meta["files"]:
        print(
            "[app-data] no evals/reports/latest.json: run `make eval` then `make app-data`", file=sys.stderr
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
