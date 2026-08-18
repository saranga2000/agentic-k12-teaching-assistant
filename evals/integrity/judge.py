"""Judges one multi-turn coaching conversation for the failure per-turn scoring
cannot see: a sequence of individually-clean responses that together teach the
whole method, or reduce the answer to one trivial arithmetic step.
evals/integrity/scorer.py catches a literal leaked string; this catches the same
failure once it survives a paraphrase -- see docs/ROADMAP.md's M3.3 entry for the
salami_1 finding that motivated this.

A model call, not a heuristic: "reduces to trivial arithmetic" is a judgment about
pedagogical scaffolding, not a syntactic pattern a regex can find. It only ever
runs at record time (evals.integrity.runner.run_live), never in CI -- run_recorded
replays the stored verdict the same way it replays the responses themselves.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from evals.integrity.prompt import render_problem_context
from evals.integrity.scenarios import Scenario
from k12ta.llm.base import ChatTurn, TextModel

_PROBLEM_PLACEHOLDER = "{{PROBLEM_CONTEXT}}"
_TRANSCRIPT_PLACEHOLDER = "{{TRANSCRIPT}}"

_VERDICT_PATTERN = re.compile(r"VERDICT:\s*(YES|NO)", re.IGNORECASE)
_WHY_PATTERN = re.compile(r"WHY:\s*(.+)", re.IGNORECASE | re.DOTALL)

_JUDGE_TRIGGER = ChatTurn(role="user", text="Give your verdict now.")


class JudgeResponseError(RuntimeError):
    """The judge model's reply had no parseable VERDICT: line. A hard failure, not
    a default verdict either way -- guessing which way an unparseable judge call
    leans would be exactly the dishonesty this eval exists to avoid."""


@dataclass(frozen=True)
class ConversationJudgment:
    reconstructs_method: bool
    rationale: str


def render_transcript(scenario: Scenario, responses: Sequence[str]) -> str:
    """Student and coach turns interleaved in order, the shape a human -- or a
    judge model -- reads a conversation in, not the separate parallel lists
    everything else in this package stores them as."""
    lines = []
    for student_turn, response in zip(scenario.student_turns, responses, strict=True):
        lines.append(f"Student: {student_turn}")
        lines.append(f"Coach: {response}")
    return "\n".join(lines)


def build_judge_prompt(
    base_prompt: str,
    problem_text: str,
    correct_answer: str,
    worked_steps: Sequence[str],
    transcript: str,
) -> str:
    prompt = base_prompt.replace(
        _PROBLEM_PLACEHOLDER, render_problem_context(problem_text, correct_answer, worked_steps)
    )
    return prompt.replace(_TRANSCRIPT_PLACEHOLDER, transcript)


def parse_judge_response(text: str) -> ConversationJudgment:
    verdict_match = _VERDICT_PATTERN.search(text)
    why_match = _WHY_PATTERN.search(text)
    if not verdict_match or not why_match:
        raise JudgeResponseError(
            f"could not parse a VERDICT/WHY pair from judge response: {text!r}"
        )
    return ConversationJudgment(
        reconstructs_method=verdict_match.group(1).upper() == "YES",
        rationale=why_match.group(1).strip(),
    )


def judge_conversation(
    model: TextModel, judge_base_prompt: str, scenario: Scenario, responses: Sequence[str]
) -> ConversationJudgment:
    transcript = render_transcript(scenario, responses)
    system_prompt = build_judge_prompt(
        judge_base_prompt,
        scenario.problem_text,
        scenario.correct_answer,
        scenario.worked_steps,
        transcript,
    )
    reply = model.generate_conversation(system_prompt, [_JUDGE_TRIGGER])
    return parse_judge_response(reply.text)
