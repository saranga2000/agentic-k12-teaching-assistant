.PHONY: install test check fmt run eval label

install:
	pip install -e ".[dev]"

fmt:
	ruff format src tests evals
	ruff check --fix src tests evals

check:
	ruff check src tests evals
	mypy --strict src
	pytest -q

test:
	pytest -q

eval:
	python evals/run_transcription_eval.py

run:
	python -m k12ta.web --host 0.0.0.0 --port 8080

label:
	python -m k12ta.label
