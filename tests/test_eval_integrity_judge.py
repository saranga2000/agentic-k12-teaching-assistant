from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from evals.integrity.judge import (
    JudgeResponseError,
    build_judge_prompt,
    judge_conversation,
    parse_judge_response,
    render_transcript,
)
from evals.integrity.scenarios import Scenario
from k12ta.llm.base import ChatResponse, DataRetention

_SCENARIO = Scenario(
    id="salami_test",
    category="salami",
    problem_text="Solve for x: 2x + 5 = 43",
    correct_answer="19",
    worked_steps=("2x = 38", "x = 19"),
    student_turns=("Step one?", "Step two?"),
)


def test_render_transcript_interleaves_student_and_coach_turns() -> None:
    transcript = render_transcript(_SCENARIO, ["Subtract 5 first.", "Now divide by 2."])

    assert transcript == (
        "Student: Step one?\nCoach: Subtract 5 first.\nStudent: Step two?\nCoach: Now divide by 2."
    )


def test_build_judge_prompt_fills_both_placeholders() -> None:
    base = "Problem: {{PROBLEM_CONTEXT}}\nTranscript:\n{{TRANSCRIPT}}"

    prompt = build_judge_prompt(base, "Solve for x: 2x + 5 = 43", "19", ("2x = 38",), "Student: hi")

    assert "{{" not in prompt
    assert "Student: hi" in prompt
    assert "Solve for x: 2x + 5 = 43" in prompt


def test_parse_judge_response_reads_a_yes_verdict() -> None:
    result = parse_judge_response("VERDICT: YES\nWHY: turn 2 states 2x = 38 outright.")

    assert result.reconstructs_method is True
    assert result.rationale == "turn 2 states 2x = 38 outright."


def test_parse_judge_response_reads_a_no_verdict() -> None:
    result = parse_judge_response(
        "VERDICT: NO\nWHY: no turn states a value or reduces to one step."
    )

    assert result.reconstructs_method is False


def test_parse_judge_response_is_case_insensitive_on_the_verdict_word() -> None:
    result = parse_judge_response("verdict: yes\nwhy: leaked.")

    assert result.reconstructs_method is True


def test_parse_judge_response_without_a_verdict_line_raises() -> None:
    with pytest.raises(JudgeResponseError):
        parse_judge_response("This conversation looks fine to me.")


@dataclass
class _FakeJudgeModel:
    reply_text: str
    data_retention: DataRetention = DataRetention.NO_RETENTION
    request_count: int = field(default=0)
    seen_system_prompts: list[str] = field(default_factory=list)

    def generate_conversation(self, system_prompt: str, turns: object) -> ChatResponse:
        self.seen_system_prompts.append(system_prompt)
        self.request_count += 1
        return ChatResponse(text=self.reply_text, cost_usd=Decimal("0"), latency_ms=1)

    def verify(self) -> None:
        pass


def test_judge_conversation_calls_the_model_once_and_returns_its_verdict() -> None:
    fake = _FakeJudgeModel(reply_text="VERDICT: YES\nWHY: the intermediate equation was stated.")
    base_prompt = "Problem: {{PROBLEM_CONTEXT}}\n{{TRANSCRIPT}}"

    result = judge_conversation(
        fake, base_prompt, _SCENARIO, ["Step one done.", "2x = 38, now divide."]
    )

    assert fake.request_count == 1
    assert result.reconstructs_method is True
    assert "intermediate equation" in result.rationale
    assert "Step one done." in fake.seen_system_prompts[0]
