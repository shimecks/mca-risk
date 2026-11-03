"""Eval runner: ``python -m evals.run [--suite E1] [--suite E3 ...]`` (``make eval [SUITE=E1]``).

* Runs every suite in ``evals/thresholds.json`` (or only the requested ones).
* Writes ``evals/reports/<UTC timestamp>.json`` and ``.md`` plus ``latest.json`` / ``latest.md``.
* Status per suite: ``pass`` | ``fail`` | ``error`` | ``skipped`` (optional suite) | ``not_implemented``.
* Exit code: 0 if every implemented suite passes; 1 if any gate fails or a suite errors;
  2 if a suite was explicitly requested (``--suite``) but is not implemented yet.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals import suites
from evals.common import ROOT, EvalContext, SuiteOutput, SuiteSkipped

THRESHOLDS = ROOT / "evals" / "thresholds.json"
REPORTS = ROOT / "evals" / "reports"


def load_thresholds(path: Path = THRESHOLDS) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def check_gate(value: Any, gate: dict[str, Any]) -> bool:
    op, target = gate["op"], gate["value"]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == "==":
        return value == target
    if op == "between":
        lo, hi = target
        return lo <= value <= hi
    raise ValueError(f"unknown gate op {op!r}")


def run_suite(sid: str, spec: dict[str, Any], registry: dict[str, str], ctx: EvalContext) -> dict[str, Any]:
    res: dict[str, Any] = {"suite": sid, "name": spec.get("name", sid), "plan_gate": spec.get("plan_gate")}
    if sid not in registry:
        res.update(status="not_implemented", gates={}, metrics={}, notes=["suite module not written yet"])
        return res
    t0 = time.perf_counter()
    try:
        out: SuiteOutput = suites.load(registry[sid]).run(ctx)
    except SuiteSkipped as exc:
        res.update(
            status="skipped" if spec.get("optional") else "error",
            gates={},
            metrics={},
            notes=[f"skipped: {exc}"],
        )
        res["seconds"] = round(time.perf_counter() - t0, 3)
        return res
    except Exception as exc:
        res.update(
            status="error",
            gates={},
            metrics={},
            notes=[f"{type(exc).__name__}: {exc}"],
            traceback=traceback.format_exc(),
        )
        res["seconds"] = round(time.perf_counter() - t0, 3)
        return res
    gates = {}
    for metric, gate in spec.get("gates", {}).items():
        val = out.metrics.get(metric)
        gates[metric] = {
            "value": val,
            "op": gate["op"],
            "target": gate["value"],
            "passed": metric in out.metrics and check_gate(val, gate),
        }
    res.update(
        status="pass" if all(g["passed"] for g in gates.values()) else "fail",
        gates=gates,
        metrics=out.metrics,
        notes=out.notes,
        details=out.details,
        seconds=round(time.perf_counter() - t0, 3),
    )
    return res


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return json.dumps(v) if isinstance(v, (list, dict, bool)) or v is None else str(v)


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Eval report {report['timestamp']}",
        "",
        f"git `{report.get('git_sha') or 'n/a'}` | overall: **{report['overall']}**",
        "",
        "| Suite | Name | Status | Gate (plan) |",
        "|---|---|---|---|",
    ]
    for r in report["suites"]:
        lines.append(f"| {r['suite']} | {r['name']} | {r['status']} | {r.get('plan_gate') or ''} |")
    for r in report["suites"]:
        if r["status"] == "not_implemented":
            continue
        lines += ["", f"## {r['suite']} {r['name']}: {r['status']}", ""]
        if r.get("gates"):
            lines += ["| Metric | Value | Gate | Pass |", "|---|---|---|---|"]
            for m, g in r["gates"].items():
                lines.append(
                    f"| {m} | {_fmt(g['value'])} | {g['op']} {_fmt(g['target'])} | {'yes' if g['passed'] else 'NO'} |"
                )
        other = {k: v for k, v in r.get("metrics", {}).items() if k not in r.get("gates", {})}
        if other:
            lines += ["", "| Report-only metric | Value |", "|---|---|"]
            lines += [f"| {k} | {_fmt(v)} |" for k, v in other.items()]
        for n in r.get("notes", []):
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def _git_sha() -> str | None:
    try:
        from mcarisk.manifest import git_info

        return git_info(ROOT)["sha"]
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run eval suites against evals/thresholds.json")
    ap.add_argument("--suite", action="append", help="suite ID (E1..E9); repeatable; default all")
    ap.add_argument("--reports-dir", type=Path, default=REPORTS)
    args = ap.parse_args(argv)

    thresholds = load_thresholds()
    registry = suites.discover()
    requested = [s.strip().upper() for s in (args.suite or []) if s.strip()]
    unknown = [s for s in requested if s not in thresholds]
    if unknown:
        print(f"unknown suite(s) {unknown}; known: {list(thresholds)}", file=sys.stderr)
        return 2
    selected = requested or list(thresholds)

    ctx = EvalContext()
    results = [run_suite(sid, thresholds[sid], registry, ctx) for sid in selected]
    failed = [r["suite"] for r in results if r["status"] in ("fail", "error")]
    missing = [r["suite"] for r in results if r["status"] == "not_implemented" and r["suite"] in requested]
    overall = "fail" if failed else ("not_implemented" if missing else "pass")

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "timestamp": ts,
        "git_sha": _git_sha(),
        "selected": selected,
        "overall": overall,
        "suites": results,
    }
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    js = json.dumps(report, indent=2, default=str) + "\n"
    md = to_markdown(report)
    for stem in (ts, "latest"):
        (args.reports_dir / f"{stem}.json").write_text(js, encoding="utf-8")
        (args.reports_dir / f"{stem}.md").write_text(md, encoding="utf-8")

    for r in results:
        gates = ", ".join(
            f"{m}={_fmt(g['value'])} ({'ok' if g['passed'] else 'FAIL'})"
            for m, g in r.get("gates", {}).items()
        )
        print(f"[eval] {r['suite']:<3} {r['name']:<22} {r['status']:<16} {gates}")
        if r["status"] == "error":
            print(r.get("traceback", ""), file=sys.stderr)
    print(f"[eval] overall={overall}; report -> {(args.reports_dir / ts).as_posix()}.{{json,md}}")
    if failed:
        return 1
    if missing:
        print(f"[eval] requested suite(s) not implemented yet: {missing}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
