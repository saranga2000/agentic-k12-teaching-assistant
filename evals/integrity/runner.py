"""Runs the adversarial scenario set (`evals/integrity/scenarios.py`) against
`prompts/coach_voice.md`, scores each turn (`evals/integrity/scorer.py`), judges
each multi-turn scenario as a whole (`evals/integrity/judge.py`), and reports
pass/fail per category -- including the seventh category, multi-attempt oracle,
which is deliberately NOT represented in `scenarios.py`: it needs no model and no
conversational turn, since it's a property of the deterministic pipeline, already
proven end to end by `tests/test_web_capture.py`'s recapture/suppression tests and
`tests/browser/test_multi_attempt_oracle.py`.

`run_live()` calls the real model (~1 call per turn, plus one judge call per
multi-turn scenario -- see docs/EVALS.md for the cost) and re-runs those specific
tests as a subprocess, so one command reports on all seven categories together,
and writes each scenario's responses to `evals/integrity/recorded/<scenario_id>.json`.
Every write is stamped with the `coach_voice.md` version it was recorded against
(`k12ta.prompts.load_prompt_version`) -- a recording is only honest evidence about
the exact prompt text that produced it, the same reasoning
`k12ta.store.page_identity_schemas` applies to a page-identity schema. `resume`
(default True) only reuses an already-recorded scenario when its stamped version
still matches the current prompt; a stale recording is silently treated as absent
and re-called, never trusted.

Within a scenario, each turn's system prompt is rebuilt with a real count of how
many times the coach has already responded on this exact problem earlier in this
conversation (`PRIOR_RESPONSE_COUNT` in `prompts/coach_voice.md`) -- computed here
from the real turn history, never left for the model to infer on its own.

`run_recorded()` is what CI runs (via `tests/test_eval_integrity.py`): zero network,
zero cost, deterministic, reading those same JSON files -- including the stored
verdict from the conversation-level judge, never re-running it. It does NOT re-run
the multi-attempt-oracle tests as a subprocess -- they are already part of the same
`pytest -q` run `tests/test_eval_integrity.py` is itself collected into, so spawning a
second, nested pytest process to prove something the outer run already proves would
be redundant, slower, and its own source of flakiness. It raises loudly, never
skips, on anything that makes a recording untrustworthy: missing entirely
(`MissingRecordingError`), or present but stale against the current prompt version
or missing a required conversation judgment (`StaleRecordingError`) -- both are
`RecordingUnusableError`, and both mean the eval has not actually run, so reporting
a pass would be dishonest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.integrity.judge import judge_conversation
from evals.integrity.prompt import build_coach_prompt
from evals.integrity.scenarios import Scenario, all_scenarios
from evals.integrity.scorer import ScoredTurn, score_consistency, score_turn
from k12ta.config import Settings
from k12ta.domain.policy import FeedbackMode, rules_for
from k12ta.llm import build_text_model
from k12ta.llm.base import ChatTurn, TextModel
from k12ta.prompts import load_prompt, load_prompt_version

RECORDED_DIR = Path(__file__).parent / "recorded"

# The pytest node IDs that prove the multi-attempt-oracle category end to end --
# named here, not rediscovered by a marker or a directory scan, so a rename of any
# of them is a deliberate, visible edit to this list, not a silent gap.
MULTI_ATTEMPT_ORACLE_TESTS = (
    "tests/test_web_capture.py::"
    "test_a_changed_answer_on_recapture_is_suppressed_but_an_unchanged_one_is_not",
    "tests/test_web_capture.py::test_a_second_new_wrong_guess_is_also_suppressed",
    "tests/browser/test_multi_attempt_oracle.py::"
    "test_a_second_capture_with_a_changed_correct_answer_never_confirms_it",
)


class RecordingUnusableError(RuntimeError):
    """Base for anything that makes a recorded scenario file untrustworthy as
    evidence -- missing entirely, or present but stale against the prompt that
    produced it. tests/test_eval_integrity.py fails loudly on any subclass of
    this, never skips: a recording that can't be trusted means the eval has not
    actually run, and treating that as a pass would be dishonest."""


class MissingRecordingError(RecordingUnusableError):
    """A scenario has no recorded response yet -- run_live() (or
    `python -m evals.integrity.run --live`) has never populated evals/integrity/
    recorded/ for it. Distinct from a scoring failure: nothing has been scored at
    all, so treating this as a leak or as a pass would both be dishonest."""


class StaleRecordingError(RecordingUnusableError):
    """A recording exists but no longer matches what's required to trust it: its
    stamped prompt_version differs from prompts/coach_voice.md's current version,
    or (for a multi-turn scenario) it has no conversation_judgment because it
    predates conversation-level scoring. The fix is the same as a missing
    recording: `python -m evals.integrity.run --live`."""


@dataclass(frozen=True)
class TurnResult:
    scenario_id: str
    category: str
    turn_index: int
    student_turn: str
    response: str
    scored: ScoredTurn


@dataclass(frozen=True)
class EvalReport:
    turn_results: tuple[TurnResult, ...]
    consistency_findings: tuple[str, ...]
    conversation_findings: tuple[str, ...]
    """One entry per multi-turn scenario the judge found reconstructs the method
    or reduces the answer to trivial arithmetic when read end to end -- distinct
    from a per-turn leak, which none of these turns individually needs to be."""
    multi_attempt_oracle_status: str
    """"covered_by_pytest_suite" (run_recorded's case -- see module docstring),
    "passed", or "failed: <captured pytest output>" (run_live's case)."""

    @property
    def leaking_turns(self) -> tuple[TurnResult, ...]:
        return tuple(t for t in self.turn_results if t.scored.leaked)

    @property
    def passed(self) -> bool:
        return (
            not self.leaking_turns
            and not self.consistency_findings
            and not self.conversation_findings
            and not self.multi_attempt_oracle_status.startswith("failed")
        )

    def category_counts(self) -> dict[str, tuple[int, int]]:
        """category -> (leaking turns, total turns)."""
        counts: dict[str, list[int]] = {}
        for turn in self.turn_results:
            bucket = counts.setdefault(turn.category, [0, 0])
            bucket[1] += 1
            if turn.scored.leaked:
                bucket[0] += 1
        return {category: (leaking, total) for category, (leaking, total) in counts.items()}


def _recorded_path(scenario_id: str, recorded_dir: Path) -> Path:
    return recorded_dir / f"{scenario_id}.json"


def _recording_problem(
    data: dict[str, Any], scenario: Scenario, current_prompt_version: int
) -> str | None:
    """None if `data` (an already-loaded recording) is trustworthy for `scenario`
    under the prompt's current version; otherwise a human-readable reason it
    isn't. Shared by run_recorded (which raises on a non-None result) and
    run_live's resume check (which treats a non-None result as "not resumable,
    call the model again") -- the two callers differ only in what they do with
    the answer, not in how they compute it."""
    stamped_version = data.get("prompt_version")
    if stamped_version != current_prompt_version:
        return (
            f"recorded under prompt_version={stamped_version!r}, coach_voice.md is "
            f"now version {current_prompt_version}"
        )
    if len(scenario.student_turns) > 1 and "conversation_judgment" not in data:
        return "has no conversation_judgment (recorded before conversation-level scoring existed)"
    return None


def _conversation_finding(scenario_id: str, judgment_data: dict[str, Any]) -> str | None:
    if not judgment_data.get("reconstructs_method"):
        return None
    return f"{scenario_id}: {judgment_data.get('rationale')}"


def _score_scenario(scenario: Scenario, responses: list[str]) -> tuple[list[TurnResult], list[str]]:
    turn_results = [
        TurnResult(
            scenario_id=scenario.id,
            category=scenario.category,
            turn_index=i,
            student_turn=turn,
            response=response,
            scored=score_turn(scenario, response),
        )
        for i, (turn, response) in enumerate(zip(scenario.student_turns, responses, strict=True))
    ]
    finding = score_consistency(scenario, responses)
    return turn_results, ([finding] if finding else [])


def run_recorded(
    scenarios: tuple[Scenario, ...] | None = None,
    recorded_dir: Path = RECORDED_DIR,
) -> EvalReport:
    scenarios = scenarios if scenarios is not None else all_scenarios()
    current_prompt_version = load_prompt_version("coach_voice")
    turn_results: list[TurnResult] = []
    findings: list[str] = []
    conversation_findings: list[str] = []
    for scenario in scenarios:
        path = _recorded_path(scenario.id, recorded_dir)
        if not path.is_file():
            raise MissingRecordingError(
                f"no recorded response for scenario {scenario.id!r} at {path} -- run "
                "`python -m evals.integrity.run --live` once to populate "
                "evals/integrity/recorded/"
            )
        data = json.loads(path.read_text())
        problem = _recording_problem(data, scenario, current_prompt_version)
        if problem is not None:
            raise StaleRecordingError(
                f"recording for {scenario.id!r} at {path} is stale: {problem} -- run "
                "`python -m evals.integrity.run --live` to refresh evals/integrity/recorded/"
            )
        responses = data["responses"]
        scenario_turns, scenario_findings = _score_scenario(scenario, responses)
        turn_results.extend(scenario_turns)
        findings.extend(scenario_findings)
        judgment_data = data.get("conversation_judgment")
        if judgment_data is not None:
            finding = _conversation_finding(scenario.id, judgment_data)
            if finding:
                conversation_findings.append(finding)
    return EvalReport(
        turn_results=tuple(turn_results),
        consistency_findings=tuple(findings),
        conversation_findings=tuple(conversation_findings),
        multi_attempt_oracle_status="covered_by_pytest_suite",
    )


def run_live(
    settings: Settings,
    scenarios: tuple[Scenario, ...] | None = None,
    recorded_dir: Path = RECORDED_DIR,
    model: TextModel | None = None,
    run_oracle_tests: bool = True,
    resume: bool = True,
) -> EvalReport:
    """`resume` (default True): a scenario whose recording already exists in
    `recorded_dir` *and* is still current (see `_recording_problem`) is scored
    from that file instead of calling the model again -- so a transient failure
    partway through a many-call run costs a retry of only the scenarios that
    never completed, not the ones that already did. A recording that exists but
    is stale (an older prompt version, or missing its conversation judgment) is
    never resumed from, even with `resume=True` -- it's treated exactly like a
    missing recording and re-called. Pass `resume=False` to force every scenario
    to be re-called regardless of what's already recorded."""
    scenarios = scenarios if scenarios is not None else all_scenarios()
    model = model if model is not None else build_text_model(settings)
    base_prompt = load_prompt("coach_voice")
    judge_base_prompt = load_prompt("integrity_judge")
    current_prompt_version = load_prompt_version("coach_voice")
    rules = rules_for(FeedbackMode.DIAGNOSTIC_ONLY)

    turn_results: list[TurnResult] = []
    findings: list[str] = []
    conversation_findings: list[str] = []
    recorded_dir.mkdir(parents=True, exist_ok=True)
    for scenario in scenarios:
        path = _recorded_path(scenario.id, recorded_dir)
        resumed: dict[str, Any] | None = None
        if resume and path.is_file():
            candidate = json.loads(path.read_text())
            if _recording_problem(candidate, scenario, current_prompt_version) is None:
                resumed = candidate

        if resumed is not None:
            responses = resumed["responses"]
            judgment_data = resumed.get("conversation_judgment")
        else:
            history: list[ChatTurn] = []
            responses = []
            for student_turn in scenario.student_turns:
                prior_response_count = sum(1 for t in history if t.role == "model")
                system_prompt = build_coach_prompt(
                    base_prompt,
                    rules,
                    scenario.problem_text,
                    scenario.correct_answer,
                    scenario.worked_steps,
                    prior_response_count=prior_response_count,
                )
                history.append(ChatTurn(role="user", text=student_turn))
                reply = model.generate_conversation(system_prompt, history)
                history.append(ChatTurn(role="model", text=reply.text))
                responses.append(reply.text)

            record: dict[str, Any] = {
                "scenario_id": scenario.id,
                "responses": responses,
                "prompt_version": current_prompt_version,
            }
            judgment_data = None
            if len(scenario.student_turns) > 1:
                judgment = judge_conversation(model, judge_base_prompt, scenario, responses)
                judgment_data = {
                    "reconstructs_method": judgment.reconstructs_method,
                    "rationale": judgment.rationale,
                }
                record["conversation_judgment"] = judgment_data
            path.write_text(json.dumps(record, indent=2) + "\n")

        scenario_turns, scenario_findings = _score_scenario(scenario, responses)
        turn_results.extend(scenario_turns)
        findings.extend(scenario_findings)
        if judgment_data is not None:
            finding = _conversation_finding(scenario.id, judgment_data)
            if finding:
                conversation_findings.append(finding)

    oracle_status = (
        _run_multi_attempt_oracle_tests() if run_oracle_tests else "covered_by_pytest_suite"
    )

    return EvalReport(
        turn_results=tuple(turn_results),
        consistency_findings=tuple(findings),
        conversation_findings=tuple(conversation_findings),
        multi_attempt_oracle_status=oracle_status,
    )


def _run_multi_attempt_oracle_tests() -> str:
    """`-o addopts=` clears this repo's default `-m 'not browser'` filter (see
    pyproject.toml) for this one subprocess invocation -- one of the three node IDs
    above is browser-marked, and without this it would be silently deselected
    rather than actually run. Needs `playwright install chromium` already done,
    same precondition as `make check-browser`."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *MULTI_ATTEMPT_ORACLE_TESTS],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return "passed"
    return f"failed: {(result.stdout + result.stderr)[-4000:]}"
