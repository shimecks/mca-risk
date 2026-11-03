# CLAUDE.md: working rules for this repo

Read `plan.md` and `METRICS.md` first. Build **one part at a time**, in order (Part 1 → 7).

## Gate (must pass before starting the next part)
1. `make lint test` is green.
2. The eval suites listed for the part meet `evals/thresholds.json`.
3. `PROGRESS.md` is updated: what was built, test counts, eval scores, known gaps.
4. Commit with the message `part-N: <summary>` and tag `part-N`.

Never loosen a threshold or skip a test to get past a gate without writing the reason in `PROGRESS.md` and asking the user.

## Non-negotiables
- Pipeline code (`src/`, `app/`, `sql/`) must **never** read `ground_truth/`. Only `evals/` may. A static test enforces this.
- `METRICS.md` is the spec. Each metric function cites its ID (D7, R3…). If code and spec disagree, stop and ask. Never silently change a definition.
- Money is stored as integer cents. Dates are `datetime.date`. Business days use the Fed ACH calendar in `src/mcarisk/calendar.py`.
- Every derived row keeps its lineage (`src_file, src_sheet, src_row`). Every link stores `method` and `confidence`.
- Raw rows are never dropped silently: each one is loaded, rejected with a reason, or repaired with a log entry.
- Model features may only use data timestamped on or before the funding date (leakage tests enforce this).
- Don't edit `raw/` or `ground_truth/`. Regenerate with `python3 scripts/generate_portfolio.py --out . --seed 11` (deterministic).

## Commands (create these in Part 1)
- `make all`: the full pipeline into `build/`
- `make test`, `make lint`, `make eval [SUITE=E5]`
- `make app`: run the Streamlit app locally
