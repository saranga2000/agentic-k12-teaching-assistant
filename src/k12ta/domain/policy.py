"""Feedback policy: what the coach is allowed to say after a wrong answer.

This is the academic integrity rail. It is derived per assignment from the content
source, never from a single global toggle, because on any given evening a student may
have graded school homework, a graded outside-school assignment, and self-directed
practice open at the same time.

Fail-closed: an unknown or ambiguous source resolves to the most restrictive policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FeedbackMode(Enum):
    FULL = "full"
    """Self-directed practice. Teach everything, show worked solutions."""

    DIAGNOSTIC_ONLY = "diagnostic_only"
    """Work that someone else will grade. Locate the error, name the concept, stop."""

    FLUENCY = "fluency"
    """Timed automaticity drills. Score speed and accuracy, teach after the timer."""


@dataclass(frozen=True)
class FeedbackRules:
    """The concrete permissions the response generator must honour."""

    mode: FeedbackMode
    reveal_final_answer: bool
    reveal_worked_steps: bool
    name_error_location: bool
    name_concept: bool
    offer_hint_ladder: bool
    is_timed: bool

    def forbids_answer(self) -> bool:
        return not self.reveal_final_answer


_RULES: dict[FeedbackMode, FeedbackRules] = {
    FeedbackMode.FULL: FeedbackRules(
        mode=FeedbackMode.FULL,
        reveal_final_answer=True,
        reveal_worked_steps=True,
        name_error_location=True,
        name_concept=True,
        offer_hint_ladder=True,
        is_timed=False,
    ),
    FeedbackMode.DIAGNOSTIC_ONLY: FeedbackRules(
        mode=FeedbackMode.DIAGNOSTIC_ONLY,
        reveal_final_answer=False,
        reveal_worked_steps=False,
        name_error_location=True,
        name_concept=True,
        offer_hint_ladder=True,
        is_timed=False,
    ),
    FeedbackMode.FLUENCY: FeedbackRules(
        mode=FeedbackMode.FLUENCY,
        reveal_final_answer=False,
        reveal_worked_steps=False,
        name_error_location=False,
        name_concept=False,
        offer_hint_ladder=False,
        is_timed=True,
    ),
}


def rules_for(mode: FeedbackMode) -> FeedbackRules:
    """Return the permission set for a mode."""
    return _RULES[mode]


def resolve_mode(
    *,
    source_default_mode: FeedbackMode | None,
    work_will_be_graded_by_someone_else: bool,
    parent_override: FeedbackMode | None = None,
) -> FeedbackMode:
    """Decide the feedback mode for one assignment.

    Precedence: an explicit parent override wins, then the graded flag, then the
    content source default, then the restrictive fallback. A student can never
    change this; only a parent-authenticated action can.
    """
    if parent_override is not None:
        return parent_override
    if work_will_be_graded_by_someone_else:
        return FeedbackMode.DIAGNOSTIC_ONLY
    if source_default_mode is not None:
        return source_default_mode
    return FeedbackMode.DIAGNOSTIC_ONLY
