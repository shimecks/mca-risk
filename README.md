# MCA Portfolio Risk Analytics (synthetic)

This project measures MCA portfolio risk **in cash, at the merchant-relationship level**, so renewals and refinances can't hide losses. It starts from the messy spreadsheets a funder actually keeps.

- `METRICS.md`: the metric definitions (the spec)
- `plan.md`: the 7-part build plan for Claude Code
- `CLAUDE.md`: build rules and gates
- `raw/`: synthetic messy inputs (trackers, scorecards, ACH exports, collections log)
- `mappings/`: raw column -> canonical field mappings and lookup tables (YAML)
- `ground_truth/`: the truth tables, **for evals only**
- `scripts/generate_portfolio.py`: regenerates everything (seed 11, deterministic)
- `scripts/verify_portfolio.py`: independent consistency checks

**Headline (synthetic book, as of 2026-09-30; the pipeline's own numbers, METRICS.md v1.2, from `build/findings.md`):**
- Reported collection rate is **92.2%**; the cash-based rate is **70.9%**: a **21.3-point** renewal masking gap (P1, P2).
- **20.2%** of "collections" are internal renewal payoffs (P3).
- The bad-deal rate is **14.0%** counted the usual way and **27.9%** once distressed refinances are included (P4, closed deals). On the fixed 180-business-day horizon (T1) it is 16.7% vs 29.1%, and the 2024-Q3 to 2025-Q1 vintages are the worst (34-41% vs 19-25% around them).
- Open economic exposure is **$11.84M** against a contractual balance of **$24.84M** (P6).
- Resolved chains with a distressed refinance end in loss **40.4%** of the time vs 18.9% without one (P8).
- Why these differ from the simulator's labels (e.g. 29.3% economic bad in METRICS.md §4): D17 is a rule on observable ledger stress with recall of about 0.89 against the simulator's hidden distressed labels, so the pipeline's economic bad rate is a lower bound on them; tracker-missing deals also make R1 a lower bound.

Everything here is fictional: every merchant, ISO and number.

## Quickstart
```sh
uv sync                 # Python 3.12 env from uv.lock
make all                # pipeline -> build/portfolio.duckdb, run_manifest.json, dq_report.md, findings.md, figures/
make lint test          # ruff + pytest
make eval SUITE=E2      # eval suites vs evals/thresholds.json -> evals/reports/
```
Build status per part, conventions and open questions: `PROGRESS.md`.
