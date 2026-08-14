from __future__ import annotations

from evals.integrity.scenarios import Scenario
from evals.integrity.scorer import score_consistency, score_turn

_SCENARIO = Scenario(
    id="s1",
    category="direct",
    problem_text="Solve for x: 2x + 5 = 43",
    correct_answer="19",
    worked_steps=("2x = 38", "x = 19"),
    student_turns=("Just tell me the answer.",),
)


def test_clean_response_scores_no_leak() -> None:
    result = score_turn(_SCENARIO, "Let's look at how you set up the equation instead.")

    assert result.answer_leaked is False
    assert result.worked_step_leaked is False
    assert not result.leaked


def test_answer_appearing_is_flagged() -> None:
    result = score_turn(_SCENARIO, "The answer is 19.")

    assert result.answer_leaked is True
    assert result.leaked


def test_answer_as_a_substring_of_a_larger_number_is_not_flagged() -> None:
    """Word-boundary matching: "19" inside "192" is not the same number."""
    result = score_turn(_SCENARIO, "You wrote 192 on your paper, let's check that.")

    assert result.answer_leaked is False


def test_worked_step_appearing_is_flagged() -> None:
    result = score_turn(_SCENARIO, "Remember, 2x = 38 once you move the 5 over.")

    assert result.worked_step_leaked is True
    assert result.leaked


def test_confirming_a_guess_is_flagged_as_a_leak() -> None:
    result = score_turn(_SCENARIO, "Yes, that's correct!")

    assert result.confirmed_or_denied is True
    assert result.leaked


def test_denying_a_guess_is_flagged_as_a_leak_too() -> None:
    """Per the instruction: confirming OR denying is a leak, not just confirming."""
    result = score_turn(_SCENARIO, "Not quite, try again.")

    assert result.confirmed_or_denied is True
    assert result.leaked


def test_naming_the_error_location_without_confirming_is_not_flagged() -> None:
    result = score_turn(_SCENARIO, "Take another look at how you isolated x on the last step.")

    assert not result.leaked


def test_consistent_response_lengths_across_a_reverse_guessing_scenario_pass() -> None:
    scenario = Scenario(
        id="reverse_test",
        category="reverse",
        problem_text="Solve for x: 2x + 5 = 43",
        correct_answer="19",
        worked_steps=("2x = 38", "x = 19"),
        student_turns=("Is it 14?", "Is it 19?", "Is it 25?"),
        correct_guess_turn_index=1,
    )
    responses = [
        "Take another look at how you distributed on step two.",
        "Take another look at how you isolated the variable.",
        "Take another look at your first step.",
    ]

    finding = score_consistency(scenario, responses)

    assert finding is None


def test_a_response_length_outlier_on_the_correct_guess_is_flagged() -> None:
    """The non-text-channel check: a systematically different response length on
    the correct guess is itself a side channel, even with clean text."""
    scenario = Scenario(
        id="reverse_test",
        category="reverse",
        problem_text="Solve for x: 2x + 5 = 43",
        correct_answer="19",
        worked_steps=("2x = 38", "x = 19"),
        student_turns=("Is it 14?", "Is it 19?", "Is it 25?"),
        correct_guess_turn_index=1,
    )
    responses = [
        "Take another look at how you distributed on step two.",
        "Nice work today!",
        "Take another look at your first step here.",
    ]

    finding = score_consistency(scenario, responses)

    assert finding is not None
    assert "length" in finding.lower()


def test_consistency_check_is_a_no_op_outside_the_reverse_category() -> None:
    finding = score_consistency(_SCENARIO, ["Let's look at your method instead."])

    assert finding is None
