# MCA risk pipeline. Recipes are POSIX sh and call every tool through `uv run`, so they work on Linux CI
# and on Windows (GNU make + Git Bash sh). `make all` rebuilds build/ from raw/.
#
#   make all            full pipeline (stages not built yet print "not implemented until Part N")
#   make ingest         Part 1: raw/ -> build/portfolio.duckdb raw_* tables + build/run_manifest.json
#   make standardize    Part 2: raw_* -> *_std tables, data_quality, build/dq_report.md (mappings/*.yaml)
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
	@echo "[resolve] not implemented until Part 3 -- skipped"
ledger:
	@echo "[ledger] not implemented until Part 4 -- skipped"
metrics:
	@echo "[metrics] not implemented until Part 4 -- skipped"
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
	@sed -n '1,13p' Makefile
