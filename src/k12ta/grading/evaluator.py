"""M6, the agentic evaluator (docs/ROADMAP.md): tiers 2 and 3 of the evaluation
ladder (docs/ARCHITECTURE.md, "The evaluation ladder"). Tier 1, the deterministic
key match, is k12ta.grading.key_grader -- unchanged, still tried first, still free.

No answer-type enumeration anywhere in this file (AGENTS.md rule 12). Every
function here takes a problem and an answer as plain text; none of them asks
what kind of question it is. The only branches are confidence branches: whether
tier 2 agreed with itself, and whether tier 2's own confidence (or the
transcription's) is low enough to escalate to tier 3.

Vision (tier 3) is not implemented in this file yet -- see should_escalate_to_
vision, which decides *whether* to escalate, offline and model-free, so the
policy is tested independently of tier 3 existing at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from k12ta.domain.models import GradeOutcome
from k12ta.llm.base import ChatTurn, TextModel
from k12ta.prompts import load_prompt

_VERDICT_FLOOR = 0.7
"""Below this, tier 2's own verdict is not trusted enough to stand alone --
escalate to tier 3 rather than pass a shaky text-only judgement through."""


@dataclass(frozen=True)
class EvaluatorResult:
    outcome: GradeOutcome
    confidence: float
    generated_answer: str | None = None
    """The evaluator's own solved answer -- only meaningful for the keyless
    path (docs/EVALS.md's key-withheld method measures this separately from
    verdict accuracy). Always None for a keyed-mismatch judgement, which never
    solves the problem itself."""
    tier: int = 2


def _strip_code_fence(text: str) -> str:
    """Same reasoning as k12ta.transcribe._shared.strip_code_fence -- not
    imported from there since that module is private to k12ta.transcribe and
    this is a handful of lines, not worth a cross-package private import."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()


_OUTCOME_BY_VERDICT = {
    "correct": GradeOutcome.CORRECT,
    "partially_correct": GradeOutcome.PARTIALLY_CORRECT,
    "incorrect": GradeOutcome.INCORRECT,
    "needs_human": GradeOutcome.NEEDS_HUMAN,
}


def _parse_response(text: str, tier: int) -> EvaluatorResult:
    """Ambiguity resolves to NEEDS_HUMAN, never a guess -- same rule
    k12ta.grading.key_grader.normalise's docstring states for the
    deterministic path. A malformed or unrecognised response is exactly as
    honest a failure here as an unreadable photograph is for transcription."""
    try:
        payload = json.loads(_strip_code_fence(text))
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        verdict = payload.get("verdict")
        outcome = _OUTCOME_BY_VERDICT.get(str(verdict))
        if outcome is None:
            return EvaluatorResult(outcome=GradeOutcome.NEEDS_HUMAN, confidence=0.0, tier=tier)
        confidence = payload.get("confidence")
        valid_confidence = isinstance(confidence, int | float) and not isinstance(confidence, bool)
        generated_answer = payload.get("generated_answer")
        return EvaluatorResult(
            outcome=outcome,
            confidence=float(confidence) if valid_confidence else 0.0,  # type: ignore[arg-type]
            generated_answer=str(generated_answer) if generated_answer else None,
            tier=tier,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return EvaluatorResult(outcome=GradeOutcome.NEEDS_HUMAN, confidence=0.0, tier=tier)


_PROMPT = load_prompt("evaluate_text")

_KEYED_SECTION = 'The answer key says: "{key_answer}"'
_KEYED_INSTRUCTIONS = (
    "Judge whether the student's answer means the same thing as the key's answer "
    "above -- reason about meaning, not exact wording. Do not solve the problem "
    "yourself here; the key is already the answer, your job is only to compare. "
    "Leave generated_answer null."
)
_KEYLESS_SECTION = (
    "No answer key exists for this problem. You must work out the correct answer yourself."
)
_KEYLESS_INSTRUCTIONS = (
    "First, solve the problem yourself, carefully. Then compare the student's "
    "answer against your own solved answer to decide the verdict. Report your "
    "own solved answer in generated_answer."
)


def _build_prompt(problem_text: str, student_answer: str, key_answer: str | None) -> str:
    section = _KEYED_SECTION.format(key_answer=key_answer) if key_answer else _KEYLESS_SECTION
    instructions = _KEYED_INSTRUCTIONS if key_answer else _KEYLESS_INSTRUCTIONS
    return (
        _PROMPT.replace("{{PROBLEM_TEXT}}", problem_text)
        .replace("{{STUDENT_ANSWER}}", student_answer)
        .replace("{{KEY_ANSWER_SECTION}}", section)
        .replace("{{KEY_ANSWER_INSTRUCTIONS}}", instructions)
    )


def _call(
    text_model: TextModel, problem_text: str, student_answer: str, key_answer: str | None
) -> EvaluatorResult:
    prompt = _build_prompt(problem_text, student_answer, key_answer)
    response = text_model.generate_conversation(
        prompt, (ChatTurn(role="user", text="Judge this answer now."),)
    )
    return _parse_response(response.text, tier=2)


def evaluate_keyed_mismatch(
    text_model: TextModel, problem_text: str, student_answer: str, key_answer: str
) -> EvaluatorResult:
    """A key exists and the deterministic exact-match failed -- the permanent
    fix for this system's first four grades at 50% unjust ("rhombus" marked
    wrong against a key of "quadrilateral"). One call: the agent reads the
    key and the student's answer and decides whether they mean the same
    thing. Never independently solves the problem -- the key already is the
    answer."""
    return _call(text_model, problem_text, student_answer, key_answer)


def evaluate_keyless(
    text_model: TextModel, problem_text: str, student_answer: str
) -> EvaluatorResult:
    """No key exists at all -- V1's core keyless capability (docs/ROADMAP.md).
    Two genuinely independent calls (fresh prompt, no shared conversation
    history between them), each solving the problem itself and judging the
    student's answer against its own solution, gated on agreement. This is
    "never grade from the model's own arithmetic alone" (docs/PROMPT_REVIEW.md)
    applied to a model that is now the one doing the arithmetic: the
    cross-check is load-bearing, not ceremony, so this costs two calls, not
    one call with a self-critique instruction bolted on."""
    first = _call(text_model, problem_text, student_answer, None)
    second = _call(text_model, problem_text, student_answer, None)
    if first.outcome is not second.outcome:
        return EvaluatorResult(outcome=GradeOutcome.NEEDS_HUMAN, confidence=0.0, tier=2)
    return EvaluatorResult(
        outcome=first.outcome,
        confidence=min(first.confidence, second.confidence),
        generated_answer=first.generated_answer or second.generated_answer,
        tier=2,
    )


def should_escalate_to_vision(
    tier2_result: EvaluatorResult, transcription_confidence: float
) -> bool:
    """All confidence signals, none of them a question category
    (docs/ARCHITECTURE.md's evaluation ladder): tier 2 itself declined or was
    unsure, or the transcription it reasoned over might be wrong in the first
    place -- a confident tier-2 verdict about a possibly-misread answer is not
    actually confident about anything real."""
    if tier2_result.outcome is GradeOutcome.NEEDS_HUMAN:
        return True
    if tier2_result.confidence < _VERDICT_FLOOR:
        return True
    return transcription_confidence < _VERDICT_FLOOR
