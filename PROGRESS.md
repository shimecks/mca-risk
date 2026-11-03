# PROGRESS

Build log for the 7 gated parts in `plan.md`. Each part records what was built, test counts, eval
scores, known gaps, and the conventions later parts must follow. Gate rules: `CLAUDE.md`.

| Part | Status | Tag |
|---|---|---|
| 1 Repo, ingestion, lineage | done | `part-1` |
| 2 Standardization + DQ report | not started | |
| 3 Entity resolution + linkage | not started | |
| 4 Ledger + metrics library | not started | |
| 5 Portfolio analytics | not started | |
| 6 Risk modeling | not started | |
| 7 App, report, demo | not started | |

---

## Part 1: Repo, ingestion, lineage

### What was built
| Path | What it is |
|---|---|
| `pyproject.toml`, `uv.lock`, `.python-version` | uv project, Python 3.12, package `mcarisk` (src layout, hatchling). All deps for Parts 1-7 are already declared (duckdb, pandas, pyarrow, openpyxl, pandera, rapidfuzz, statsmodels, scikit-learn, scipy, lifelines, streamlit, pyyaml, pydantic, openai, matplotlib, plotly; dev: pytest, hypothesis, ruff). ruff + pytest config live here. |
| `Makefile` | `all ingest test lint format eval app clean` + explicit stubs `standardize resolve ledger metrics analytics model report` that print "not implemented until Part N -- skipped". `make eval SUITE=E1`. `make app` exits 1 with a message until Part 7. |
| `.github/workflows/ci.yml` | ubuntu, `astral-sh/setup-uv` (pinned SHA, v10.3.1), `uv sync --locked`, `make lint test`, `make clean all`, `make eval`; uploads manifest + latest eval report. |
| `src/mcarisk/paths.py` | Repo-root-relative paths (`RAW_DIR`, `BUILD_DIR`, `DB_PATH`, `MANIFEST_PATH`, `METRICS_MD`). |
| `src/mcarisk/calendar.py` | Fed ACH calendar: `is_business_day`, `add_business_days`, `business_days_between`, `next_business_day`, `previous_business_day`, `holidays(year)`. Rule-based for any year; pinned by tests for 2022-2027. |
| `src/mcarisk/ingest.py` | Stage 1. `python -m mcarisk.ingest` / `make ingest`. Loads every file in `raw/` into `build/portfolio.duckdb` as text with lineage. |
| `src/mcarisk/manifest.py` | Reusable `build/run_manifest.json` writer (API below). |
| `evals/thresholds.json` | All suites E1-E9 with the plan.md §4 gates verbatim. |
| `evals/run.py`, `evals/common.py`, `evals/suites/` | Runner + E1. Reports to `evals/reports/<UTC ts>.{json,md}` and `latest.{json,md}`. |
| `evals/suites/e1_ingestion.py` | E1 Ingestion, counting the source side independently (raw XML via zipfile/ElementTree for xlsx; raw bytes + pandas for CSV). |
| `tests/` | 65 tests (see below). |
| `.gitignore`, `.gitattributes` | Ignores `build/`, `.venv/`, `.env*`, caches, `evals/reports/*` (keeps `.gitkeep`). `raw/**` and `ground_truth/**` are `-text` so bytes (and hashes) are identical on every OS. |

### Tests: 65 passing (`make lint test` green)
- `test_calendar.py` (28): full Fed holiday schedule 2022-2027 vs published dates; known holidays (2023-07-04, 2024-11-28, 2026-01-01, 2023-01-02 observed for Sunday New Year's, 2022-06-20, 2022-12-26, 2027-07-05); Saturday holidays not moved (2026-07-03, 2027-12-24, 2027-06-18, 2021-12-31, 2023-11-10 are business days); add/between conventions; hypothesis round-trip and brute-force properties.
- `test_ingest_unit.py` (13): header detection (title row + blank row fixture, all-text numeric-looking rows, narrow title rows, failure); cell rendering; column naming; end-to-end synthetic workbook with a title row; extra sheet rejected with reason; ambiguous multi-sheet workbook fails; CSV union with blank/ragged rows; schema drift and unknown files fail loudly.
- `test_ingest_raw.py` (10): every raw file ingested; per-file row counts equal file contents (counted separately); reconciliation identity; lineage non-null and unique; all data columns VARCHAR; `src_row` definition; values kept as text (ACH date-format switch preserved); real Excel dates flagged; reject reasons.
- `test_manifest.py` (4), `test_no_ground_truth.py` (2: static scan of `src/ app/ sql/` + pattern self-test), `test_evals_runner.py` (8: thresholds equal plan gates, gate ops, not_implemented semantics, missing metric fails, E1 passes on clean ingest and **fails** on a dropped row / changed cell).

### Eval scores
`make eval SUITE=E1`: **pass**.
| Metric | Value | Gate |
|---|---|---|
| rows_accounted_rate | 1.0 | >= 1.0 |
| files_reconciled_rate | 1.0 (52/52 files) | >= 1.0 |
| value_fidelity_rate (report-only) | 1.0 (every loaded cell equals the independently read source cell) | |
| lineage_nonnull_rate (report-only) | 1.0 | |

Source: 52 files / 52 sheets, 171,823 non-blank rows = 52 header rows + 171,770 loaded + 1 rejected title row; plus 1 blank row rejected (`raw/deal_tracker_2023.xlsx` rows 1 and 2).

Loaded rows: raw_tracker_2023 114, raw_tracker_2024 405, raw_tracker_2025 1,448 (719 + 729), raw_scorecards 496, raw_collections 726, raw_ach 168,581.

`make all` from a clean `build/`: **~6.6 s wall** (ingest ~4.7 s) on the dev machine.

### Known gaps
- Stages 2-7 are Makefile stubs. `make all` currently means ingest only.
- `make app` exits 1 until Part 7.
- Re-running `make ingest` alone replaces every `raw_*` table but leaves later-stage tables untouched (stale). Use `make clean all` for a consistent build.
- `git.sha` in the manifest is null until the first commit exists. `dirty` ignores untracked files.

### Conventions later parts must follow
**DuckDB tables (`build/portfolio.duckdb`)**
| Table | Source |
|---|---|
| `raw_tracker_2023` | `raw/deal_tracker_2023.xlsx` (sheet `Funded`, header on row 3) |
| `raw_tracker_2024` | `raw/deal_tracker_2024.xlsx` (sheet `2024 Deals`) |
| `raw_tracker_2025` | `raw/deal_tracker_2025.xlsx` + `raw/deal_tracker_2026_YTD.xlsx` (same snake_case schema; tell them apart by `src_file`) |
| `raw_scorecards` | `raw/underwriting_scorecards_2023_2024.xlsx` |
| `raw_collections` | `raw/collections_log.xlsx` |
| `raw_ach` | all 46 `raw/ach_exports/ach_YYYY_MM.csv`, unioned. Overlap duplicates are **kept**; L2 dedupe is Part 2. |
| `raw_rejects` | `src_file, src_sheet, src_row, table_name, reason, detail, raw_values` (JSON array of cell texts) |
| `raw_sources` | one row per file x sheet: `table_name, status (ingested/rejected/empty), header_row, n_physical_rows, n_loaded, n_rejected, n_columns, file_sha256, ...` with `n_physical_rows = header + loaded + rejected` |
| `raw_columns` | table column ← header text, source column number/letter, header row, per file |
| `raw_cell_types` | every non-text, non-empty xlsx cell: `native_type` (int/float/datetime/bool) + `number_format` |

Source families and file patterns are declared in `mcarisk.ingest.SOURCES`. An unrecognized file in `raw/`, a header mismatch inside a family, or a workbook with an unexpected second non-empty sheet makes ingest **fail** rather than guess.

**Column names** are the header text, stripped of surrounding whitespace, exactly as in the file (quote them in SQL: `"Deal #"`, `"Trans Date"`). Blank headers become `col_<letter>`; case-insensitive duplicates get `__2`. Lineage columns come first.

**Lineage** (all NOT NULL in every `raw_*` table; carry them, or a list of them, into every derived row):
- `src_file`: POSIX path relative to the repo root, e.g. `raw/ach_exports/ach_2024_07.csv`.
- `src_sheet`: worksheet name; the literal `(csv)` for CSV files.
- `src_row` (INTEGER): **1-based physical row number**. For xlsx it is the Excel row number, counting title, blank and header rows (the 2023 tracker's first deal is row 4). For CSV it is the 1-based record number with the header as record 1, so the first data record is `src_row = 2`. That is the row Excel shows when the CSV is opened there. The raw CSVs have no multi-line fields, so record = line.

**Cell text** (every data column is VARCHAR; no coercion):
- Empty cell or zero-length CSV field: NULL. Whitespace is kept verbatim.
- xlsx int: `str(int)`. xlsx float: `repr(float)`, the shortest round-trip form (e.g. `2929.69`, `0.109`).
- xlsx real date: ISO `YYYY-MM-DD`, with `THH:MM:SS` if a time is present. Only `raw_tracker_2024."Funded"` has real dates; every other date is text in the workbook. Use `raw_cell_types` to tell a real date from an ISO-looking string. Excel serial numbers that are not date-formatted stay as numbers. Part 2's date parser must still handle serials.
- Bool: `TRUE`/`FALSE`. Formulas: the cached value (`data_only=True`; the raw workbooks contain none).

**Reject reasons**: `blank_row`, `preamble_above_header`, `field_count_mismatch`, `unmapped_sheet`. Add new reasons as constants in the module that rejects, and record them through the manifest.

**Manifest API** (`mcarisk.manifest`); every stage appends to the same run:
```python
from mcarisk.manifest import open_run

run = open_run()  # build/run_manifest.json; ingest calls start_run()
with run.stage("standardize") as st:  # timed; status ok/failed; written on exit
    st.table(
        "deals_std",
        accepted=n_ok,
        rejected=n_bad,
        repaired=n_fix,
        reasons={"rejected": {"unparseable_date": n_bad}, "repaired": {"money_text": n_fix}},
    )
    st.note("...")
    st.extra("key", value)
run.add_llm_usage(model=..., input_tokens=..., output_tokens=..., cost_usd=..., cache_hits=...)  # Part 3
run.write()
```
`st.table` raises if the rejected or repaired counts aren't fully explained by reasons. Top-level keys: `schema_version, run_id, created_at, updated_at, git{sha,dirty}, metrics_md{version,sha256}, inputs{path:{sha256,bytes}}, stages, llm, totals`.

**Calendar**: `business_days_between(a, b)` counts business days in `(a, b]`, so Friday to Monday = 1. `add_business_days(d, n)` gives the n-th business day strictly after `d`, and `n = 0` returns `d` unchanged. Pass `datetime.date` only; a `datetime` raises.

**Adding an eval suite**: create `evals/suites/e<N>_<slug>.py` with `SUITE_ID = "E<N>"` and `def run(ctx: EvalContext) -> SuiteOutput`. It must emit every metric named under `gates` for its ID in `evals/thresholds.json`; a missing gated metric counts as a failure. Optional extra metrics go in `report_only`. The runner discovers the file automatically. `make eval` treats a suite with no module as `not_implemented` and does not fail on it. `make eval SUITE=EN` exits 2 for an unimplemented suite and 1 when a gate fails or the suite errors. Raise `SuiteSkipped` only for optional suites (E9). Gate metric names in `thresholds.json` are the contract. Renaming a key is fine if op and value are unchanged and the rename is noted here. **Never change a gate value** without the user's approval (CLAUDE.md).

**ground_truth/**: only `evals/` reads it. `tests/test_no_ground_truth.py` scans `src/`, `app/` and `sql/` for any spelling of the phrase (`ground_truth`, `ground-truth`, `GroundTruth`, `"ground" / "truth"`), comments and docstrings included.

### Environment notes (dev machine)
- Windows 11 with an Application Control policy (Smart App Control) blocks newly published, unsigned binary wheels. Two symptoms showed up:
  - The uv-managed CPython 3.12.12's `python.exe` was blocked. The venv is therefore built on the system Python 3.12.10 (`uv venv --python C:\Users\shime\AppData\Local\Programs\Python\Python312\python.exe`). CI uses setup-uv's Python.
  - The newest duckdb 2.0.0, pyarrow, scipy 1.18 and statsmodels 0.15 DLLs were blocked. `pyproject.toml` caps them to `duckdb<1.5`, `pyarrow<22`, `scipy<1.17`, `scikit-learn<1.8`, `statsmodels<0.15`, `lifelines<0.31`. These versions are verified to import and run here: duckdb query, sklearn HistGradientBoosting fit, statsmodels Logit + `statsmodels.api`, lifelines KM/Cox import, matplotlib Agg, streamlit import. Relax the caps only after checking imports on this machine.
- GNU make is not on PATH in fresh shells here: `export PATH="/c/Users/shime/AppData/Local/Microsoft/WinGet/Packages/ezwinports.make_Microsoft.Winget.Source_8wekyb3d8bbwe/bin:$PATH"`.
- `python3` is a Windows Store stub here. Use `uv run python`.
- The OpenRouter key for Part 3 lives outside the repo (`C:\Users\shime\MCA\.env`, `OPENROUTER_API_KEY`, `MODEL`). `.env*` is git-ignored.

### Open questions / deviations
1. **E1 extra metrics**: `value_fidelity_rate` and `lineage_nonnull_rate` are report-only (both 1.0), so the gate stays exactly as plan.md states ("100% accounted"). Gating them would be stricter, not looser. Say if you want that.
2. **E6 default-date check**: plan.md §4 lists "default date within ±25 business days" as measured but gives no gate number. It is `report_only` (`default_date_within_25bd_rate`) in `thresholds.json`. Decide whether it needs a gate before Part 4.
3. **E7 "$1 per record"** is encoded as max absolute error ≤ $1.00 for deal and relationship metrics (`*_max_abs_err_usd <= 1.0`), and "±0.5 pp portfolio" as `portfolio_metric_max_abs_err_pp <= 0.5`. **E8 "main-driver signs correct"** is the boolean `main_driver_signs_correct == true`, and leakage tests are pytest-enforced (report-only in E8). **E9** is `optional` with `llm_minus_regex_accuracy >= 0`.
4. **plan.md §1** names the Anthropic SDK for the optional LLM. Per the user's later choice, `openai` (for OpenRouter) is the declared dependency.
5. **Blank-row policy**: fully blank rows are rejected with reason `blank_row` (not loaded and flagged), so `raw_*` tables hold only rows with content.

---

## Part 2: Standardization and data-quality report
_not started_

## Part 3: Entity resolution and linkage
_not started_

## Part 4: Ledger and metrics library
_not started_

## Part 5: Portfolio analytics
_not started_

## Part 6: Risk modeling
_not started_

## Part 7: App, report, reproducibility, demo
_not started_
