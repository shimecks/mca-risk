"""Eval suites. One module per suite, named ``e<N>_<slug>.py`` (e.g. ``e1_ingestion.py``).

A suite module must define::

    SUITE_ID = "E1"
    def run(ctx: evals.common.EvalContext) -> evals.common.SuiteOutput: ...

and emit every metric listed under ``gates`` in ``evals/thresholds.json`` for its ID. A suite with no
module yet is reported as ``not_implemented`` by the runner (it does not fail ``make eval``; it does
fail ``make eval SUITE=EN`` because you explicitly asked for it). Raise ``SuiteSkipped`` for
legitimately optional suites (E9 without an LLM key).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import ModuleType

_PAT = re.compile(r"^e(\d+)_[a-z0-9_]+\.py$")


def discover() -> dict[str, str]:
    """{"E1": "evals.suites.e1_ingestion", ...} for every suite module present."""
    out: dict[str, str] = {}
    for p in sorted(Path(__file__).parent.glob("e*_*.py")):
        m = _PAT.match(p.name)
        if m:
            sid = f"E{int(m.group(1))}"
            if sid in out:
                raise RuntimeError(f"two modules implement {sid}: {out[sid]} and {p.stem}")
            out[sid] = f"evals.suites.{p.stem}"
    return out


def load(module_name: str) -> ModuleType:
    return importlib.import_module(module_name)
