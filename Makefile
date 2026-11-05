# MCA risk pipeline. Recipes are POSIX sh and call every tool through `uv run`, so they work on Linux CI
# and on Windows (GNU make + Git Bash sh). `make all` rebuilds build/ from raw/.
#
#   make all            full pipeline from raw/ (python -m mcarisk.pipeline; all stages below, < 2 min)
#   make ingest         Part 1: raw/ -> build/portfolio.duckdb raw_* tables + build/run_manifest.json
#   make standardize    Part 2: raw_* -> *_std tables, data_quality, build/dq_report.md (mappings/*.yaml)
#   make resolve        Part 3: merchants, renewal links, L1 payoffs, L3 repairs, Q3/Q4 links, exceptions
#   make notes-llm      Part 3 (optional): fill cache/llm_notes.jsonl via OpenRouter
#   make ledger         Part 4: ledger, deal_status_events, ledger_rejects, cal_sched (METRICS.md §1)
#   make metrics        Part 4: deal/relationship/portfolio metrics, T1 targets, SQL views, Q6, dq report
#   make analytics      Part 5: figures + build/findings.md (every number from a metric-table query)
#   make model          Part 6: risk models, predictions, model figures, build/model_card.md
#   make test | lint    pytest | ruff check + ruff format --check
#   make eval           all eval suites (unimplemented suites are reported, not failed)
#   make eval SUITE=E1  one suite (fails if that suite is not implemented yet)
#   make report         Part 7: build/report.md (index of findings, model card, DQ report, eval summary)
#   make app-data       Part 7: refresh app/data/ (the app's committed DB + figures snapshot) from build/
#   make app            Streamlit app (Part 7) on app/data/
#   make clean          remove build/

UV      ?= uv
PY      := $(UV) run python
BUILD   := build
DB      := $(BUILD)/portfolio.duckdb
SUITE   ?=


.PHONY: notes-llm
.PHONY: all ingest standardize resolve ledger metrics analytics model report \
        test lint format eval app clean help

# One process runs every stage (mcarisk.pipeline), from an empty build/portfolio.duckdb.
all:
	$(PY) -m mcarisk.pipeline

ingest:
	$(PY) -m mcarisk.ingest

# ---- later stages: explicit stubs, replaced part by part (never fail silently) ----
standardize:
	$(PY) -m mcarisk.standardize
resolve:
	$(PY) -m mcarisk.resolve

# Optional: send collections notes missing from cache/llm_notes.jsonl to the LLM (needs OPENROUTER_API_KEY
# + MODEL in the env or in $$MCARISK_DOTENV). `make all` never calls the network; it reads the cache.
notes-llm:
	$(PY) -m mcarisk.notes.extract --llm
# Part 4: the METRICS.md §1 cash ledger, then D/R/P metrics + SQL views (sql/metrics/*.sql).
# D17 return basis: MCARISK_D17_RETURNS=attempt (default, METRICS.md v1.2) | asof (v1.1 literal reading).
ledger:
	$(PY) -m mcarisk.ledger
metrics:
	$(PY) -m mcarisk.metrics
# Part 5: figures (build/figures/*.png, *.plotly.json, data/*.csv) + build/findings.md from the metric tables.
analytics:
	$(PY) -m mcarisk.analytics
# Part 6: T1 models (baseline, logistic, GBM + isotonic, naive-target logistic), T2 KM/Cox, T3, figures,
# predictions in DuckDB and build/model_card.md.
model:
	$(PY) -m mcarisk.model
# Part 7: build/report.md (links findings, model card, DQ report, latest eval summary).
report:
	$(PY) -m mcarisk.report
# Part 7: refresh the app's committed snapshot app/data/ from build/ (+ evals/reports/latest.json).
app-data:
	$(PY) -m mcarisk.appdata

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

eval:
	$(PY) -m evals.run $(if $(SUITE),--suite $(SUITE),)

app:
	$(UV) run streamlit run app/Home.py

clean:
	rm -rf $(BUILD)

help:
	@sed -n '1,20p' Makefile
