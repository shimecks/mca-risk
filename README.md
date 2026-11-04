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

**Headline (synthetic book, as of 2026-09-30):**
- Reported collection rate is **92.1%**; the cash-based rate is **70.8%**.
- **20%** of "collections" are internal renewal payoffs.
- The bad-deal rate is **13.9%** counted the usual way and **29.3%** once distressed refinances are included.

Everything here is fictional: every merchant, ISO and number.

## Quickstart
```sh
uv sync                 # Python 3.12 env from uv.lock
make all                # pipeline -> build/portfolio.duckdb, build/run_manifest.json, build/dq_report.md
make lint test          # ruff + pytest
make eval SUITE=E2      # eval suites vs evals/thresholds.json -> evals/reports/
```
Build status per part, conventions and open questions: `PROGRESS.md`.
