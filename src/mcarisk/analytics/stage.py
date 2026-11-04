"""Stage 5 ``analytics``: figures and findings from the persisted metric tables. ``python -m mcarisk.analytics``.

Reads only metric tables written by ``mcarisk.metrics`` (``portfolio_metrics``, ``target_metrics``,
``relationship_metrics``, ``deal_metrics``) and writes:

  build/figures/<name>.png            static figure (matplotlib)
  build/figures/<name>.plotly.json    the same figure as plotly JSON (``plotly.io.from_json``) for the app
  build/figures/data/<name>.csv       the figure's data frame
  build/findings.md                   the findings, every number from a registered metric-table query
  build/findings_numbers.json         key, metric IDs, SQL, value and text of every quoted number

Figure names: masking, bad_rate_vintage, exposure, vintage_curves, refi_quality, segments.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt

from mcarisk import paths
from mcarisk.analytics import data, figures
from mcarisk.analytics.findings import build_findings, check_findings
from mcarisk.manifest import RunManifest, StageRecorder, open_run

FIG_DIR = paths.BUILD_DIR / "figures"
FINDINGS_MD = paths.BUILD_DIR / "findings.md"
FINDINGS_JSON = paths.BUILD_DIR / "findings_numbers.json"


@dataclass
class AnalyticsResult:
    figures: dict[str, dict[str, str]]
    findings: dict
    check: dict
    seconds: float


def render_figure(name: str, df, out_dir: Path, **kw) -> dict[str, str]:
    """Render one figure from its frame and write PNG, plotly JSON and CSV. Returns the paths."""
    out_dir = Path(out_dir)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    mfig, pfig = figures.FIGURES[name](df, **kw)
    png, pj, csv = out_dir / f"{name}.png", out_dir / f"{name}.plotly.json", out_dir / "data" / f"{name}.csv"
    mfig.savefig(png, dpi=130, facecolor=mfig.get_facecolor())
    plt.close(mfig)
    pj.write_text(pfig.to_json(), encoding="utf-8")
    df.to_csv(csv, index=False, lineterminator="\n")
    return {"png": png.as_posix(), "plotly": pj.as_posix(), "csv": csv.as_posix()}


def run_analytics(
    db_path: Path = paths.DB_PATH,
    *,
    fig_dir: Path = FIG_DIR,
    findings_md: Path = FINDINGS_MD,
    findings_json: Path = FINDINGS_JSON,
    run: RunManifest | None = None,
) -> AnalyticsResult:
    t0 = time.perf_counter()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        as_of = str(con.execute("SELECT MAX(as_of) FROM portfolio_metrics").fetchone()[0])
        overall = con.execute(
            "SELECT value FROM target_metrics WHERE metric_id = 'T1' AND metric = 'bad_rate_economic' "
            "AND dimension = 'all'"
        ).fetchone()
        out = {}
        for name, fn in data.DATASETS.items():
            kw = {"overall": overall[0] if overall else None} if name == "segments" else {}
            out[name] = render_figure(name, fn(con), fig_dir, **kw)
        fnd = build_findings(con, findings_md, findings_json, as_of)
        chk = check_findings(con, findings_md, findings_json)
    finally:
        con.close()
    if chk["mismatches"] or chk["unregistered_tokens"]:
        raise RuntimeError(f"findings check failed: {chk}")
    res = AnalyticsResult(out, fnd, chk, round(time.perf_counter() - t0, 3))
    if run is not None:
        st = StageRecorder(run.data["stages"]["analytics"])
        st.extra("figures", {k: v["png"] for k, v in out.items()})
        st.extra("figure_metric_ids", figures.FIGURE_METRICS)
        st.extra("findings", {"path": Path(findings_md).as_posix(), **fnd})
        st.extra("findings_check", {k: v for k, v in chk.items() if k != "mismatches"})
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Portfolio analytics: figures + findings (Part 5).")
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--manifest", type=Path, default=paths.MANIFEST_PATH)
    args = ap.parse_args(argv)
    if not args.db.exists():
        print(f"[analytics] {args.db} not found; run `make metrics` first", file=sys.stderr)
        return 1
    run = open_run(args.manifest)
    with run.stage("analytics"):
        res = run_analytics(args.db, run=run)
    print(f"[analytics] done in {res.seconds:.2f}s -> {FIG_DIR}, {FINDINGS_MD}")
    for k, v in res.figures.items():
        print(f"  {k:<18} {v['png']}")
    print(f"  findings: {res.check['numbers']} registered numbers, all equal their metric-table queries")
    return 0
