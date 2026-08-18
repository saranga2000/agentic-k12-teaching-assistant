from __future__ import annotations

from evals.integrity.prompt import build_coach_prompt
from k12ta.domain.policy import FeedbackMode, rules_for

_BASE = (
    "Problem: {{PROBLEM_CONTEXT}}\nPermissions: {{PERMISSION_SET}}\nPrior: {{PRIOR_RESPONSE_COUNT}}"
)


def test_prior_response_count_is_rendered_into_the_prompt() -> None:
    prompt = build_coach_prompt(
        _BASE,
        rules_for(FeedbackMode.DIAGNOSTIC_ONLY),
        "Solve for x: 2x + 5 = 43",
        "19",
        ("2x = 38", "x = 19"),
        prior_response_count=0,
    )

    assert "Prior: 0" in prompt


def test_prior_response_count_reflects_the_caller_supplied_value() -> None:
    prompt = build_coach_prompt(
        _BASE,
        rules_for(FeedbackMode.DIAGNOSTIC_ONLY),
        "Solve for x: 2x + 5 = 43",
        "19",
        ("2x = 38", "x = 19"),
        prior_response_count=2,
    )

    assert "Prior: 2" in prompt


def test_no_placeholder_survives_a_real_prompt_render() -> None:
    prompt = build_coach_prompt(
        _BASE,
        rules_for(FeedbackMode.DIAGNOSTIC_ONLY),
        "Solve for x: 2x + 5 = 43",
        "19",
        ("2x = 38", "x = 19"),
        prior_response_count=1,
    )

    assert "{{" not in prompt
