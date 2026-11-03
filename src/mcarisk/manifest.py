"""Run manifest: ``build/run_manifest.json`` (plan.md §3 "Lineage").

One manifest describes one pipeline run. The first stage (ingest) starts a fresh run with
:func:`start_run`; every later stage opens the same file with :func:`open_run` and appends its own
stage record. Typical use in a stage::

    from mcarisk.manifest import open_run

    run = open_run()                       # or start_run(inputs_dir=RAW_DIR) in the first stage
    with run.stage("standardize") as st:
        ...
        st.table("deals_std", accepted=1950, rejected=3, repaired=12,
                 reasons={"rejected": {"unparseable_date": 3}, "repaired": {"money_text": 12}})
        st.note("dq rules evaluated: Q1-Q7")
    run.write()                             # stage() also writes on exit

Layout (top-level keys are a contract; ``tests/test_manifest.py`` checks them)::

    {
      "schema_version": 1,
      "run_id": "20261103T153000Z-ab12cd3",
      "created_at" / "updated_at": ISO-8601 UTC,
      "git": {"sha": str | null, "dirty": bool | null},
      "metrics_md": {"version": "v1.0", "sha256": str},
      "inputs": {"raw/deal_tracker_2023.xlsx": {"sha256": str, "bytes": int}, ...},
      "stages": {
        "<stage>": {"status": "ok" | "failed", "started_at", "finished_at", "seconds",
                    "tables": {"<table>": {"accepted", "rejected", "repaired",
                                           "reasons": {"rejected": {reason: n}, "repaired": {reason: n}}}},
                    "notes": [str], "extra": {...}}
      },
      "llm": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
              "cache_hits": 0, "models": {}},
      "totals": {"seconds": float}
    }
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcarisk import paths

SCHEMA_VERSION = 1
REQUIRED_KEYS = (
    "schema_version",
    "run_id",
    "created_at",
    "updated_at",
    "git",
    "metrics_md",
    "inputs",
    "stages",
    "llm",
    "totals",
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def git_info(root: Path = paths.ROOT) -> dict[str, Any]:
    """Current commit SHA and whether the worktree has uncommitted changes (None if git unavailable)."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty": None}
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        dirty: bool | None = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return {"sha": sha or None, "dirty": dirty}


_VERSION_RE = re.compile(r"^#\s.*\((v\d+(?:\.\d+)*)\)\s*$", re.MULTILINE)


def metrics_md_info(path: Path = paths.METRICS_MD) -> dict[str, Any]:
    """Version parsed from METRICS.md's title line (``# ... (v1.0)``) plus the file's sha256."""
    text = path.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    return {"version": m.group(1) if m else None, "sha256": sha256_file(path)}


def hash_inputs(inputs_dir: Path, root: Path = paths.ROOT) -> dict[str, dict[str, Any]]:
    """sha256 + size of every file under ``inputs_dir``, keyed by POSIX path relative to ``root``."""
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(x for x in inputs_dir.rglob("*") if x.is_file()):
        try:
            key = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            key = p.as_posix()
        out[key] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
    return out


@dataclass
class StageRecorder:
    """Mutable record for one stage; obtained from :meth:`RunManifest.stage`."""

    data: dict[str, Any]

    def table(
        self,
        name: str,
        *,
        accepted: int,
        rejected: int = 0,
        repaired: int = 0,
        reasons: dict[str, dict[str, int]] | None = None,
    ) -> None:
        reasons = reasons or {}
        for kind, total in (("rejected", rejected), ("repaired", repaired)):
            got = sum(reasons.get(kind, {}).values())
            if total and got != total:
                raise ValueError(
                    f"{name}: {kind}={total} but reasons sum to {got}; every {kind} row needs a reason"
                )
        self.data["tables"][name] = {
            "accepted": int(accepted),
            "rejected": int(rejected),
            "repaired": int(repaired),
            "reasons": {
                "rejected": {k: int(v) for k, v in sorted(reasons.get("rejected", {}).items())},
                "repaired": {k: int(v) for k, v in sorted(reasons.get("repaired", {}).items())},
            },
        }

    def note(self, text: str) -> None:
        self.data["notes"].append(text)

    def extra(self, key: str, value: Any) -> None:
        self.data["extra"][key] = value


@dataclass
class RunManifest:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    # ---- stages -------------------------------------------------------------------------------
    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[StageRecorder]:
        """Time a stage and record its tables. The manifest is written on exit, success or failure."""
        rec = {
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "seconds": None,
            "tables": {},
            "notes": [],
            "extra": {},
        }
        self.data["stages"][name] = rec
        t0 = time.perf_counter()
        try:
            yield StageRecorder(rec)
            rec["status"] = "ok"
        except BaseException as exc:
            rec["status"] = "failed"
            rec["notes"].append(f"error: {type(exc).__name__}: {exc}")
            raise
        finally:
            rec["seconds"] = round(time.perf_counter() - t0, 3)
            rec["finished_at"] = _now()
            self.write()

    # ---- LLM accounting (Part 3 notes extractor) ----------------------------------------------
    def add_llm_usage(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float = 0.0,
        calls: int = 1,
        cache_hits: int = 0,
    ) -> None:
        llm = self.data["llm"]
        llm["calls"] += calls
        llm["input_tokens"] += input_tokens
        llm["output_tokens"] += output_tokens
        llm["cost_usd"] = round(llm["cost_usd"] + cost_usd, 6)
        llm["cache_hits"] += cache_hits
        m = llm["models"].setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        m["calls"] += calls
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["cost_usd"] = round(m["cost_usd"] + cost_usd, 6)

    # ---- persistence --------------------------------------------------------------------------
    def write(self) -> Path:
        self.data["updated_at"] = _now()
        self.data["totals"]["seconds"] = round(
            sum((s.get("seconds") or 0.0) for s in self.data["stages"].values()), 3
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return self.path


def _empty(root: Path, metrics_md: Path) -> dict[str, Any]:
    git = git_info(root)
    created = _now()
    stamp = created[:19].replace("-", "").replace(":", "") + "Z"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{stamp}-{(git['sha'] or 'nogit')[:7]}",
        "created_at": created,
        "updated_at": created,
        "git": git,
        "metrics_md": metrics_md_info(metrics_md),
        "inputs": {},
        "stages": {},
        "llm": {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "cache_hits": 0,
            "models": {},
        },
        "totals": {"seconds": 0.0},
    }


def start_run(
    path: Path = paths.MANIFEST_PATH,
    *,
    inputs_dir: Path | None = paths.RAW_DIR,
    root: Path = paths.ROOT,
    metrics_md: Path = paths.METRICS_MD,
) -> RunManifest:
    """Begin a new run (overwrites any previous manifest). Called by the first pipeline stage."""
    data = _empty(root, metrics_md)
    if inputs_dir is not None:
        data["inputs"] = hash_inputs(inputs_dir, root)
    run = RunManifest(Path(path), data)
    run.write()
    return run


def open_run(path: Path = paths.MANIFEST_PATH, **start_kwargs: Any) -> RunManifest:
    """Open the current run to append a stage; starts a new run if no manifest exists yet."""
    path = Path(path)
    if not path.exists():
        return start_run(path, **start_kwargs)
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"{path} is not a valid run manifest (missing {missing}); run `make clean all`")
    return RunManifest(path, data)
