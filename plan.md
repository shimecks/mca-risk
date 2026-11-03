# MCA Portfolio Risk Analytics: Build Plan

> This plan is for round 2 at an MCA funder. Their problem: risk metrics in the industry aren't reliable, because renewals and refinances make receivables look better than their cash economics. Their data lives in spreadsheets. They want **a set of definitions first**, and I would be the **first analytical hire**.
> This project **replaces** the earlier underwriting-copilot plan. It is built in **7 gated parts** by Claude Code. No part starts until the previous part's gate passes.

---

## 0. The pitch (what the walkthrough proves)

1. **Definitions.** `METRICS.md` defines collections, exposure, default and distressed refinance precisely, at the deal and relationship level, measured in cash.
2. **Messy spreadsheets in, a trustworthy dataset out.** It is reproducible, every row has lineage, and every raw row is accounted for.
3. **The finding.** On this book, the standard reported collection rate is **92.1%**, but the cash-based rate is **70.8%**. One in five "collected" dollars is the funder paying itself off with a renewal. The bad-deal rate doubles (**13.9% → 29.3%**) once distressed refinances are counted.
4. **A risk model that respects those definitions:** a fixed-horizon target, a temporal validation split, calibration, and honest limitations.
5. **Everything is evaluated against ground truth.** The data is synthetic with a known data-generating process, so every step has a measurable accuracy.

Not in scope: production deployment at the funder, real data, pricing optimization.

---

## 1. Decisions locked in

| Topic | Decision | Why |
|---|---|---|
| Language | **Python 3.12**, managed with `uv` | Matches the work (spreadsheets, stats, models) |
| Storage / SQL | **DuckDB** (a single `build/portfolio.duckdb` file) plus SQL views for every metric | Shows SQL skills; fast; no server |
| DataFrames | pandas, with `pandera` schemas at every stage boundary | Typed contracts make evals possible |
| Matching | `rapidfuzz` | Entity resolution |
| Stats / ML | `statsmodels` (logit, with interpretable coefficients), `scikit-learn` (gradient boosting, calibration), `lifelines` (Kaplan–Meier, Cox) | Explainable first, flexible second |
| Tests | `pytest` + `hypothesis` | Property tests for parsers and the ledger |
| Lint | `ruff` | |
| UI | **Streamlit**, deployed to Streamlit Community Cloud (free) | Fast to build an analytics UI. Vercel isn't needed. |
| Optional LLM | Anthropic SDK, used only for free-text notes extraction, compared against a regex baseline | Keeps an AI component that is honest and evaluated |
| Orchestration | `make all`: ingest → standardize → resolve → ledger → metrics → analytics → model → report | One command reproduces everything |

---

## 2. The data (already generated)

`scripts/generate_portfolio.py --seed 11 --merchants 1200` is deterministic. It produces **1,200 merchants**, **2,011 deals** (1,200 new, 567 healthy renewals, 244 distressed refinances), about **150k ACH events**, $140M+ advanced across 2023–2026, as of **2026-09-30**.

### `raw/`: what the funder actually has
| File | Messiness |
|---|---|
| `deal_tracker_2023.xlsx` | Junk title row above the header. Money stored as text (`$38,500.00`). Dates as `MM/DD/YY`. Weekly deals show `"$x wkly"` and `"24 wks"`. Renewal links appear **only in free-text Notes**. |
| `deal_tracker_2024.xlsx` | Different column names. Factor stored as `1.35x`. Commission as a fraction. `Prior Deal` blank 25% of the time. |
| `deal_tracker_2025.xlsx`, `deal_tracker_2026_YTD.xlsx` | snake_case schema. Underwriting features inline (about 6% blank). EIN about 70% populated. `renewal_of` blank 15% of the time. |
| `underwriting_scorecards_2023_2024.xlsx` | Features for 2023–24 deals, keyed by **merchant name and application date only**. Industry is free text ("restaurant/bar", "Food Service"). About 6% missing. |
| `ach_exports/ach_YYYY_MM.csv` (46 files) | Each overlaps the previous month by 3 days. The **date format changes in July 2024**. `PAYOFF` is used for both cash payoffs and renewal payoffs. 250 payments are posted to the prior deal. |
| `collections_log.xlsx` | Defaults, bankruptcies, reconciliations and modifications, settlements, write-offs. Deal number missing about 25% of the time. Free-text notes hold the new payment amounts and settlement terms. |

Names vary across files: upper case, suffix dropped, `L.L.C.`, the DBA used instead of the legal name, one-character typos. Tracker status is stale or inconsistent (`PIF`, `Paid Off - Renewal`, `Refi`, `Charged Off`…). **About 40% of renewed deals are typed as "Paid Off."**

### `ground_truth/`: eval only, never read by pipeline code
`merchants.csv` (including the latent quality *z*), `deals.csv` (true links, types, outcomes and every D-metric), `cashflows.csv` (the true ledger), `relationships.csv` (every R-metric), `anomalies.csv` (every injected defect), `portfolio_expected.json` (P-metrics), `dgp.json` (the true hazard coefficients).

Reference points for the evals:
- A logistic model using only origination features reaches a **temporal-test AUC of about 0.76**.
- The *true* hazard ranks at about **0.79**. That is the ceiling.
- Deduplicated ACH rows reproduce the true ledger exactly: same count, same sum.

---

## 3. Architecture

```
raw/ ──► ingest ──► standardize ──► resolve ──► ledger ──► metrics ──► analytics ──► model ──► report / app
          │            │              │           │          │            │            │
       raw_* tables  *_std tables   merchants   ledger     deal_m,      figures,     model card,
       + lineage     + DQ report    deal_links  events     rel_m,       findings     predictions
                                    exceptions             portfolio_m
                          ▲                                    ▲
                   mappings/*.yaml                         METRICS.md (spec) ─ sql/metrics/*.sql
evals/ ── compares every stage to ground_truth/ ── evals/reports/<ts>.{json,md}
```

**Lineage (the observability layer for this project):**
- Every derived row carries `src_file, src_sheet, src_row` (or a list of them).
- Each run writes `build/run_manifest.json`, containing:
  - the git SHA and the `METRICS.md` version;
  - input file hashes;
  - row counts at every stage, with accepted / rejected / repaired counts and reasons;
  - timings;
  - and, if the LLM is used, its tokens and cost.
- A `data_quality` table lists every rule (Q1–Q7, L1–L3) with its count of hits, plus sample rows.
- Every link (merchant merge, renewal link, payoff classification) stores a `method` (explicit | regex | inferred | fuzzy | llm) and a `confidence`.
- An `exceptions` table holds everything the pipeline couldn't resolve automatically. This is the human-review queue.

---

## 4. Evals plan

**Runner:** `make eval` writes `evals/reports/<ts>.json` and `.md`, and the app shows the latest report. Gates live in `evals/thresholds.json`. A failed gate fails `make eval` and CI.

| Suite | Measures | Gate |
|---|---|---|
| **E1 Ingestion** | Every raw row is loaded or rejected with a reason; per-file counts reconcile | 100% accounted |
| **E2 Standardization** | Parsed advance / RTR / payment / dates / factor / term equal the truth for each deal | ≥ 99.5% field accuracy |
| **E3 Dedupe** | `payments_std` count and sum equal the true ledger (excluding commissions) | exact |
| **E4 Entity resolution** | Pairwise precision / recall / F1 of merchant clusters vs. `merchant_id`; deal → merchant accuracy | F1 ≥ 0.98 |
| **E5 Linkage** | Renewal links P/R vs. `prior_deal_id`; payoff internal-vs-cash accuracy (L1); misposting repair recall (L3); scorecard match rate | links P ≥ 0.98, R ≥ 0.97; L1 ≥ 0.99; L3 R ≥ 0.95 |
| **E6 Definitions** | Outcome accuracy (§2.1); default date within ±25 business days; D17 P/R vs. `renewal_type` | outcome ≥ 0.97; D17 P ≥ 0.95, R ≥ 0.85 |
| **E7 Metrics** | Deal and relationship metrics vs. truth (on correctly linked records); P-metrics vs. `portfolio_expected.json` | $1 per record; ±0.5 pp portfolio |
| **E8 Model** | Temporal-test AUC, Brier score, calibration slope, decile lift; coefficient signs vs. `dgp.json`; leakage tests | AUC ≥ 0.70 (ceiling ≈ 0.79); slope 0.8–1.2; main-driver signs correct |
| **E9 Notes (optional LLM)** | Modification amount and settlement amount extraction vs. truth: regex vs. LLM | report both; LLM ≥ regex |

**Error propagation:** E7 is also reported *end to end*, meaning on all records and not only correctly linked ones. That shows how much a linkage error moves the headline numbers. It is a strong talking point for the interview.

---

## 5. The 7 parts

Each part has the same **gate**: `make lint test` passes, the part's eval suites meet their thresholds, `PROGRESS.md` is updated, and a commit is tagged `part-N`.

### Part 1: Repo, ingestion, lineage
**Build**
- `uv` project with a `src/mcarisk/` package, ruff, pytest, a Makefile, and GitHub Actions.
- `ingest.py` loads every file in `raw/` into DuckDB `raw_*` tables **as text**, with `src_file, src_sheet, src_row`.
- Header-row detection (the title row in the 2023 tracker).
- One table per source family. ACH exports are unioned.
- `run_manifest.json` writer.

**Tests:** header detection; row counts per file equal the file contents; lineage columns are populated; a static test asserts that `src/` never references `ground_truth`.

**Evals:** E1.

### Part 2: Standardization and data-quality report
**Build**
- `mappings/tracker_2023.yaml`, `tracker_2024.yaml`, `tracker_2025.yaml` map raw columns to canonical fields.
- Parsers for:
  - money (`$`, commas, text);
  - percent vs. fraction;
  - factor (`1.35x`);
  - dates (MM/DD/YY, MM/DD/YYYY, ISO, Excel serial);
  - frequency (`D`/`Daily`/`wkly`);
  - term (`24 wks` → 120 business days);
  - industry free text → canonical (a lookup table).
- Canonical tables: `deals_std`, `payments_std` (with L2 dedupe after date normalization), `scorecards_std`, `collections_std`, each with a pandera schema.
- A `data_quality` table and `build/dq_report.md`.

**Tests:** hypothesis property tests (money round-trips and never parses negatives silently; the date parser handles every format); a dedupe test that specifically covers the July 2024 format switch; schema failures raise.

**Evals:** E2, E3.

### Part 3: Entity resolution and linkage
**Build**
- `resolve/merchants.py` (Q1): normalization, blocking, EIN, fuzzy matching, DBA aliases. Each cluster gets a stable `merchant_key` and records the method and score of every merge.
- `resolve/renewals.py` (Q2): explicit field, then notes regex, then L1 inference.
- `resolve/payoffs.py` (L1): classifies internal vs. cash payoffs.
- `resolve/misposting.py` (L3).
- Scorecard ↔ deal (Q3) and collections ↔ deal (Q4).
- Tracker-missing deal reconstruction from ACH (Q5).
- `exceptions` table for low-confidence or conflicting links.
- **Optional:** `notes/extract.py` with a regex baseline, plus an LLM extractor (structured output validated by pydantic, cached by input hash, with tokens and cost logged to the manifest) for modification amounts and settlement terms.

**Tests:** unit tests for the name normalizer on every variant pattern; L1 edge cases (a cash payoff the same week as an unrelated new deal; a payoff with no `payoff_amt`); misposting repair; the LLM output schema and cache.

**Evals:** E4, E5, E9.

### Part 4: Ledger and the metrics library
**Build**
- `ledger.py` builds the event table following §1 of METRICS.md, including `commission_out` from the tracker.
- `metrics/deal.py`, `metrics/relationship.py`, `metrics/portfolio.py`: pure functions that are **mirrored in `sql/metrics/*.sql` views**, with a test asserting that Python and SQL agree.
- Outcome derivation (§2.1), D16 default, D17 distressed refinance.
- Every metric function's docstring cites its METRICS.md ID.

**Tests:**
- Ledger identity per deal: RTR = net debits + internal payoff + balance (+ discount / forgiven for early payoffs and settlements).
- The worked example M0219 reproduces exactly.
- Hypothesis tests for order invariance.
- Python == SQL.

**Evals:** E6, E7.

### Part 5: Portfolio analytics (the findings)
**Build** `analytics/` producing figures and `build/findings.md`:
1. **Renewal masking:** reported vs. adjusted collection rate, overall and by vintage (P1–P3).
2. **Bad rate, naive vs. economic,** by vintage quarter (P4). Shows the 2024-Q3 → 2025-Q1 vintage deterioration built into the data: about 37–43% economic bad, vs. about 21–30% for the surrounding vintages. **Use the T1 fixed-horizon view and mark vintages younger than the horizon as immature.** Bad rates computed on closed deals only are biased for recent vintages: 2026-Q3 shows 61% because only fast failures have closed yet.
3. **Exposure:** economic vs. contractual (P6).
4. **Vintage curves** of the cumulative cash multiple (P7).
5. **Refinance quality:** loss rate of chains with a distressed refinance vs. chains without (P8); modification cure rate (P9).
6. **Segments:** industry, ISO, number of positions, burden buckets. Bad rates come with Wilson confidence intervals.

Each figure cites the metric IDs it uses.

**Tests:** each figure function runs on a tiny fixture; findings numbers equal the metric-table queries.

**Evals:** E7 at the portfolio level, ±0.5 pp.

### Part 6: Risk modeling
**Build**
- Targets T1 (primary), T2 (survival) and T3 (relationship), defined in METRICS.md §5.
- `features.py` uses origination-only features plus as-of-renewal prior-deal features, with a **leakage guard**: features are computed from data with timestamps ≤ the funding date. A test perturbs post-funding data and asserts that the features don't change.
- **Temporal split:** train on deals funded ≤ 2024-12, validate on 2025-01 to 2025-06, test on 2025-07 to 2026-03 (the T1 horizon must be observable).
- **Models:**
  - a baseline (a rule score built from burden and positions);
  - a logistic regression (statsmodels, with coefficient table and odds ratios);
  - gradient boosting (sklearn), with calibration (isotonic, on validation).
- Kaplan–Meier by segment and a Cox model for T2.
- **Outputs:** AUC, Brier, a calibration plot, a decile lift table, and the same model trained on the **naive** target, to show how it under-ranks distressed-refinance risk.
- `build/model_card.md` covers intended use, data, performance, limits, and fairness considerations (state and industry only; no protected attributes).

**Tests:** leakage guard; split boundaries; deterministic seeds; the calibration function.

**Evals:** E8.

### Part 7: App, report, reproducibility, demo
**Build** a Streamlit app (`app/`) with these pages:
- **Portfolio:** headline cards, masking chart, vintage curves.
- **Merchant drill-down:** a chain timeline showing fundings, payments, returns, payoffs (internal vs. cash), modifications and default, with D- and R-metrics, and a lineage popover showing source file and row.
- **Definitions:** METRICS.md rendered, with links from every number to its definition.
- **Data quality:** rule hits, the exceptions queue, the run manifest.
- **Model:** model card, calibration, lift, segment survival curves.
- **Evals:** the latest report against its thresholds.

Also:
- `make all` runs the whole pipeline from `raw/` in under 2 minutes.
- Deploy to Streamlit Community Cloud with a prebuilt `portfolio.duckdb` committed.
- `DEMO.md` holds the walkthrough script.

**Tests:** app smoke test (`streamlit.testing.v1.AppTest`) on every page; `make all` from a clean checkout in CI.

**Evals:** the full suite, green.

---

## 6. Walkthrough script (about 15 minutes)
1. **The problem in their words (1 min):** "A receivable isn't its face value." Show the M0219 chain: one deal "paid off," the other written off, and the relationship lost $10k.
2. **Definitions (3 min):** METRICS.md. Cash vs. non-cash events, the L1 payoff rule, D17 and its measured precision and recall, deal vs. relationship.
3. **The data work (3 min):** the data-quality page. 250 mispostings, the date-format dedupe trap, renewal links hidden in notes, stale status. Everything is accounted for and traceable.
4. **Findings (4 min):** the 21-point masking gap, naive vs. economic bad rate, exposure of $12.0M economic vs. $25.2M contractual, the vintage deterioration, distressed-refinance chains losing at about twice the rate.
5. **Model (3 min):** target choice matters more than the algorithm. Temporal AUC against the ceiling, calibration, what I would *not* use it for yet.
6. **First 90 days at their company (1 min):** inventory their spreadsheets, agree the definitions with stakeholders, rebuild history, publish a monthly portfolio pack, then start modeling.

---

## 7. Schedule (round 2 date unknown; this assumes about 5 working days)
| Day | Parts |
|---|---|
| 1 | 1, 2 |
| 2 | 3 |
| 3 | 4, 5 |
| 4 | 6 |
| 5 | 7 + rehearsal |

**If short on time, cut in this order:** the optional LLM notes extraction → T3 → the Cox model → app pages other than Portfolio and Drill-down (use the notebook and `findings.md` instead).
**Never cut:** METRICS.md, E3–E7, the masking finding, the M0219 drill-down.

## 8. Risks
| Risk | Mitigation |
|---|---|
| The interviewer asks "is this realistic?" | Name the synthetic assumptions openly (`dgp.json`). The point is the method and the definitions, not the numbers. |
| Entity resolution errors distort the metrics | The end-to-end E7 quantifies the impact; the exceptions queue exists for human review |
| The model is over-claimed | Report the ceiling AUC; fixed-horizon target; temporal split; calibration; a clear model card |
| Their real data is different | Column mappings are YAML; the parsers and definitions are source-agnostic; this is exactly what a first-90-days plan addresses |
