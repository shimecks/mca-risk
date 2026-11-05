"""The whole pipeline in one process: ``python -m mcarisk.pipeline`` (what ``make all`` runs).

Stages, in order (plan.md §3): ingest, standardize, resolve, ledger, metrics, analytics, model, report.
Every stage writes into one build directory (default ``build/``) and appends to the same run manifest.
The per-stage ``make`` targets (``make ingest`` ...) run the same functions one at a time.

No network: the collections-notes extractor runs in its default mode, which reads the committed LLM
cache and never calls the API (``make notes-llm`` is the only target that may).
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcarisk import paths
from mcarisk.manifest import start_run

STAGES = ("ingest", "standardize", "resolve", "ledger", "metrics", "analytics", "model", "report")
TIME_BUDGET_SECONDS = 120.0  # plan.md §5 Part 7: `make all` from raw/ in under 2 minutes


@dataclass
class PipelineResult:
    build_dir: Path
    seconds: dict[str, float] = field(default_factory=dict)
    total_seconds: float = 0.0


def run_all(
    build_dir: Path = paths.BUILD_DIR,
    *,
    raw_dir: Path = paths.RAW_DIR,
    mappings_dir: Path = paths.MAPPINGS_DIR,
    eval_latest: Path | None = None,
    log=print,
) -> PipelineResult:
    """Run every stage from ``raw_dir`` into ``build_dir``. Returns wall-clock seconds per stage."""
    from mcarisk.analytics.stage import run_analytics
    from mcarisk.ingest import ingest
    from mcarisk.ledger import run_ledger
    from mcarisk.metrics.stage import run_metrics
    from mcarisk.model.stage import run_model
    from mcarisk.report import EVAL_LATEST, build_report
    from mcarisk.resolve.stage import resolve
    from mcarisk.standardize import standardize

    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    db = build_dir / "portfolio.duckdb"
    if db.exists():
        db.unlink()  # a full run always starts from an empty database
    dq = build_dir / "dq_report.md"
    figs = build_dir / "figures"
    run = start_run(build_dir / "run_manifest.json", inputs_dir=raw_dir)
    res = PipelineResult(build_dir)
    t_all = time.perf_counter()

    steps = {
        "ingest": lambda: ingest(raw_dir, db, run=run),
        "standardize": lambda: standardize(db, mappings_dir=mappings_dir, dq_report_path=dq, run=run),
        "resolve": lambda: resolve(db, dq_report_path=dq, run=run),
        "ledger": lambda: run_ledger(db, run=run),
        "metrics": lambda: run_metrics(db, dq_report_path=dq, run=run),
        "analytics": lambda: run_analytics(
            db,
            fig_dir=figs,
            findings_md=build_dir / "findings.md",
            findings_json=build_dir / "findings_numbers.json",
            run=run,
        ),
        "model": lambda: run_model(db, fig_dir=figs, model_card=build_dir / "model_card.md", run=run),
        "report": lambda: build_report(build_dir, eval_latest=eval_latest or EVAL_LATEST, run=run),
    }
    for name in STAGES:
        t0 = time.perf_counter()
        with run.stage(name):
            steps[name]()
        res.seconds[name] = round(time.perf_counter() - t0, 3)
        log(f"[{name}] ok in {res.seconds[name]:.2f}s")
    run.write()
    res.total_seconds = round(time.perf_counter() - t_all, 3)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the full pipeline from raw/ into build/.")
    ap.add_argument("--build", type=Path, default=paths.BUILD_DIR)
    ap.add_argument("--raw", type=Path, default=paths.RAW_DIR)
    args = ap.parse_args(argv)
    res = run_all(args.build, raw_dir=args.raw)
    flag = (
        "" if res.total_seconds < TIME_BUDGET_SECONDS else f" -- OVER the {TIME_BUDGET_SECONDS:.0f}s budget"
    )
    print(
        f"[all] {len(STAGES)} stages in {res.total_seconds:.1f}s{flag} -> {args.build}/ (see run_manifest.json)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
