"""Canonical repository paths. Everything is resolved from the repo root, never from the CWD.

Set ``MCARISK_ROOT`` to point the pipeline at another checkout (tests use explicit arguments instead).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("MCARISK_ROOT", Path(__file__).resolve().parents[2]))
RAW_DIR = ROOT / "raw"
BUILD_DIR = ROOT / "build"
DB_PATH = BUILD_DIR / "portfolio.duckdb"
MANIFEST_PATH = BUILD_DIR / "run_manifest.json"
METRICS_MD = ROOT / "METRICS.md"
