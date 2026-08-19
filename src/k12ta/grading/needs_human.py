"""Determining why a problem is flagged needs-human.

This is the single place cause determination lives -- never in a view layer, which
must only *render* a cause it is handed. The causes are deliberately distinct and
honest about what is and is not known:

- `LOW_CONFIDENCE`: the transcription could not be trusted enough to grade even
  against a key that exists.
- `UNKNOWN_PAGE`: no page number was supplied, so no key could even be looked up.
- `NO_KEY_FOR_PAGE`: the page is known but no key covers it (or covers this item).
- `NEEDS_PERSON`: a key entry exists but is explicitly ungradeable, or the student's
  answer leaves nothing to confidently compare.
- `CONFLICTING_PAGE_MARKERS`: `k12ta.grading.page_identity` saw two different values
  for some identity component on one photo (e.g. two "Day N" banners on a
  two-page spread) and refused to pick one. Decided upstream of `decide` below, not
  here -- by the time `decide` runs, page identity is already resolved-or-not, and
  `decide` only ever produces `UNKNOWN_PAGE` for "not," never this cause. Kept as
  its own enum value rather than folded into `UNKNOWN_PAGE`'s copy, on purpose: the
  fix for a parent is different (photograph one page at a time, not "wait for a
  key"), and the eval harness needs to count how often each actually fires.
- `ANSWER_DIFFERS_FROM_KEY`: the student's answer doesn't exactly match a
  non-numeric key entry. Deliberately not INCORRECT: a name that differs from
  the key isn't necessarily wrong (a rhombus is a quadrilateral; a square is a
  rectangle), and telling them apart needs either a taxonomy or a model judging
  equivalence -- both unmeasured confidence in exactly the place a wrong mark
  costs the most, so this asks a person instead of guessing. Numeric keys don't
  get this treatment: a number has exactly one correct value, so a mismatch
  there is still INCORRECT (see `k12ta.grading.key_grader.looks_numeric`).
- `PARTIAL_PAGE_MARKERS`: `k12ta.grading.page_identity` read some but not all of
  this source's identity components on one photo (e.g. the day banner but not the
  section marker). Also decided upstream of `decide`, for the same reason as
  `CONFLICTING_PAGE_MARKERS` above, and also its own cause rather than folded into
  `UNKNOWN_PAGE`: unlike a page with no marker at all, this is recoverable by
  re-photographing with the missing part in frame, and the message can say so
  specifically because `k12ta.grading.page_identity.PageIdentityResolution` names
  which components were seen and which were missing.

`NEEDS_PERSON` deliberately bundles two situations that genuinely both need a
person: the key says the answer varies, and the answer field is empty. No separate
cause is invented for the blank case -- labelling a blank "low confidence" when the
model is confident would be a different lie.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from k12ta.domain.models import GradeOutcome
from k12ta.grading.key_grader import (
    CONFIDENCE_FLOOR,
    fraction_value,
    grade_against_key,
    looks_numeric,
)


class NeedsHumanCause(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    """Transcription below the confidence floor; could not read the writing."""

    UNKNOWN_PAGE = "unknown_page"
    """No page number supplied; no key could even be looked up for it."""

    NO_KEY_FOR_PAGE = "no_key_for_page"
    """Page known, but no confirmed key entry covers it (or this item)."""

    NEEDS_PERSON = "needs_person"
    """Key marks the item ungradeable, or the answer leaves nothing to compare."""

    CONFLICTING_PAGE_MARKERS = "conflicting_page_markers"
    """Two different values seen for some identity component on one photo --
    decided by k12ta.grading.page_identity, never by decide() below."""

    PARTIAL_PAGE_MARKERS = "partial_page_markers"
    """Some but not all of this source's identity components were seen on one
    photo -- decided by k12ta.grading.page_identity, never by decide() below."""

    ANSWER_DIFFERS_FROM_KEY = "answer_differs_from_key"
    """A non-numeric answer that doesn't exactly match the key -- may still be
    right under a different valid name. See the module docstring above."""


@dataclass(frozen=True)
class GradeDecision:
    outcome: GradeOutcome
    needs_human_cause: NeedsHumanCause | None = None
    """Set to the cause when `outcome` is NEEDS_HUMAN; None for a definite grade."""

    expected_answer: str | None = None
    """The confirmed key answer, surfaced so the renderer can show it if useful."""

    unsimplified: bool = False
    """Set only when outcome is CORRECT and the match required comparing
    fraction values, not exact strings (e.g. "2/6" against a key of "1/3") --
    numerically right, but not in lowest terms. Not INCORRECT (the value is
    right) and not a bare CORRECT either (many workbooks ask to simplify),
    so the renderer gets to say both without a new outcome value."""


class KeyEntry(Protocol):
    """The slice of a confirmed answer-key entry `decide` relies on. Deliberately a
    Protocol rather than a concrete store type so the grading logic never depends on
    the persistence layer. Read-only properties, not plain attributes: the real
    implementation (`k12ta.store.answer_keys.AnswerKeyEntryRow`) is a frozen
    dataclass, and a settable-attribute Protocol member rejects a read-only field
    under mypy strict."""

    @property
    def answer_text(self) -> str | None: ...

    @property
    def ungradeable_reason(self) -> str | None: ...


def decide(
    student_answer: str,
    transcription_confidence: float,
    page_number: int | None,
    key_entry: KeyEntry | None,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> GradeDecision:
    """Choose an outcome and, when needs-human, an honest cause.

    Order matters and is tested: a low-confidence transcription is its own cause even
    when a key exists (a confident wrong grade is the worst failure this system
    avoids). Only once confidence, page identity, and a real key answer are all
    present do we call `grade_against_key` for a definite CORRECT/INCORRECT mark.
    """
    if transcription_confidence < confidence_floor:
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN, needs_human_cause=NeedsHumanCause.LOW_CONFIDENCE
        )
    if page_number is None:
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN, needs_human_cause=NeedsHumanCause.UNKNOWN_PAGE
        )
    if key_entry is None:
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN, needs_human_cause=NeedsHumanCause.NO_KEY_FOR_PAGE
        )
    if key_entry.ungradeable_reason is not None:
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN, needs_human_cause=NeedsHumanCause.NEEDS_PERSON
        )
    key_answer = key_entry.answer_text
    if key_answer is None:
        # CHECK constraint forbids both being unset, so reaching here means the
        # entry had no real answer and no ungradeable reason -- treat as needing a
        # person rather than inventing a key.
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN, needs_human_cause=NeedsHumanCause.NEEDS_PERSON
        )

    outcome = grade_against_key(
        student_answer, key_answer, transcription_confidence, confidence_floor
    )
    if outcome is GradeOutcome.NEEDS_HUMAN:
        # At this point grade_against_key only escalates a blank/unclear answer:
        # confidence is at/above the floor and a real key answer exists, so there is
        # nothing to confidently compare -- a person should judge.
        return GradeDecision(
            outcome=GradeOutcome.NEEDS_HUMAN,
            needs_human_cause=NeedsHumanCause.NEEDS_PERSON,
            expected_answer=key_answer,
        )
    if outcome is GradeOutcome.INCORRECT:
        key_fraction = fraction_value(key_answer)
        if key_fraction is not None and fraction_value(student_answer) == key_fraction:
            # Same value, different reduction ("2/6" vs "1/3") -- not wrong,
            # but not silently indistinguishable from a fully-simplified
            # answer either. See GradeDecision.unsimplified.
            return GradeDecision(
                outcome=GradeOutcome.CORRECT, expected_answer=key_answer, unsimplified=True
            )
        if not looks_numeric(key_answer):
            # A name that differs from the key isn't necessarily wrong -- only
            # a numeric key has exactly one correct value. See
            # ANSWER_DIFFERS_FROM_KEY.
            return GradeDecision(
                outcome=GradeOutcome.NEEDS_HUMAN,
                needs_human_cause=NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY,
                expected_answer=key_answer,
            )
    return GradeDecision(outcome=outcome, expected_answer=key_answer)
