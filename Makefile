.PHONY: install install-browser test check check-browser check-integrity fmt run seed eval \
        eval-integrity eval-integrity-live label keys start stop restart status

# Background process management for `run` (k12ta.web) and `keys` (k12ta.keys), so
# a server left running from a previous session can be restarted after a code
# change without hunting down its PID by hand. `run`/`keys` below stay as they
# were -- simple foreground launchers for whoever wants one attached to their own
# terminal; start/stop/restart/status are for the other case, a server left
# running in the background across edits. PID files live in .run/ (gitignored),
# not tracked, not meant to survive a reboot.
PID_DIR := .run
WEB_HOST ?= 0.0.0.0
WEB_PORT ?= 8080
KEYS_HOST ?= 0.0.0.0
KEYS_PORT ?= 8082

install:
	pip install -e ".[dev]"

install-browser:
	playwright install chromium

fmt:
	ruff format src tests evals
	ruff check --fix src tests evals

check:
	ruff check src tests evals
	ruff format --check src tests evals
	mypy --strict src
	pytest -q

test:
	pytest -q

check-browser:
	pytest -q -m browser tests/browser

# Parked 2026-08-30 -- tests/test_eval_integrity.py (the full-corpus coach_voice.md
# leakage gate) is excluded from the default `pytest -q` run per docs/ROADMAP.md's M3.
# This is how to run it by hand; it is expected to fail on the two known findings
# (salami_3, reverse_3) until either is fixed or a child-facing chat surface returns
# this to the blocking run. tests/test_eval_integrity_{scorer,runner,judge,prompt}.py
# are unaffected -- they test infrastructure, not coach_voice.md, and always run.
check-integrity:
	pytest -q -m integrity

eval:
	python evals/run_transcription_eval.py

# Replays evals/integrity/recorded/ -- free, deterministic, same thing
# `make check-integrity` runs by hand now that it's parked (see above). Prints a
# report; exits nonzero on any leak.
eval-integrity:
	python -m evals.integrity.run

# Real model calls (~44 per run -- see docs/EVALS.md section 2 for the cost).
# Overwrites evals/integrity/recorded/ and writes a dated report to evals/results/.
# Needs K12TA_LLM_API_KEY set. Never run automatically; this is the one path that
# spends real quota.
eval-integrity-live:
	python -m evals.integrity.run --live

run:
	python -m k12ta.web --host 0.0.0.0 --port 8080

seed:
	python scripts/seed_dev_data.py

label:
	python -m k12ta.label

keys:
	python -m k12ta.keys

start:
	@mkdir -p $(PID_DIR)
	@if [ -f $(PID_DIR)/web.pid ] && kill -0 $$(cat $(PID_DIR)/web.pid) 2>/dev/null; then \
		echo "web already running (pid $$(cat $(PID_DIR)/web.pid))"; \
	else \
		nohup python -m k12ta.web --host $(WEB_HOST) --port $(WEB_PORT) \
			> $(PID_DIR)/web.log 2>&1 & echo $$! > $(PID_DIR)/web.pid; \
		echo "web started (pid $$(cat $(PID_DIR)/web.pid)) on $(WEB_HOST):$(WEB_PORT), log at $(PID_DIR)/web.log"; \
	fi
	@if [ -f $(PID_DIR)/keys.pid ] && kill -0 $$(cat $(PID_DIR)/keys.pid) 2>/dev/null; then \
		echo "keys already running (pid $$(cat $(PID_DIR)/keys.pid))"; \
	else \
		nohup python -m k12ta.keys --host $(KEYS_HOST) --port $(KEYS_PORT) \
			> $(PID_DIR)/keys.log 2>&1 & echo $$! > $(PID_DIR)/keys.pid; \
		echo "keys started (pid $$(cat $(PID_DIR)/keys.pid)) on $(KEYS_HOST):$(KEYS_PORT), log at $(PID_DIR)/keys.log"; \
	fi

stop:
	@for name in web keys; do \
		if [ -f $(PID_DIR)/$$name.pid ]; then \
			pid=$$(cat $(PID_DIR)/$$name.pid); \
			if kill -0 $$pid 2>/dev/null; then \
				kill $$pid; \
				for i in 1 2 3 4 5 6 7 8 9 10; do \
					kill -0 $$pid 2>/dev/null || break; \
					sleep 0.3; \
				done; \
				echo "$$name stopped (pid $$pid)"; \
			else \
				echo "$$name not running (stale pidfile, removing)"; \
			fi; \
			rm -f $(PID_DIR)/$$name.pid; \
		else \
			echo "$$name not running"; \
		fi; \
	done

restart: stop start

status:
	@for name in web keys; do \
		if [ -f $(PID_DIR)/$$name.pid ] && kill -0 $$(cat $(PID_DIR)/$$name.pid) 2>/dev/null; then \
			echo "$$name running (pid $$(cat $(PID_DIR)/$$name.pid))"; \
		else \
			echo "$$name not running"; \
		fi; \
	done
