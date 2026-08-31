"""How many genuine attempts a student has made at one problem.

Confirming or denying a guess is safe in isolation, but a sequence of them is an
oracle: wrong, wrong, right reveals the answer as surely as stating it, even
though every individual response was honest and within policy. This module
decides where that line falls, deliberately kept free of I/O (same ethos as
`k12ta.domain.policy`) so it can be tested against plain lists of past outcomes,
no database required.

Two rules:

- NEEDS_HUMAN never counts. An unreadable photo or a page the coach could not
  resolve taught the student nothing, and must not burn her one exempt attempt --
  a blurry retake has to be free.
- A resubmission with an unchanged answer is not a new attempt. Photographing a
  whole page again after revising only one problem on it resends every other
  problem's answer unchanged; only the one that actually changed should ever
  count as a new guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_GRADED_OUTCOMES = frozenset({"correct", "partially_correct", "incorrect"})
"""Outcomes that count as a real, disclosed grade for oracle-suppression purposes.
`partially_correct` (docs/ROADMAP.md's V1 "Verdicts") is disclosed to the student
exactly like correct/incorrect, so it must count here too -- leaving it out would
reopen the multi-attempt oracle for every answer M6's evaluator judges partial."""


@dataclass(frozen=True)
class PastAttempt:
    outcome: str
    student_answer_raw: str


def _logical_attempt_count(answers_in_order: Sequence[str]) -> int:
    """Chronological submitted answers -> number of genuinely distinct guesses,
    collapsing a resubmission of the same answer into the attempt it repeats.
    Each answer is compared only to the most recent distinct one, so reverting
    to an earlier guess still counts as a new attempt -- a deliberately
    conservative tie-break, cheap and consistent with this module's fail-closed
    sibling in k12ta.domain.policy."""
    count = 0
    last: str | None = None
    for answer in answers_in_order:
        if count == 0 or answer != last:
            count += 1
        last = answer
    return count


def attempt_number(past_attempts: Sequence[PastAttempt], current_answer: str) -> int:
    """past_attempts: every earlier graded_problems row for this exact problem
    identity (student, source, page, problem_id), already in chronological
    order, EXCLUDING the attempt being classified now. This function does not
    sort -- callers own ordering, so a bug there fails loudly instead of being
    silently masked here."""
    graded_answers = [a.student_answer_raw for a in past_attempts if a.outcome in _GRADED_OUTCOMES]
    return _logical_attempt_count([*graded_answers, current_answer])


def already_disclosed(past_attempts: Sequence[PastAttempt], current_answer: str) -> bool:
    """True from the second genuinely distinct guess onward -- the point past
    which confirming or denying correctness becomes an oracle, so the response
    must say the same thing regardless of whether this guess happens to be
    right."""
    return attempt_number(past_attempts, current_answer) > 1
