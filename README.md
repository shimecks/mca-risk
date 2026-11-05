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

**Headline (synthetic book, as of 2026-09-30; the pipeline's own numbers, METRICS.md v1.3, from `build/findings.md` and `build/model_card.md`):**
- Reported collection rate is **92.2%**; the cash-based rate is **70.9%**: a **21.3-point** renewal masking gap (P1, P2).
- **20.2%** of "collections" are internal renewal payoffs (P3).
- The bad-deal rate is **14.0%** counted the usual way and **27.9%** once distressed refinances are included (P4, closed deals). On the fixed 180-business-day horizon (T1) it is 16.7% vs 29.1%, and the 2024-Q3 to 2025-Q1 vintages are the worst (34-41% vs 19-25% around them).
- Open economic exposure is **$11.84M** against a contractual balance of **$24.84M** (P6).
- Resolved chains with a distressed refinance end in loss **40.4%** of the time vs 18.9% without one (P8).
- The T1 risk model (logistic, origination-only features) reaches a temporal-test AUC of **0.660** against an observable ceiling of 0.681 (true hazard 0.726); calibration slope 0.96 after validation-fitted recalibration.
- Why these differ from the simulator's labels (e.g. 29.3% economic bad in METRICS.md §4): D17 is a rule on observable ledger stress with recall of about 0.89 against the simulator's hidden distressed labels, so the pipeline's economic bad rate is a lower bound on them; tracker-missing deals also make R1 a lower bound.

Everything here is fictional: every merchant, ISO and number.

## Quickstart
```sh
uv sync                 # Python 3.12 env from uv.lock
make all                # full pipeline from raw/ (< 2 min, no network) -> build/: portfolio.duckdb, run_manifest.json,
                        #   dq_report.md, findings.md, model_card.md, figures/, report.md
make lint test          # ruff + pytest (incl. Streamlit AppTest smoke tests on every app page)
make eval               # eval suites E1-E9 vs evals/thresholds.json -> evals/reports/latest.{json,md}
make app-data           # refresh the app's committed snapshot app/data/ from build/ (+ the latest eval report)
make app                # Streamlit app on app/data/ (http://localhost:8501)
```
- `build/report.md` links the findings, model card, data-quality report and eval summary.
- `DEMO.md` is the 15-minute walkthrough script, with the numbers and which app page to show.
- Build status per part, conventions, user decisions and open questions: `PROGRESS.md`.

## App
Five pages, each with one job, written for executives first (plain-English labels; metric IDs only in the `?`
tooltips; grey = what the tracker reports, blue = what the cash says, red = losses):
- **Home (Overview)** (`app/Home.py`): the whole story on one screen: a one-line takeaway, five cards
  comparing the tracker's view with the cash view (collection rate, bad-deal rate, money at risk, renewal
  "collections", distressed-refi chains) and one chart (reported vs cash collection rate by quarter).
- **Portfolio trends**: which funding quarters went bad (young quarters greyed), money at risk, refinance
  quality and a segment breakdown.
- **Merchant story**: one merchant dollar by dollar (default: the METRICS.md §7 example M0219, Indigo Spa
  Corp): cash out / back / net, a one-line verdict, a timeline and a per-deal summary; full metrics and
  source-row lineage in expanders. `?deal=1150` or `?merchant=<key>` jump to a merchant.
- **Risk model**: how well it ranks, whether its probabilities can be trusted, top-10% lift, what not to use
  it for, then charts; the full model card in an expander.
- **Behind the numbers**: tabs for data quality, tests (the eval report), definitions (`?id=D17` opens one)
  and run details.

The app opens `app/data/portfolio.duckdb`
read-only (a 27-table export of `build/portfolio.duckdb`, about 6 MB) and the figure JSON next to it. It
reads `METRICS.md` and `evals/thresholds.json` from the repo, displays only metric-table values, never
calls the network and needs no API key. Set `MCARISK_APP_DATA` to point it at another snapshot.

## Deploy (Streamlit Community Cloud)
The snapshot in `app/data/` is committed, so the deployed app needs no pipeline run.
1. Push the repository to GitHub (with `app/data/` committed; refresh it first with `make all eval app-data`).
2. On [share.streamlit.io](https://share.streamlit.io), choose **Create app**, pick the repo and branch, and set
   **Main file path** to `app/Home.py`.
3. Under **Advanced settings**, choose **Python 3.12**. No secrets are needed.
4. Deploy. Community Cloud installs `app/requirements.txt` (the dependency file next to the entrypoint takes
   precedence over the repo-root `uv.lock`, so only streamlit, duckdb, pandas, numpy, pyarrow and plotly are
   installed). The theme comes from `.streamlit/config.toml`.

To update the deployment, rerun `make all eval app-data`, commit `app/data/` and push.
