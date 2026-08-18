"""Fills `prompts/coach_voice.md`'s per-call placeholders: which problem this is, and
what the coach is permitted to say about it. Mirrors
`k12ta.transcribe._shared.build_identity_prompt`'s pattern -- the static instructional
text lives in `prompts/*.md` with a `{{PLACEHOLDER}}`; a small builder fills it in per
call, since which problem and which permission set apply is a fact about this call,
not something fixed at prompt-load time.
"""

from __future__ import annotations

from collections.abc import Sequence

from k12ta.domain.policy import FeedbackRules

_PROBLEM_PLACEHOLDER = "{{PROBLEM_CONTEXT}}"
_PERMISSION_PLACEHOLDER = "{{PERMISSION_SET}}"
_PRIOR_RESPONSE_COUNT_PLACEHOLDER = "{{PRIOR_RESPONSE_COUNT}}"


def render_problem_context(
    problem_text: str, correct_answer: str, worked_steps: Sequence[str]
) -> str:
    steps = " then ".join(worked_steps)
    return (
        f'The problem: "{problem_text}"\n'
        f"The correct final answer: {correct_answer}\n"
        f"The worked solution: {steps}"
    )


def render_permission_set(rules: FeedbackRules) -> str:
    return "\n".join(
        [
            f"- reveal_final_answer: {str(rules.reveal_final_answer).lower()}",
            f"- reveal_worked_steps: {str(rules.reveal_worked_steps).lower()}",
            f"- name_error_location: {str(rules.name_error_location).lower()}",
            f"- name_concept: {str(rules.name_concept).lower()}",
            f"- offer_hint_ladder: {str(rules.offer_hint_ladder).lower()}",
        ]
    )


def build_coach_prompt(
    base_prompt: str,
    rules: FeedbackRules,
    problem_text: str,
    correct_answer: str,
    worked_steps: Sequence[str],
    prior_response_count: int,
) -> str:
    """`prior_response_count`: how many times the coach has already responded about
    this exact problem earlier in this conversation -- computed by the caller from
    the real turn history (see evals/integrity/runner.run_live), never left for the
    model to infer from raw history on its own. This is what lets
    prompts/coach_voice.md's "repeated turns" rule hold a hard line under
    salami-slicing pressure instead of trusting the model's own turn count."""
    prompt = base_prompt.replace(
        _PROBLEM_PLACEHOLDER, render_problem_context(problem_text, correct_answer, worked_steps)
    )
    prompt = prompt.replace(_PERMISSION_PLACEHOLDER, render_permission_set(rules))
    return prompt.replace(_PRIOR_RESPONSE_COUNT_PLACEHOLDER, str(prior_response_count))
