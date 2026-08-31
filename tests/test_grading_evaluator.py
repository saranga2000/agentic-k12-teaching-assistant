"""M6, the agentic evaluator (docs/ROADMAP.md): tier 2 (text) and the
escalation policy to tier 3 (vision). Offline only -- every model call here is
a fake, matching tests/test_eval_integrity_runner.py's own _FakeTextModel
pattern. No answer-type branch anywhere (AGENTS.md rule 12): these functions
take a problem and an answer, never a "kind" of either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from k12ta.domain.models import GradeOutcome
from k12ta.grading.evaluator import (
    EvaluatorResult,
    evaluate_keyed_mismatch,
    evaluate_keyless,
    should_escalate_to_vision,
)
from k12ta.llm.base import ChatResponse, DataRetention


@dataclass
class _FakeTextModel:
    replies: list[str]
    data_retention: DataRetention = DataRetention.NO_RETENTION
    request_count: int = field(default=0)
    seen_prompts: list[str] = field(default_factory=list)

    def generate_conversation(self, system_prompt: str, turns: object) -> ChatResponse:
        self.seen_prompts.append(system_prompt)
        self.request_count += 1
        return ChatResponse(
            text=self.replies[self.request_count - 1], cost_usd=Decimal("0"), latency_ms=1
        )

    def verify(self) -> None:
        pass


def _reply(verdict: str, confidence: float, generated_answer: str | None = None) -> str:
    return json.dumps(
        {"verdict": verdict, "confidence": confidence, "generated_answer": generated_answer}
    )


# --- evaluate_keyed_mismatch: one call, judges against a given key ----------


def test_keyed_mismatch_recognises_a_semantically_equivalent_answer() -> None:
    """The real failure that produced this system's first four grades at 50%
    unjust (docs/ROADMAP.md's M6): "rhombus" against a key of "quadrilateral"."""
    model = _FakeTextModel(replies=[_reply("correct", 0.9)])

    result = evaluate_keyed_mismatch(model, "What shape is this?", "rhombus", "quadrilateral")

    assert result.outcome is GradeOutcome.CORRECT
    assert result.confidence == 0.9
    assert result.tier == 2


def test_keyed_mismatch_reports_incorrect() -> None:
    model = _FakeTextModel(replies=[_reply("incorrect", 0.95)])

    result = evaluate_keyed_mismatch(model, "2 + 2", "5", "4")

    assert result.outcome is GradeOutcome.INCORRECT


def test_keyed_mismatch_reports_partially_correct() -> None:
    model = _FakeTextModel(replies=[_reply("partially_correct", 0.7)])

    result = evaluate_keyed_mismatch(
        model, "Explain photosynthesis.", "plants use light", "plants convert light to energy"
    )

    assert result.outcome is GradeOutcome.PARTIALLY_CORRECT


def test_keyed_mismatch_never_solves_the_problem_itself() -> None:
    """The prompt hands over the key -- generated_answer is only meaningful for
    the keyless path, per prompts/evaluate_text.md."""
    model = _FakeTextModel(replies=[_reply("correct", 0.9, generated_answer=None)])

    result = evaluate_keyed_mismatch(model, "2 + 2", "4", "4")

    assert result.generated_answer is None


def test_keyed_mismatch_prompt_includes_the_key_and_the_student_answer() -> None:
    model = _FakeTextModel(replies=[_reply("correct", 0.9)])

    evaluate_keyed_mismatch(model, "2 + 2", "4", "4")

    assert "2 + 2" in model.seen_prompts[0]
    assert "4" in model.seen_prompts[0]


def test_keyed_mismatch_malformed_response_is_needs_human_not_a_crash() -> None:
    model = _FakeTextModel(replies=["not json at all"])

    result = evaluate_keyed_mismatch(model, "2 + 2", "4", "4")

    assert result.outcome is GradeOutcome.NEEDS_HUMAN
    assert result.confidence == 0.0


def test_keyed_mismatch_unrecognised_verdict_string_is_needs_human() -> None:
    model = _FakeTextModel(replies=[_reply("definitely_correct", 0.9)])

    result = evaluate_keyed_mismatch(model, "2 + 2", "4", "4")

    assert result.outcome is GradeOutcome.NEEDS_HUMAN


# --- evaluate_keyless: two independent calls, agreement-gated ---------------


def test_keyless_agreement_between_two_independent_solves_is_trusted() -> None:
    """docs/ARCHITECTURE.md's evaluation ladder: "independent solve, then an
    adversarial cross-check pass, then agreement gating" -- the cross-check is
    load-bearing, not ceremony, so this is two real, separate calls, not one
    call with a self-critique instruction."""
    model = _FakeTextModel(
        replies=[
            _reply("correct", 0.9, generated_answer="19"),
            _reply("correct", 0.85, generated_answer="19"),
        ]
    )

    result = evaluate_keyless(model, "Solve for x: 2x + 5 = 43", "19")

    assert result.outcome is GradeOutcome.CORRECT
    assert model.request_count == 2
    # Conservative: the lower of the two confidences, never the higher.
    assert result.confidence == 0.85
    assert result.generated_answer == "19"


def test_keyless_disagreement_between_the_two_solves_is_needs_human() -> None:
    """Never grade from the model's own arithmetic alone
    (docs/PROMPT_REVIEW.md) -- two independent attempts disagreeing is
    exactly the signal that a single self-confident call would hide."""
    model = _FakeTextModel(
        replies=[
            _reply("correct", 0.9, generated_answer="19"),
            _reply("incorrect", 0.9, generated_answer="21"),
        ]
    )

    result = evaluate_keyless(model, "Solve for x: 2x + 5 = 43", "19")

    assert result.outcome is GradeOutcome.NEEDS_HUMAN
    assert model.request_count == 2


def test_keyless_two_calls_are_genuinely_independent_not_a_shared_conversation() -> None:
    """Each call gets its own fresh system prompt/turn -- no memory of the
    other attempt, which is the actual point of an independent second solve."""
    model = _FakeTextModel(
        replies=[_reply("correct", 0.9, generated_answer="19"), _reply("correct", 0.9)]
    )

    evaluate_keyless(model, "Solve for x: 2x + 5 = 43", "19")

    assert model.seen_prompts[0] == model.seen_prompts[1]


def test_keyless_prompt_never_mentions_a_key_answer() -> None:
    model = _FakeTextModel(replies=[_reply("correct", 0.9), _reply("correct", 0.9)])

    evaluate_keyless(model, "2 + 2", "4")

    assert "no answer key" in model.seen_prompts[0].lower()


# --- should_escalate_to_vision: confidence branches only, never a type branch


def test_low_tier2_confidence_escalates_to_vision() -> None:
    result = EvaluatorResult(outcome=GradeOutcome.CORRECT, confidence=0.4, tier=2)

    assert should_escalate_to_vision(result, transcription_confidence=0.98) is True


def test_needs_human_from_tier2_escalates_to_vision() -> None:
    result = EvaluatorResult(outcome=GradeOutcome.NEEDS_HUMAN, confidence=0.0, tier=2)

    assert should_escalate_to_vision(result, transcription_confidence=0.98) is True


def test_low_transcription_confidence_escalates_to_vision_even_if_tier2_was_sure() -> None:
    """This turns a failed transcription from a dead end into an attempt
    (docs/ARCHITECTURE.md) -- tier 3 gets a chance even when tier 2 itself
    was confident, because tier 2 was confident about a possibly-wrong read."""
    result = EvaluatorResult(outcome=GradeOutcome.CORRECT, confidence=0.95, tier=2)

    assert should_escalate_to_vision(result, transcription_confidence=0.3) is True


def test_confident_tier2_on_a_confident_transcription_does_not_escalate() -> None:
    result = EvaluatorResult(outcome=GradeOutcome.CORRECT, confidence=0.95, tier=2)

    assert should_escalate_to_vision(result, transcription_confidence=0.98) is False
