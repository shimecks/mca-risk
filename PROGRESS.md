# PROGRESS

Build log for the 7 gated parts in `plan.md`. Each part records what was built, test counts, eval
scores, known gaps, and the conventions later parts must follow. Gate rules: `CLAUDE.md`.

| Part | Status | Tag |
|---|---|---|
| 1 Repo, ingestion, lineage | done | `part-1` |
| 2 Standardization + DQ report | done | `part-2` |
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

### What was built
| Path | What it is |
|---|---|
| `mappings/tracker_2023.yaml`, `tracker_2024.yaml`, `tracker_2025.yaml` | Raw column -> canonical field for each tracker schema (2025 also covers `deal_tracker_2026_YTD.xlsx`). Field spec: `{column, parser, required?, ...options}`, `{from_hints, default}`, plus named `derivations` / `repairs` / `checks`. |
| `mappings/scorecards.yaml`, `collections.yaml`, `ach.yaml` | Same engine for the other sources. `ach.yaml` also holds the L2 `dedupe_key` and the expected `sign_by_type`. |
| `mappings/industry.yaml`, `status.yaml`, `collection_events.yaml` | Lookup tables: free-text industry -> 10 canonical industries; typed status -> `status_norm`; collections Event -> `event_type`. Case- and whitespace-insensitive; a variant mapped to two canonicals is an error. |
| `src/mcarisk/parse.py` | Pure parsers. Each returns a value, `None` (missing) or a typed `ParseFailure(reason, raw)`; they never guess, round or silently accept negatives. money -> int cents (Decimal, `$`, commas, parentheses, sub-cent = failure, negative = failure unless `allow_negative`); `parse_rate` (percent vs fraction; `%` wins, a bare number uses the mapping's declared unit); factor (`1.35x`); dates (MM/DD/YY, MM/DD/YYYY, M/D/YYYY, ISO, ISO+`T00:00:00`, YYYY/MM/DD, Excel serial 1990-2100; month-first only, day-first is a failure); frequency; term; payment (`"$x wkly"`); EIN; state; lookups; `installment_cents`. |
| `src/mcarisk/mapping.py` | Generic engine: `load_mapping`, `load_lookups`, `apply_row` -> values + `LogEntry`s (`rejected` / `nulled` / `repaired` / `derived`). Derivation `rtr_over_payment`; repair `payment_whole_dollar_from_rtr_term`. |
| `src/mcarisk/schemas.py` | pandera (`pandera.pandas`) schemas for `deals_std`, `payments_std`, `payments_dupes`, `scorecards_std`, `collections_std`: strict columns, Int64 cents, `datetime.date` (not datetime), lineage not null, unique ids, L2 key unique in `payments_std`, value sets. `validate()` uses `lazy=True` and raises `SchemaErrors`. |
| `src/mcarisk/standardize.py` | Stage 2. `python -m mcarisk.standardize` / `make standardize` (wired into `make all`). Writes every table below in one transaction, plus `build/dq_report.md` and the manifest stage `standardize`. |
| `evals/suites/e2_standardization.py`, `e3_dedupe.py` | E2 and E3 (details below). `thresholds.json`: report-only key lists extended; gates unchanged. |
| `tests/test_parse.py` (111), `tests/test_standardize.py` (33), `tests/test_evals_runner.py` (+3), `tests/conftest.py` (`std_build` session fixture) | |

### Tests: 212 passing (`make lint test` green; Part 1's 65 still pass)
- `test_parse.py` (111): unit tests on every variant seen in raw/ (money, int, rate, factor, dates, frequency, term, payment, EIN, state, lookup) and failure codes. Hypothesis: money round-trips (`$1,234.56`, plain, `repr(float)`, whole dollars); negatives never parse silently (`-`, `()`, `$-`); arbitrary text never yields a negative; the date parser handles every format for any date 1990-2099 (MM/DD/YY only for 1970-2069); percent/fraction agree; weeks = 5 business days; a derived term always round-trips to the stated installment exactly.
- `test_standardize.py` (33): 2023 weekly row (`"$2,812 wkly"`, `"28 wks"`) incl. the whole-dollar repair and its limits; conflicting frequency hints null the field; 2024 term derivation (exact only); required failure rejects with raw cells kept; optional failure nulls + logs; missing raw column fails loudly; **the July 2024 dedupe trap** (`06/28/2024` in the June export vs `2024-06-28` in the July export: one kept, June copy kept, dupe records both lineages; raw-text dedupe would keep two; distinct events never merge); the same on real data; pandera failures raise (negative money, datetime/text dates, bad frequency, factor, term 0, null lineage, bad status, duplicate deal id, extra column, repeated L2 key, zero amount); **row accounting**: every raw row appears exactly once in `{*_std, std_rejects, payments_dupes}` at its lineage, and nothing points at a non-existent raw row; counts, types, data_quality rules, manifest, lineage, raw tables untouched.
- `test_evals_runner.py` (+3): E2/E3 pass on the standardized build; E2 detects a changed payment and a nulled term; E3 detects a surviving duplicate.

### Eval scores
`make eval`: **overall pass** (E1 pass, E2 pass, E3 pass, E4-E9 not implemented).

**E2 Standardization** (gate `field_accuracy >= 0.995`): **1.0** (11,802 / 11,802 field values over 1,967 tracker deals; 0 unknown deal ids).
| Field | Accuracy | | File | Accuracy |
|---|---|---|---|---|
| advance | 1.0 | | deal_tracker_2023.xlsx | 1.0 |
| rtr | 1.0 | | deal_tracker_2024.xlsx | 1.0 |
| payment | 1.0 | | deal_tracker_2025.xlsx | 1.0 |
| funding_date | 1.0 | | deal_tracker_2026_YTD.xlsx | 1.0 |
| factor | 1.0 | | | |
| term (business days) | 1.0 | | | |

Report-only, also 1.0: fee, commission_rate, frequency (n=1,967); explicit prior_deal_id (634), payoff_amt (474), fico, avg_monthly_rev, tib_months, positions, nsf_90d, industry, state (2025+ inline features where given). Coverage 1,967 / 2,011 truth deals = 0.978: the 44 missing deals have no tracker row (Q5, Part 3), so they are not standardization output.

**E3 Dedupe** (gate: exact): count diff **0**, sum diff **0 cents**. `payments_std` 152,941 rows / 4,108,841,584 cents = the true ledger. Comparison: every `ground_truth/cashflows.csv` row except `commission_out` (paid through AP, never in ACH): funding_out<->FUNDING, debit<->DEBIT, return<->RETURN, internal_payoff + early_payoff_cash<->PAYOFF, settlement<->SETTLEMENT; truth `round(amount*100)`. Signs agree as exported (internal_payoff positive in both). Per-type counts and sums are exact; the (date, type, amount) multiset matches exactly. Deal attribution is not gated: 227 rows sit on a different deal than truth (mispostings, L3 / Part 3). Deduping on the raw date text would leave 153,066 rows (+125): the July 2024 trap.

**Runtime**: `make clean all` ~15 s wall on the dev machine (ingest ~4.6 s, standardize ~6.5 s). `make test` ~38 s.

### Known gaps
- Nothing is linked yet: no merchant key, renewal links from notes, payoff classification or misposting repair (Part 3). `deals_std.prior_deal_id` holds explicit links only.
- Q5 only detects (44 deal numbers with ACH activity and no tracker row); reconstruction is Part 3. Q6 (stale status) needs outcomes (Part 4).
- On this data nothing is rejected or nulled; the reject/null paths are covered by unit tests only.
- `payments_std` has no `std_flags` column because ACH rows are never repaired, only rejected or deduped.
- standardize is pure Python row mapping (memoized per field value); ~6 s is fine for now.

### Conventions Part 3+ must follow
**Tables (`build/portfolio.duckdb`)**. Money is BIGINT cents, dates are DATE, lineage is `src_file, src_sheet, src_row` from the raw row (Part 1 definition).
| Table | Grain / key | Columns |
|---|---|---|
| `deals_std` | one per tracker row; `deal_id` unique | `deal_id, source_family (tracker_2023/2024/2025), merchant_name_raw, dba_raw (2023 only), ein (NN-NNNNNNN, 2025+ only), funding_date, advance_cents, factor (DOUBLE), rtr_cents, payment_cents (installment as stated: weekly amount for weekly deals), frequency (daily/weekly), term_bdays, fee_cents, commission_rate (DOUBLE fraction, 0.099 = 9.9%), iso_name, prior_deal_id (explicit field only: 2024 Prior Deal, 2025 renewal_of; method = explicit), payoff_amt_cents (2025+ only), status_raw, status_norm, notes_raw, fico, avg_monthly_rev_cents, tib_months, industry_raw, industry, state, positions, nsf_90d (inline UW: 2025+ only), std_flags` + lineage |
| `payments_std` | one per distinct ACH event after L2; `payment_id` 1..n ordered by (date, deal, type, batch, amount) | `txn_date, deal_id_posted (AS POSTED; mispostings not repaired), merchant_name_raw, amount_cents (signed as exported: FUNDING/RETURN < 0; DEBIT/PAYOFF/SETTLEMENT > 0), txn_type, return_code, batch_id, description (verbatim; "RENEWAL PAYOFF" etc. are L1 hints), date_format_raw, n_copies` + lineage of the **kept** copy (always the export for the transaction's own month) |
| `payments_dupes` | one per dropped L2 duplicate | `payment_id` (of the kept row), key fields, `date_text_raw, date_format_raw, kept_src_file, kept_src_row` + the dropped row's own lineage |
| `scorecards_std` | `scorecard_id` | `business_name_raw, app_date, requested_cents, fico, avg_monthly_rev_cents, nsf_90d, positions, tib_years (DOUBLE, as given; 1 decimal), industry_raw, industry, state, uw_notes_raw, std_flags` + lineage |
| `collections_std` | `collection_id` | `log_date, merchant_name_raw, deal_id_raw (NULL ~25%), event_raw, event_type (default/bankruptcy/modification/settlement/write_off), balance_cents, amount_cents, notes_raw (verbatim: new payment amounts, settlement terms), std_flags` + lineage |
| `std_rejects` | raw rows not standardized | lineage, `source_table, target_table, reason (<field>:<failure> or duplicate_deal_id), detail, raw_values (JSON)` |
| `std_log` | one per field event | lineage, `target_table, field, action (rejected/nulled/repaired/derived), reason, raw_value, value` |
| `std_accounting` | one per raw table | `n_raw = n_loaded + n_rejected + n_l2_duplicates`, `n_repaired` (loaded rows with a log entry). The stage raises if the identity breaks. |
| `data_quality` | one per rule | `rule_id, rule, owner_part, status (evaluated / partial / pending (Part N)), hits, denominator, sample_rows (JSON lineage), detail`. Rules Q1-Q7, L1-L3, plus S1-S6 (standardization cross-field checks: RTR vs advance x factor, payment vs RTR/term, funding year vs tracker, explicit prior deal unknown/not earlier (17 hits: priors without a tracker row), missing contract field, ACH sign). Later parts should **update their rule's row** (same columns) and regenerate `build/dq_report.md`. |

**Units**: term = business days = number of daily-equivalent remittance days (the trackers' `Term (days)`/`term` columns; `N wks` = 5N). Daily equivalent (METRICS.md §0) = `payment_cents` for daily deals, `payment_cents / 5` for weekly.

**Raw text kept for linking (Part 3)**: `merchant_name_raw` (trackers, ACH, collections), `business_name_raw` (scorecards), `dba_raw`, `ein`, `notes_raw` (2023 renewal links live only here: `renewal of NNNN`, `paid off #NNNN`, `refi #NNNN`, `ren. prev deal NNNN`; also `recon approved pmt $x`), `description` (ACH), collections `notes_raw`, `status_raw`. Names are stored verbatim (no trim or case change).

**Manifest**: stage `standardize` has tables `deals_std, payments_std, scorecards_std, collections_std`. `payments_std.rejected` counts unparseable rows **plus** L2 duplicates (reason `l2_duplicate`). Repaired reasons are per row (`std_flags`, `;`-joined). `extra`: `accounting`, `field_events`, `data_quality`.

**Adding a source or schema**: add a YAML in `mappings/` and register it in `standardize.py` (`TRACKER_MAPPINGS` for trackers). New parsers / derivations / repairs go in `mapping.PARSERS` / `DERIVATIONS` / `REPAIRS` and fail loudly if a YAML names an unknown one.

### Open questions / deviations
1. **Whole-dollar installment repair (2023 tracker, 7 rows)**: about 15% of 2023 money cells are typed without cents. That loses nothing for advance, RTR and fee (whole dollars in this book) but loses the cents of 7 installments (`"$1,150"` for $1,150.25). They are rebuilt as RTR / term (half-up to the cent) only when the text has no cents and is within $0.50 of it. Each is logged in `std_log` (action `repaired`). Without the repair E2 would be 0.99941, still above the gate. Say if you'd rather keep the typed value.
2. **2024 term is derived** (405 rows; that sheet has no term column) as the term whose installment reproduces the stated payment to the cent. Logged as `derived`; NULL if no exact term exists (never happened).
3. **`status_norm`** is a convenience normalization of what was typed (`mappings/status.yaml`). It is not an outcome, and Q6 says it is never trusted for one.
4. **Mispostings**: this seed's `anomalies.csv` lists **227** misposted rows, not the "250" in plan.md/the brief. E3 shows exactly 227 rows on a different deal, so Part 3's L3 recall should be measured against 227.
5. S1-S6 are new standardization checks listed in `data_quality`. They only count rows and are not METRICS.md definitions.
6. Scorecard TIB is in years with 1 decimal (`tib_years`); the 2025 tracker has `tib_months`. Converting years x 12 is approximate; Part 6 should treat them as different precisions.

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
