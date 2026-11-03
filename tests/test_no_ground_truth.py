"""Static guard (CLAUDE.md non-negotiable): pipeline code (src/, app/, sql/) never references the truth tables."""

from __future__ import annotations

import re

from mcarisk import paths

PIPELINE_DIRS = ("src", "app", "sql")
# ground_truth, ground-truth, GroundTruth, "ground" / "truth", 'ground' + '_truth', ...
PATTERN = re.compile(r"ground[\s_\-'\"+./\\]*truth", re.IGNORECASE)
SKIP_PARTS = {"__pycache__"}


def scan() -> list[str]:
    hits = []
    for d in PIPELINE_DIRS:
        base = paths.ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or SKIP_PARTS & set(p.parts):
                continue
            text = p.read_bytes().decode("utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), 1):
                if PATTERN.search(line):
                    hits.append(f"{p.relative_to(paths.ROOT).as_posix()}:{i}: {line.strip()}")
    return hits


def test_pipeline_code_never_references_ground_truth():
    assert scan() == []


def test_pattern_catches_variants():
    variants = [
        "ground_truth/deals.csv",
        'ROOT / "ground" / "truth"',
        "GroundTruth",
        "ground-truth",
        "'ground' + '_truth'",
    ]
    for s in variants:
        assert PATTERN.search(s), s
