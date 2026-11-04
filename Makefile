# MCA risk pipeline. Recipes are POSIX sh and call every tool through `uv run`, so they work on Linux CI
# and on Windows (GNU make + Git Bash sh). `make all` rebuilds build/ from raw/.
#
#   make all            full pipeline (stages not built yet print "not implemented until Part N")
#   make ingest         Part 1: raw/ -> build/portfolio.duckdb raw_* tables + build/run_manifest.json
#   make standardize    Part 2: raw_* -> *_std tables, data_quality, build/dq_report.md (mappings/*.yaml)
#   make resolve        Part 3: merchants, renewal links, L1 payoffs, L3 repairs, Q3/Q4 links, exceptions
#   make notes-llm      Part 3 (optional): fill cache/llm_notes.jsonl via OpenRouter
#   make ledger         Part 4: ledger, deal_status_events, ledger_rejects, cal_sched (METRICS.md §1)
#   make metrics        Part 4: deal/relationship/portfolio metrics, SQL views, Q6, dq report
#   make test | lint    pytest | ruff check + ruff format --check
#   make eval           all eval suites (unimplemented suites are reported, not failed)
#   make eval SUITE=E1  one suite (fails if that suite is not implemented yet)
#   make app            Streamlit app (Part 7)
#   make clean          remove build/

UV      ?= uv
PY      := $(UV) run python
BUILD   := build
DB      := $(BUILD)/portfolio.duckdb
SUITE   ?=


.PHONY: notes-llm
.PHONY: all ingest standardize resolve ledger metrics analytics model report \
        test lint format eval app clean help

all: ingest standardize resolve ledger metrics analytics model report
	@echo "[all] done -> $(BUILD)/ (see $(BUILD)/run_manifest.json)"

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
analytics:
	@echo "[analytics] not implemented until Part 5 -- skipped"
model:
	@echo "[model] not implemented until Part 6 -- skipped"
report:
	@echo "[report] not implemented until Part 7 -- skipped"

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
	@echo "[app] not implemented until Part 7 (will run: $(UV) run streamlit run app/Home.py)"; exit 1

clean:
	rm -rf $(BUILD)

help:
	@sed -n '1,15p' Makefile
