"""Shared types for eval suites. ``evals/`` is the only code allowed to read ``ground_truth/``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class EvalContext:
    root: Path = ROOT
    raw_dir: Path = ROOT / "raw"
    build_dir: Path = ROOT / "build"
    db_path: Path = ROOT / "build" / "portfolio.duckdb"
    ground_truth_dir: Path = ROOT / "ground_truth"


@dataclass
class SuiteOutput:
    metrics: dict[str, Any]
    details: dict[str, Any] = field(default_factory=dict)  # free-form, goes into the JSON report
    notes: list[str] = field(default_factory=list)  # short human-readable lines for the .md report


class SuiteSkipped(Exception):
    """Raise from ``run`` when an optional suite cannot run (reason in the message)."""
