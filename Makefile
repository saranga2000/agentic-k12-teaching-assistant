.PHONY: install test check fmt run eval

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
	python -m alc.web --host 0.0.0.0 --port 8080
