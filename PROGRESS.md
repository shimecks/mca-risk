# PROGRESS

Build log for the 7 gated parts in `plan.md`. Each part records what was built, test counts, eval
scores, known gaps, and the conventions later parts must follow. Gate rules: `CLAUDE.md`.

| Part | Status | Tag |
|---|---|---|
| 1 Repo, ingestion, lineage | done | `part-1` |
| 2 Standardization + DQ report | done | `part-2` |
| 3 Entity resolution + linkage | done | `part-3` |
| 4 Ledger + metrics library | built; **gate pending** (E6 D17 recall, E7 portfolio, M0219 commission): user decisions, see Part 4 open questions | |
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
2. **E6 default-date check**: plan.md §4 lists "default date within ±25 business days" as measured but gives no gate number. It is `report_only` (`default_date_within_25bd_rate`) in `thresholds.json`. Decide whether it needs a gate before Part 4. **User decision (2026-11-03): E6 default-date-within-±25-business-days stays report-only.**
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

**Status: done, tagged `part-3`.** A first run under METRICS.md v1.0 failed one gate: E5 L1 scored
0.9737 because 22 renewals go into deals that have no tracker row. The user adopted **METRICS.md v1.1**
(2026-11-03), which adds an L1 condition for that case (Open question 1, resolved). `infer` is now the
default policy; `strict` reproduces v1.0 for comparison. All gates pass.

### What was built
| Path | What it is |
|---|---|
| `src/mcarisk/resolve/names.py` | Q1 normalizer: casefold, `&` -> `and`, `L.L.C.`/`Inc.` handling, punctuation stripped, trailing legal suffix split off and standardized (`llc inc corp co ltd lp llp pllc`). `name_score` = rapidfuzz `token_set_ratio` on the cores. It is **0 when both names carry different suffixes**: `Amber Physical Therapy Co` and `... Corp` are different merchants, and 159 legal-name cores in this book differ only by suffix. `compact_score` (space-free ratio >= 95) is a last-resort lookup tier for glued-word typos and is never used for merges. |
| `src/mcarisk/resolve/merchants.py` | Q1 clustering. The atomic record is a **deal**. Every name seen on a deal belongs to its merchant: the tracker name, every ACH name posted to it, collections names that carry its deal #, and the 2023 DBA. The anchor is the deal's most frequent ACH name. Merge methods: `exact` (same normalized anchor), `ein` (same EIN), `fuzzy` (token-set >= 92 within the first-token block, vetoed when EINs or suffixes conflict). Each union is a `merchant_merges` row. `merchant_key` = `MK` + sha1(canonical normalized name)[:10], so it is stable from run to run. `AliasIndex` resolves name-only records tier by tier: `alias_exact` -> `alias_core`/`dba_alias` -> `fuzzy` (in block) -> `fuzzy_cross_block` (first-token typo; searches blocks whose first token is >= 75 similar) -> `fuzzy_compact`. One name can return several candidate merchants (a shared DBA, a suffix-dropped name); Q3/Q4 choose among them by date. |
| `src/mcarisk/resolve/renewals.py` | Q2 in priority order: explicit field (confidence 1.0) -> notes regex (0.95; `renewal of NNNN`, `RENEWAL - paid off #NNNN`, `refi #NNNN`, `ren. prev deal NNNN`) -> `inferred` from L1 (carries the L1 confidence). Validation: an unknown or later prior is dropped and queued; a prior on another merchant is kept at 0.7 and queued. Also queued: explicit vs notes conflicts, one prior with several renewals, and notes saying "renewal"/"rnwl" with no link. |
| `src/mcarisk/resolve/payoffs.py` | L1 per METRICS.md v1.1. N must be the same resolved merchant and fund within 2 Fed-ACH business days of t. Then one of: a tracker link; \|payoff - N.payoff_amt\| <= $1; \|N.advance - N.fee - \|funding_out\| - payoff\| <= $1; or (v1.1) N has no tracker row (`inferred_tracker_missing`, confidence 0.6, queued). The description is a hint only; disagreements are queued. Each row records the criteria, the method, the confidence and the candidates. Policy: `infer` (default, v1.1) or `strict` (v1.0, for comparison). |
| `src/mcarisk/resolve/misposting.py` | L3: a DEBIT posted to P strictly after P's internal payoff date, while N is funded and not yet paid off, is reassigned to N and logged in `misposting_repairs`. A debit after a payoff that cannot be reassigned stays as posted and is queued. |
| `src/mcarisk/resolve/matching.py` | Q3 (scorecard <-> deal): candidate merchants from the name; the deal must fund 0-10 calendar days after the app date; the closest wins. Ties go to the higher name score, then the lower deal id, and are queued; a deal claimed twice is queued. Q4 (collections <-> deal): the deal # if present. Otherwise the candidate merchants' deal that is open on the log date: funded on or before the date and not closed before it (close = payoff date, or the date cumulative cash reached RTR - 1 cent). Several open: the most recent, queued. None open: the latest funded, at confidence 0.5, queued. The name tiers are walked until one yields an eligible deal. |
| `src/mcarisk/resolve/completeness.py` | Q5: the 44 deals with ACH rows and no tracker row are reconstructed. They get the funding date and net funding from the FUNDING row (with its lineage), the anchor name, the modal debit amount (`payment_cents_ach`) and the frequency from `DAILY/WEEKLY REMIT`. Contract fields stay NULL. Flag: `tracker_missing`. |
| `src/mcarisk/resolve/stage.py`, `__main__.py` | Stage `resolve` (`make resolve`, wired into `make all`). It writes the tables below in one transaction, updates `data_quality` rows Q1-Q5, L1 and L3, regenerates `build/dq_report.md` (adding table counts and the review queue), and records the manifest stage and LLM usage. |
| `src/mcarisk/notes/extract.py` | E9: a regex baseline plus an LLM extractor. The LLM path uses the OpenAI client pointed at OpenRouter: `chat.completions` with a strict `json_schema` response format, validated into the pydantic `NoteTerms`, 20 notes per batch. Credentials come from env `OPENROUTER_API_KEY`/`MODEL`, else from the dotenv at `MCARISK_DOTENV` (default `../.env`), and are never logged. Results are cached in `cache/llm_notes.jsonl`, committed, 258 entries, key = sha256(model, prompt version, note). `make notes-llm` fills cache misses. |
| `cache/llm_notes.jsonl` | Synthetic notes, their extractions and each note's share of tokens and cost. No credentials. |
| `evals/suites/e4_entity_resolution.py`, `e5_linkage.py`, `e9_notes.py`, `evals/truth.py` | E4, E5, E9. `ground_truth/` has no row key for scorecards or collections rows, so `evals/truth.py` reconstructs their true deal. Scorecards: the deal with identical underwriting features, funded 1-7 days after the app date. Collections rows: the deal # if present, else the event-date rule plus the name. `thresholds.json`: report-only lists extended; **no gate changed**. |
| `Makefile` | `resolve` stage is real; new `notes-llm` target. `.gitattributes`: `*.jsonl` stored with LF. |

### Tests: 304 passing (`make lint test` green; the 212 from Parts 1-2 still pass)
- `test_resolve_unit.py` (53, counting parametrized cases; includes the v1.1 default-policy test, both policies keeping a cash payoff cash when the same-day tracker-missing deal belongs to *another* merchant, and a tracker-missing deal outside the window):
  - **Normalizer, every variant pattern:** upper case, `L.L.C.`/`L.L.C`, `, Inc.`, `&`/`and`, whitespace, Incorporated/Company, suffix dropped, one-char typos (including inside the suffix and `LCL`), suffix conflict, `Keystone Market` vs `Marketing`, empty name.
  - **Clustering:** exact merge with DBA alias kinds, EIN merge across a DBA anchor, fuzzy merge of a typo anchor, EIN veto, a suffix conflict never merges, stable keys.
  - **Alias tiers:** exact; an ambiguous suffix-dropped name returns two merchants; cross-block first-token typo; compact glued words.
  - **Q2:** regex on each pattern and flag; priority and validation; explicit beats conflicting notes; L1 inference; "renewal" note with no link.
  - **L1:** tracker link; **a payoff with no payoff_amt** (formula); payoff_amt tolerance ($1.00 in, $1.01 out); **a cash payoff the same week as an unrelated new deal**, plus another merchant's matching deal the same day, with the hint disagreement queued; **business-day window over Labor Day 2024** (Fri -> Wed = 2 bd, in; Thu = 3 bd, out; 2 bd before, in); tracker-missing policy strict vs infer; env parsing.
  - **Misposting:** debits before, on and after the payoff; after N closed; after a cash payoff; N's own debit.
  - **Q3 tie-breaks:** closest deal; 0 and 11 days; a shared DBA resolved by date; equal-gap tie.
  - **Q4 tie-breaks:** deal # wins; open deal by date; nothing funded yet; name vs deal # disagreement; several open; none open.
  - Close dates, including the 1-cent tolerance.
- `test_notes.py` (19):
  - Regex on every phrasing.
  - The pydantic schema rejects negatives, a bad period, zero installments, extra fields and non-numeric JSON.
  - Mocked client: one call; then a fresh process is served from the cache with zero calls; a different model is a cache miss (auto -> regex, still no call).
  - An auth error, garbage JSON or missing items never break the pipeline: regex fallback, nothing cached.
  - No key means no call. Dotenv parsing. The committed cache covers every collections note.
- `test_resolve_build.py` (18), on the real build:
  - Every ACH deal has a `deals_resolved` row (44 reconstructed, contract fields NULL).
  - `payments_resolved` matches `payments_std` row for row (posted deal, amount and lineage untouched). Only DEBITs move, and each move has a repair row pointing to a renewal of the posted deal. Ledger kinds and `is_cash` are consistent.
  - Every PAYOFF is classified with a method and confidence; plain "PAYOFF" rows are classified both ways.
  - Links have method and confidence, and priors are earlier.
  - Lineage is non-null in 8 tables. Q3/Q4 coverage and window hold. Exceptions have reasons.
  - DQ rows are evaluated, the manifest stage is recorded, std tables are untouched, and the run is deterministic.
- `test_evals_runner.py` (+2 new, 1 updated): all suites on the resolved build (E1-E4 pass, E8 not_implemented, exit code consistent). E4/E5 run on the build and **detect** a merged cluster, dropped regex links, unrepaired mispostings and flipped payoff classes.

### Eval scores (`make eval`, METRICS.md v1.1, default `infer` policy): **overall pass**
| Suite | Metric | Value | Gate |
|---|---|---|---|
| E1-E3 | (unchanged) | pass | |
| E4 | pairwise F1 / P / R | **1.0** / 1.0 / 1.0 (2,011 deals -> 1,200 clusters = 1,200 merchants) | F1 >= 0.98: pass |
| E4 | deal -> merchant accuracy | 1.0 | report |
| E4 | scorecard merchant accuracy; collections merchant accuracy (deal # / name only) | 1.0; 1.0 / 0.989 (86/87) | report |
| E5 | renewal link precision / recall | **1.0 / 1.0** (811 of 811) | P >= 0.98, R >= 0.97: pass |
| E5 | by method (links, precision, share of true) | explicit 634, 1.0, 0.782; regex 44, 1.0, 0.054; inferred 133, 1.0, 0.164 (111 via the funding formula + 22 `inferred_tracker_missing`) | report |
| E5 | **L1 accuracy** | **1.0** (836/836) | >= 0.99: pass |
| E5 | L1 by method | tracker_link 678/678; funding_formula 111/111; no_successor 25/25; inferred_tracker_missing 22/22 | report |
| E5 | L1 successor accuracy; description hint alone | 1.0; 0.505 | report |
| E5 | L3 recall / precision | **1.0** (227/227) / 1.0 | R >= 0.95: pass |
| E5 | rows on the wrong deal: as posted -> after repair | 227 -> 0 | report |
| E5 | scorecard match rate (Q3) | 1.0 (496/496) | report |
| E5 | collections link accuracy (deal # / name only) | 1.0 / 0.989 | report |
| E9 | regex / LLM accuracy | 0.8815 / 0.8815 (253/287 each). Both are **1.0 on notes that state an amount**; the 34 misses are "Settled - see agreement in drive", which states no amount | LLM - regex >= 0: pass (0.0) |
| E9 | LLM cost | $0.005774 (13 calls, 10,928 input + 9,363 output tokens, `openai/gpt-6-luna`) | report |

For comparison, with `MCARISK_L1_TRACKER_MISSING=strict` (v1.0): links P/R 1.0/0.9729, L1 0.9737 (FAIL), L3 recall 0.9604 (218/227), 9 rows left on the wrong deal. Every other number is the same.

**Runtime**: `make clean all` takes **about 22 s** wall with no key and no network (ingest 3.1 s, standardize 7.3 s, resolve 6.2 s). `make test` takes about 74 s.

### Known gaps
- The 22 `inferred_tracker_missing` payoffs rest on merchant + timing alone (confidence 0.6). All 22 are in the review queue.
- Q4 has one unresolved name-only collections row. `Crescent Dental` is a DBA never seen on any of its merchant's deals; it is queued. There is also one open-deal tie, queued. Q4's "open" uses Part 3's close date (payoff, or cash reaching RTR); Part 4's outcome derivation may refine it.
- Reconstructed deals have no advance, factor, RTR, fee or commission, because ACH does not carry them. Part 4 must treat their D1-D6 as unknown, or estimate and flag them. Their cash events are complete.
- On this book the LLM adds nothing over regex because the notes are templated; E9 shows parity, not lift.
- The `fuzzy` merge path made 0 merges here, because every deal's anchor is an exact legal name. The path is unit-tested.

### Conventions Part 4+ must follow
**Tables (`build/portfolio.duckdb`)**. Money is BIGINT cents, dates are DATE, lineage is `src_file, src_sheet, src_row`.
| Table | Grain | Key columns |
|---|---|---|
| `deals_resolved` | one per deal (1,967 tracker + 44 `tracker_missing`) | All `deals_std` columns (`prior_deal_id` renamed `prior_deal_id_explicit`), plus: `merchant_key, merchant_method, merchant_score, anchor_name, tracker_missing, funding_payment_id`; `funding_net_cents` (= -ACH FUNDING); `payment_cents_ach, frequency_ach` (reconstructed rows only); `prior_deal_id, link_method, link_confidence, next_deal_id` (resolved Q2); `closed_by_payoff` (internal_payoff / early_payoff_cash / NULL); `payoff_date`. **From Part 4 on, use this table, not `deals_std`.** Reconstructed rows have `source_family = 'ach_reconstructed'`, `std_flags = 'tracker_missing'` and NULL contract fields. |
| `merchants` | one per `merchant_key` | `canonical_name, name_norm, block, ein, n_deals, deal_ids, first_funding_date, last_funding_date` (the relationship unit, METRICS.md §3) |
| `merchant_aliases`, `merchant_merges` | one per name / one per union | aliases: `source` (ach/tracker/dba/collections), `alias_kind` (legal_variant/dba); merges: `method, score, record_a, record_b` |
| `deal_links` | one per renewal N | `deal_id` (N), `prior_deal_id` (P), `method` (explicit/regex/inferred), `confidence`, `evidence`, plus N's lineage |
| `payoff_classes` | one per ACH PAYOFF | `payoff_class` (`internal_payoff`/`early_payoff_cash`), `successor_deal_id`; `l1_tracker_link, l1_payoff_amt, l1_funding_formula` (NULL = not evaluable); `method, confidence, description_hint, hint_agrees` |
| `payments_resolved` | one per `payments_std` row | **`deal_id` = repaired attribution; use it for every deal metric.** `deal_id_posted` (as exported); `repaired, repair_reason, merchant_key`; `ledger_kind` (METRICS.md §1: funding_out/debit/return/internal_payoff/early_payoff_cash/settlement); `is_cash` (false only for internal_payoff); `amount_cents` signed as exported (FUNDING/RETURN < 0) |
| `misposting_repairs` | one per L3 move | `deal_id_posted, deal_id_repaired, reason, detail` |
| `scorecard_links` | one per scorecard | `deal_id` (NULL if unmatched), `merchant_key, method, confidence, day_gap` |
| `collections_links` | one per collections row | `deal_id` (Q4), `method` (`deal_number` / `name_<tier>+open_deal` / `+latest_deal` / `unmatched`), `confidence, event_type` |
| `notes_extractions` | one per (collection_id, extractor) | Both extractors' `new_payment, payment_period, previous_payment, settlement_amount, settlement_installments` (dollars). `used` marks the chosen extractor. |
| `collections_terms` | one per collections row | The chosen extractor's terms in cents, plus `deal_id` (Q4), `event_type`, `amount_cents` (the Amount column), `notes_raw` and lineage. **Part 4 takes modification payments from here** (`new_payment_cents`, `payment_period`). |
| `exceptions` | the review queue | `rule_id`, `entity` (deal/payment/scorecard/collection/merchant), `entity_id, reason, detail`, plus lineage when the entity has one |

- Every link stores `method` and `confidence`. Low-confidence or conflicting links are in `exceptions` with a reason. There are 25 rows now: L1 22 (`l1_successor_tracker_missing_inferred_internal`), Q4 3.
- Manifest stage `resolve`: the tables above (`payments_resolved.repaired` = L3 repairs, `deals_resolved.repaired` = Q5 reconstructions). `extra` holds `data_quality`, `exceptions_by_reason`, `payoff_classes`, `deal_links_by_method`, `l1_tracker_missing_policy` and `notes_extraction`. LLM usage goes into the top-level `llm` block; a cached run shows 258 `cache_hits` and 0 calls.
- Notes extractor: `MCARISK_NOTES_EXTRACTOR=auto|regex|llm`. The default `auto` uses the LLM cache if it covers every note, else regex, and never calls the network. The committed cache covers all 258 distinct notes, so `make all` uses the LLM results. If a later change alters the notes or the model, `auto` falls back to regex until `make notes-llm` is run with a key.
- L1 policy: `MCARISK_L1_TRACKER_MISSING=infer|strict`, or `python -m mcarisk.resolve --l1-policy ...`. Default `infer` (METRICS.md v1.1); `strict` = v1.0, for comparison only.
- `build/run_manifest.json` `metrics_md.version` is now `v1.1`.
- **User decision for Part 4 (2026-11-03): E6 default-date-within-±25-business-days stays report-only.**

### Open questions / deviations
1. **Resolved (user decision, 2026-11-03: adopt METRICS.md v1.1).** L1 when the new deal has no tracker row.
   - **Problem:** 22 PAYOFF rows are true internal payoffs whose successor N is one of the 44 tracker-missing deals. Under v1.0 they could not be classified: N has no tracker link, no `payoff_amt` and no advance or fee. As a result L1 scored 0.9737, below its 0.99 gate.
   - **Fix:** v1.1 adds a fourth L1 condition. If N has no tracker row, the payoff is internal when N is the same resolved merchant and funds within 0-2 business days of t. Method `inferred_tracker_missing`, confidence 0.6, always routed to `exceptions`. A changelog entry was added; nothing else in METRICS.md changed.
   - **Code:** `infer` is the default policy; `strict` keeps v1.0 for comparison.
2. "N funds within 0-2 business days of t" is read as either side of t (`|bd| <= 2`). The true lag is always 0 here, so both readings give the same result.
3. Q3's "0-10 days" is calendar days, with the app date on or before funding. Q4's "open" means funded on or before the log date and not closed before it (definition in `matching.py`).
4. E9 counts a value as correct within `max($1, 0.5%)`, because the notes round amounts (`new pmt 248/day`, `35.7k`). A missing value counts as wrong.
5. Merchant clustering anchors each deal on its most frequent ACH name, plus EIN and in-block fuzzy matching. The DBA alias tables come from names observed on the deals. Q1 does not say what the anchor should be, so this is a design choice; it is documented in `merchants.py`.
6. `plan.md` names the Anthropic SDK for the LLM. Per the user, the LLM is OpenRouter (`openai/gpt-6-luna`) through the `openai` client (Part 1 note 4).

## Part 4: Ledger and metrics library

**Status: built, gate pending, not tagged.** Everything in the Part 4 scope is built and `make lint`
is green. All tests pass except one strict `xfail` (listed below). Two gates fail, and one required test
cannot pass on this data. In all three cases the cause is in METRICS.md or the data, not in the code
(Open questions 1-3). Per CLAUDE.md nothing was loosened. The xfail is written up here and needs the
user's decision.

### What was built
| Path | What it is |
|---|---|
| `src/mcarisk/ledger.py` | Stage `ledger` (`make ledger`). It builds the METRICS.md §1 event table. Every `payments_resolved` row becomes exactly one event: funding_out, debit, return, early_payoff_cash, internal_payoff or settlement. The ACH rows keep their `deal_id` after the L3 repair, plus `deal_id_posted` and `attribution` (`posted`/`l3_repair`). Each deal with a known advance and commission % above 0 gets one `commission_out` = -round_half_up(advance x rate), dated on the funding date, with the tracker row's lineage. Collections rows give `modification` events (amount 0, `new_payment_cents`) and `write_off` events (amount = the log's written-off Balance, positive, non-cash). Each return is paired with the debit it reverses (`matched_event_id` on both rows): same deal, same cents, earlier date, preferring a 2-business-day lag. All 7,636 returns are matched. It also writes `deal_status_events` (the collections `default`, `bankruptcy` and `settlement` rows, the last with its agreed amount), `ledger_rejects` (1 collections row with no Q4 deal, reason `collections_row_unlinked`), `ledger_accounting` and `cal_sched`. |
| `src/mcarisk/metrics/schedule.py` | Scheduled payment days on the Fed ACH calendar, used by D12, D15 and D16. `daily` = every business day after funding. Weekly deals pay on one day a week: the weekday of the first business day after funding. If that day is a holiday, the deal pays on the last business day before it in the same week; if there is none, it skips that week. `cal_sched` (code, date, is_sched, n) is the same calendar as a table for the SQL. |
| `src/mcarisk/metrics/deal.py` | D1-D19 and the §2.1 outcome as pure functions with an `as_of` date (default 2026-09-30). It never uses an event dated after `as_of`, and a debit counts as returned only once its return is dated on or before the evaluation date. Also D16 (default date + recognition date + trigger), D17 (all three triggers, the D14/D13 values at the renewal date, and the payment-ratio severity attribute) and the D17 return basis (`MCARISK_D17_RETURNS=asof|attempt`, see Open question 1). The docstrings cite their IDs. |
| `src/mcarisk/metrics/relationship.py`, `portfolio.py` | R1-R10 and P1-P9. P4 is computed overall and by funding-quarter vintage. P7 gives the cumulative R2/R1 curves by first-funding quarter and month k, censored at the as-of date. P8 gives the distressed share of renewals and the R9 rate of chains with and without a D17 renewal. P9 gives the modification cure rate over all modified deals and over closed modified deals. P-metrics are stored long: `metric_id, metric, segment, k, value, numerator, denominator, unit`. |
| `sql/metrics/01_deal_metrics.sql`, `02_relationship_metrics.sql`, `03_portfolio_metrics.sql` | DuckDB table macros `deal_metrics_sql(as_of, attempt_basis)`, `relationship_metrics_sql(...)` and `portfolio_metrics_sql(...)`. They mirror the Python line by line: cents use integer arithmetic, and D12 rounds half-up as `(2*n*p + k) // 2k`. The stage installs them and creates the views `v_deal_metrics`, `v_relationship_metrics` and `v_portfolio_metrics` at the build's as-of date and D17 basis. |
| `src/mcarisk/metrics/stage.py` | Stage `metrics` (`make metrics`). It persists `deal_metrics` (2,011 rows), `relationship_metrics` (1,200) and `portfolio_metrics` (406), installs the SQL, sets `data_quality` Q6 to evaluated, regenerates `build/dq_report.md` (new sections: ledger accounting, derived outcomes, all table counts) and records the manifest stage `metrics` (`as_of`, `d17_basis`, outcomes, headline P-metrics, Q6 pairs, tracker-missing summary). |
| `evals/suites/e6_definitions.py`, `e7_metrics.py` | E6 and E7 (details below). No threshold was changed. |
| `Makefile` | `ledger` and `metrics` stages are real and wired into `make all`. |
| `tests/test_metrics_unit.py`, `tests/test_metrics_build.py`, `tests/fixtures/m0219_*.csv`, `tests/conftest.py` (`metrics_build`) | See Tests. |

### Tests: 346 passed + 1 strict xfail (`make lint test` green, about 2 min); the 304 from Parts 1-3 still pass
- `test_metrics_unit.py` (25, on hand-built fixtures):
  - **Schedule**: a Juneteenth Friday moves the payment to Thursday; a Labor Day Monday skips the week; schedule codes; commission rounding; return matching prefers the 2-bd lag.
  - **D16**: daily rule fires on the 20th missed scheduled day, and 19 missed days followed by a good payment does not fire. **Weekly deal, 4 missed payments**: 3 is not enough and the 4th fires. **A collections-log default (and a bankruptcy) recognised before the 20-day rule fires**: the recognition date is the log date and the default date is the last good payment + 1 bd. **Balance 0**: a paid-off deal never defaults. No default after an internal payoff.
  - **D17, each trigger alone**: returns (3 of the last 20 fires, 2 does not), pace < 0.85, modification (the payment ratio uses the modified payment). The `asof` vs `attempt` basis on a return that posts after the renewal date.
  - **Outcome precedence**: a write-off event beats a settlement. Settled (within the 1-cent-per-instalment tolerance) beats default and the 180-day write-off. Default + 180 days becomes written_off (and an unfinished settlement does not stop it). Defaulted beats refinanced. Also paid_in_full_early, paid_in_full within $0.01, and active_modified. A deal funded after the as-of date is excluded.
  - **Hypothesis**: shuffling the events and status rows leaves every deal and relationship metric unchanged.
  - Relationship arithmetic; month clamping.
  - **M0219 §7 hand-built fixture**: deals 1102 and 1150 with the §7 contract terms and the pipeline's own ledger events. Every number in the §7 table and the relationship view reproduces: RTR $40,960 / $31,720, factors 1.28 / 1.22, D6 $34,049 / $2,745, D7 $18,022 / $8,700, D8 $22,938 / 0, D10 100% / 27.4%, D11 44.0% / 27.4%, payment ratio 0.44, 175 days, outcomes refinanced_distressed / written_off ($23,020), R1 $36,794, R2 $26,723, **R3 0.73, loss $10,072**.
- `test_metrics_build.py` (19 + 1 xfail, on the real build):
  - **Ledger**: every ACH row is exactly one event (deal, amount, kind, date, lineage); kinds, signs, `is_cash` and lineage; commission_out = tracker commission % x advance, and no commission for tracker-missing deals; collections rows fully accounted; returns matched one to one.
  - **Ledger identity per deal**: RTR - D7 - D8 = balance. It is 0 for all 811 renewals; 0 or 1 cent for paid in full; a discount > 0 and < 15% for early payoffs; exactly the log's written-off balance for written-off deals; >= 0 forgiven for settled deals. Open deals have balance > 0.
  - **Python == SQL** for all 3 tables on the build (exact for cents, ids, dates, flags and labels; doubles to 1e-12 relative), plus as of 2025-06-30 and 2024-03-15 and with the `attempt` basis.
  - **As of**: metrics as of 2025-06-30 are identical when every later event and deal is deleted.
  - **Order invariance**: shuffled real ledger, deals and status rows give the same output.
  - Outcome and flag consistency.
  - **M0219 from the built tables**: every §7 number that raw/ supports is exact (see Open question 2). The commission-dependent numbers are a strict `xfail`.
  - Q6, the DQ report and the manifest. E6 and E7 on the build, and both **detect** corrupted outcomes and amounts.
- `test_evals_runner.py`: the all-suites run now uses the Part 4 build and checks that the exit code follows the gates. The "requested but not implemented" check now uses E8.

### Eval scores (`make eval`, D17 basis `asof` = default): overall **fail** (E6, E7). E1-E5 and E9 pass unchanged.
| Suite | Metric | Value | Gate |
|---|---|---|---|
| E6 | outcome accuracy (§2.1) | **0.9727** (strict, unmapped 0.9597) | >= 0.97: pass |
| E6 | D17 precision / recall vs `renewal_type` | **0.9853 / 0.8238** (201 TP, 204 predicted, 244 true) | P >= 0.95 pass; **R >= 0.85 FAIL** |
| E6 | default date within +/-25 bd (report-only, user decision) | 0.9930 (n=285; median abs error 1 bd, p90 3 bd) | report |
| E6 | same with D17 basis `attempt` | outcome 0.9806, D17 P 0.9864 / **R 0.8893** | (would pass) |
| E7 | deal metrics on correctly linked records (D1-D9, D12) | max error **$0.63** (D12, weekly deals only: the truth rounds rtr/term to the cent, §0 says payment / 5) | <= $1: pass |
| E7 | relationship metrics on linked records (R1, R2, R4, R6, R7, R8) | max error **$0.00** (1,153 relationships) | <= $1: pass |
| E7 | P-metrics vs `portfolio_expected.json` | max **4.93 pp** (D17-dependent rows) | <= 0.5 pp: **FAIL** |
| E7 | end to end (report-only) | deal max $15,400 (D6: the commission on tracker-missing deals, $197,717 in total); R7 max $127,165 (open tracker-missing deals with unknown RTR; 2 R5 status flips) | report |

**E6 confusion matrix** (rows = truth, `active_stressed` mapped to `active`). On the diagonal: written_off 158/158, settled 61/61, paid_in_full 524/524, paid_in_full_early 25/25, renewed_healthy 564/567, refinanced_distressed 201/244, defaulted 64/71, active_modified 37/38, active 322/323. Off the diagonal:
- refinanced_distressed -> renewed_healthy 43 (D17 misses)
- renewed_healthy -> refinanced_distressed 3
- defaulted -> active / active_modified 5: true defaults within 20 bd of the as-of date with no log entry yet, so they are not observable
- defaulted -> written_off 2: D16(b) fired months before the simulator's default (4 consecutive returned weekly payments); the deal paid again later
- active -> defaulted 1: a name-only Q4 link (confidence 0.6) puts another merchant's default on deal 2548; it is in the exceptions queue
- active_modified -> defaulted 1

**E7 populations.**
- Deals: 2,010 of 2,011 are correctly linked: right merchant cluster, right prior link, ledger events equal to the true cash flows, right collections links. The one exception is 2548 (above).
  - Contract metrics (D1-D3, D5, D6, D10, D11) are compared where the tracker supplies them, which excludes the 44 tracker-missing deals.
  - D12 is compared where the truth defines it (open active deals) and the outcome agrees.
- Relationships: 1,153 of 1,200 are in scope; 44 contain a tracker-missing deal, 2 have a different R5, 1 holds deal 2548.
- The truth is adjusted to the as-of date first: `cash_from_merchant` / `balance_asof` include **41 ACH returns dated 2026-10-01/02** ($34,873.81, 32 deals), which METRICS.md §0 excludes. Without that adjustment the linked deal error would be one payment on those 32 deals.

**Headline P-metrics vs expected** (pipeline, as of 2026-09-30, `asof` basis; expected from `portfolio_expected.json`):
| Metric | Pipeline | Expected | Difference |
|---|---|---|---|
| P1 reported / adjusted collection rate | **92.15% / 70.88%** | 92.08% / 70.84% | 0.07 / 0.04 pp |
| P2 masking gap | **21.28 pp** | 21.24 pp | 0.04 pp |
| P3 internal-payoff share | **20.22%** | 20.22% | 0.00 pp |
| P4 bad rate naive / economic (closed) | **13.98% / 26.88%** | 13.87% / 29.32% | 0.11 / **2.44 pp** |
| P5 resolved relationships with loss; cash multiple | **21.43%** (165/770); 1.1449 | 21.22%; 1.1442 | 0.21 pp; 0.07 pp |
| P6 economic exposure / contractual balance (open) | **$11.84M / $24.84M** | $11.95M / $25.24M | -0.94% / -1.56% (report-only, $) |
| P8 distressed-chain loss share | **40.0%** (34/85) | 39.78% | 0.22 pp |
| P8 distressed share of renewals | **25.15%** (204/811) | 30.09% (244/811) | **4.93 pp** |
| P5 capital-weighted loss rate; P9 cure rate | 6.54%; 32.8% all / 48.7% closed | (no expected value) | |

With the `attempt` basis, P4 economic is 27.89% (1.43 pp off) and the distressed share is 27.13% (2.96 pp off). Both still fail. The P6 dollar gaps come from tracker-missing deals: R7 is unknown when the RTR is unknown, and R1 leaves out their unknown commissions.

**Q6 (stale status)** is evaluated: 364 of 1,967 tracker statuses disagree with the §2.1 outcome. The biggest disagreements: paid_off->renewed_healthy 137, active->paid_in_full 93, paid_off->refinanced_distressed 57, active->renewed_healthy 39, active->written_off 17. 194 of 791 renewed deals are typed paid off.

**Runtime**: `make clean all` takes **33.7 s** wall with no network (ingest 4.7, standardize 6.5, resolve 5.2, ledger 4.7, metrics 3.1). The SQL views run in about 0.5 s (deal) and 1 s (portfolio). `make test` takes about 2 min.

### Known gaps
- Tracker-missing deals (44): D1, D3 and D5 are NULL (`commission_known = false`), D6 and R1 are lower bounds, and `relationship_metrics.n_commission_unknown` counts them. RTR is inferred for 31 of them: 20 from internal payoff (exact) and 11 paid in full. The other 13 have no RTR, so D10-D12, R7 and P1 leave them out.
- D16(b) has no cure: once 4 weekly or 20 daily scheduled payments are missed, the deal is in default even if it pays again, and the 180-day write-off follows (deals 2115, 2442). That is what METRICS.md says.
- Settlement completion uses the agreed amount from the log's Amount column, with 1 cent of tolerance per instalment received. The instalment count in the notes is not used.
- `deal_status_events` holds the log's default, bankruptcy and settlement rows. §1 lists only `modification` and `write_off` as ledger events, so these rows are not in `ledger`. Every log row is still accounted for in `ledger_accounting`.
- `make app` and later stages are unchanged.

### Conventions Part 5+ must follow
- **Tables**:
  - `ledger`: one row per event. Use `deal_id` and `is_cash`; `matched_event_id` pairs returns with debits.
  - `deal_status_events`, `deal_metrics`, `relationship_metrics`.
  - `portfolio_metrics`: long format. P7 rows have `k` = months since first funding and `segment` = first-funding quarter; P4 rows have `segment` = funding quarter or `all`.
  - Money is cents (BIGINT). Rates are fractions, except P2, which is in pp (see the `unit` column).
- **Never** recompute a metric outside `mcarisk.metrics.*` or the `*_sql` macros. For another as-of date, call `deal_metrics_sql(DATE 'yyyy-mm-dd', attempt_basis)` (and the R and P macros), or `mcarisk.metrics.stage.compute(con, as_of=..., d17_basis=...)` in Python. Python == SQL is enforced by tests.
- Use the macros for vintage or as-of views. The T1 fixed horizon in Part 5 needs D19 as of funding + 180 bd.
- **Closed** = written_off, settled, refinanced_distressed, renewed_healthy, paid_in_full_early, paid_in_full. `defaulted` is open.
- **Part 6 (leakage)**: the prior-deal features "as of the renewal date" are `d17_d14_value`, `d17_d13_value` and `d17_payment_ratio`. Under the default `asof` basis they use only returns posted by the renewal date. The `attempt` basis looks 2 bd ahead and must not be used for features.
- `MCARISK_D17_RETURNS=asof|attempt` (or `python -m mcarisk.metrics --d17-basis ...`; also `--as-of`). The build records its basis in the manifest (`stages.metrics.extra.d17_basis`), and E6 reports both bases.
- METRICS.md interpretations used here are in the `deal.py` module docstring. Read them before changing a metric:
  - scheduled payment days (`schedule.py`)
  - good payment
  - outcome dates and the D12 close date
  - settlement completion
  - the D16 balance tolerance ($0.01, as for paid_in_full; it changes 0 deals here)
  - the tracker-missing RTR inference

### Open questions / deviations (need the user)
1. **D17 recall gate (E6) fails under the literal reading of "as of the renewal date"** (0.824 < 0.85).
   - **Ambiguity:** "the prior deal, as of the renewal date, satisfies D14 >= 3/20 ...". Literally, a return that posts after the renewal date is not known on it. That is the implemented default `asof`. Distressed deals are usually refinanced 5-25 days into stress, so the returns of the last few debits (posted 2 bd later) fall after the renewal date.
   - **Alternative:** count a debit attempted on or before the renewal date as returned if its return arrives later (`attempt`). That gives P 0.986 / R 0.889, and outcome accuracy rises to 0.981. METRICS.md's own "measured" P 0.98 / R 0.87 is close to this reading, so the spec author probably measured it this way.
   - **Cost of `attempt`:** a 2-bd look-ahead. Part 6 must then not reuse these values as features.
   - **Also unspecified:** "D14 >= 3/20 (3 or more returns ...)" when there are fewer than 20 attempts. The formula (a ratio) is implemented.
   - **Proposed fix:** METRICS.md v1.2 adopts the `attempt` basis for D17 only (one line in §2, plus a changelog entry), then set the default to `attempt` and re-run. The alternative is to keep `asof` and ask whether the 0.85 gate still applies.
2. **M0219 cannot be reproduced exactly from the built tables. Deal 1102 is one of the 44 tracker-missing deals** (`anomalies.csv`: deal_missing_from_tracker). Its advance ($32,000), factor (1.28), fee and commission ($3,424) appear nowhere in raw/.
   - **What does reproduce exactly from the pipeline:** D4 $30,625, D7 $18,022, D8 $22,938, D9/RTR $40,960 (inferred from the ledger identity), D10 100%, D11 44.0%, payment $409.60, payment ratio 0.44, refinanced_distressed. Every number for deal 1150. R2 $26,723. The relationship is bad and resolved.
   - **What does not:** D6 $34,049, R1 $36,794, R3 0.73 and loss $10,072 come out as **$30,625, $33,370, 0.80 and $6,648**, because the commission is unknown and flagged. The test for these numbers is a strict `xfail`. The hand-built fixture (§7 terms supplied) reproduces all of §7 exactly.
   - **Proposed fix**, pick one:
     - (a) Add a note to §7 that 1102 has no tracker row, so the pipeline shows a lower bound until its terms are recovered.
     - (b) Use a fully tracked relationship as the worked example.
     - (c) Supply 1102's terms as a documented manual override file (`mappings/overrides.yaml`), which the business would do in practice.
3. **E7 portfolio gate fails on the two D17-dependent P-metrics:** P4 economic bad rate 26.88% vs 29.32% (2.44 pp) and the P8 distressed share 25.15% vs 30.09% (4.93 pp). Every other rate is within 0.22 pp.
   - **Cause:** `portfolio_expected.json` counts the simulator's true distressed refinances (244). §2.1 counts D17-flagged ones, and D17 v1 has recall < 1 by design (METRICS.md itself says 0.87). The expected D19 rate can only be reached if D17 is perfect.
   - Even with the `attempt` basis the gaps are 1.43 / 2.96 pp. **No change to the code can close this without changing D17.**
   - **Proposed fix:** keep these two rows report-only in E7, with this reason. The alternative is an E7 expected value computed by applying §2.1 (with the spec's D17) to the true ledger. Changing the gate needs your approval.
4. E6 maps the truth label `active_stressed` (27 deals; the simulator's latent "stressed" state) to `active`, because §2.1 has no such outcome. Strict accuracy is reported too (0.9597; 0.9677 with `attempt`).
5. E7 compares the truth after removing the 41 returns dated after 2026-09-30 (METRICS.md §0). The adjustment is reported in the E7 output.
6. E7 "$1 per record" is applied to the dollar metrics. Rates are reported per record (exact on linked records). P6 dollar totals are compared as relative error (report-only), because a pp gate does not apply to dollars.

## Part 5: Portfolio analytics
_not started_

## Part 6: Risk modeling
_not started_

## Part 7: App, report, reproducibility, demo
_not started_
