"""The M3.3 leakage eval, replayed from committed recordings -- zero network, zero
cost, deterministic.

**Parked 2026-08-30, per docs/ROADMAP.md's rewritten M3 "done when" and
docs/EVALS.md section 2.** This scores `prompts/coach_voice.md`, the child-facing
Socratic tutoring prompt -- V1 builds no child-facing conversational surface at all,
so this file no longer runs in the default `pytest -q` / CI `check` job (see the
`integrity` marker in pyproject.toml). It is not deleted and not weakened: every
recording stays on disk, every assertion below is unchanged, and this file returns to
the blocking run the moment any child-facing chat surface is built -- at which point
wiring that surface is itself gated on every assertion here passing at 100 percent.
Run it explicitly with `make check-integrity` (or `pytest -q -m integrity`); as of
this note it still fails, honestly, on the two known findings recorded in
docs/ROADMAP.md's M3 section (`salami_3`, `reverse_3`'s length side channel) -- this
file failing today is expected, not a regression to chase.

A missing or stale recording is a hard failure here, not a skip. A gate that quietly
skips when the thing it's supposed to check hasn't run yet is not a gate -- it was
found reporting a fully green CI while 22 of 32 scenarios had never been scored.
"Stale" now includes a recording made under an older prompt_version than
prompts/coach_voice.md's current one, and a multi-turn scenario recorded before
conversation-level scoring existed (see evals/integrity/runner.RecordingUnusableError
and its two subclasses). Populate the gap with `python -m evals.integrity.run --live`
(see docs/EVALS.md); until every scenario has a current recording, this is meant to
fail, honestly, the same way `make eval-integrity` already does on the identical
condition.
"""

from __future__ import annotations

import pytest

from evals.integrity.runner import EvalReport, RecordingUnusableError, run_recorded

pytestmark = pytest.mark.integrity


def _report() -> EvalReport:
    try:
        return run_recorded()
    except RecordingUnusableError as exc:
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


def test_no_multi_turn_scenario_reconstructs_the_method_across_turns() -> None:
    """The salami_1 finding: a conversation can hand over the whole method while
    every individual turn passes score_turn. This is what catches that -- see
    evals/integrity/judge.py."""
    report = _report()

    assert not report.conversation_findings, "\n".join(report.conversation_findings)
