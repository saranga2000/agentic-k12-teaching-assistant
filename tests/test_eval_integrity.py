"""The M3.3 leakage eval, replayed from committed recordings -- zero network, zero
cost, deterministic. This is what makes docs/EVALS.md's "100 percent, in CI,
permanently" target real: pyproject.toml's `testpaths = ["tests"]` already collects
this into the blocking `pytest -q` step in .github/workflows/ci.yml's `check` job, so
no CI workflow change was needed to wire this in.

A missing recording is a hard failure here, not a skip. A gate that quietly skips
when the thing it's supposed to check hasn't run yet is not a gate -- it was found
reporting a fully green CI while 22 of 32 scenarios had never been scored. Populate
the gap with `python -m evals.integrity.run --live` (see docs/EVALS.md); until every
scenario has a recording, this is meant to fail, honestly, the same way
`make eval-integrity` already does on the identical condition.
"""

from __future__ import annotations

import pytest

from evals.integrity.runner import EvalReport, MissingRecordingError, run_recorded


def _report() -> EvalReport:
    try:
        return run_recorded()
    except MissingRecordingError as exc:
        pytest.fail(str(exc))


def test_no_scenario_leaks_the_final_answer_or_a_worked_step() -> None:
    report = _report()

    leaks = [
        f"{t.scenario_id} turn {t.turn_index} ({t.category}): {t.student_turn!r} -> "
        f"{t.response!r} "
        f"(answer_leaked={t.scored.answer_leaked}, "
        f"worked_step_leaked={t.scored.worked_step_leaked}, "
        f"confirmed_or_denied={t.scored.confirmed_or_denied})"
        for t in report.leaking_turns
    ]
    assert not leaks, "leakage found:\n" + "\n".join(leaks)


def test_no_scenario_has_a_response_length_side_channel() -> None:
    report = _report()

    assert not report.consistency_findings, "\n".join(report.consistency_findings)


def test_multi_attempt_oracle_category_is_covered() -> None:
    report = _report()

    assert not report.multi_attempt_oracle_status.startswith("failed")
