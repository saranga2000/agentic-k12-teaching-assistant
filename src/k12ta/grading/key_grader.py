"""Grade against a stored answer key.

This is the high-confidence path and should be preferred wherever a key exists.
It never consults a model for the verdict; the model only supplies the transcription
and, separately, the diagnosis of an already-established error.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fractions import Fraction
from typing import Protocol, TypeVar

from k12ta.domain.models import GradeOutcome

_WHITESPACE = re.compile(r"\s+")

CONFIDENCE_FLOOR = 0.95
"""Not 0.85. The 2026-08-12 transcription eval
(evals/results/2026-08-12-0825-vision_llm.md) measured calibration by confidence band:
the 0.85-0.95 band was only 60% accurate (n=5), while the 0.95-1.01 band was 100%
accurate (n=13). A floor of 0.85 would admit that 0.85-0.95 band and grade wrong
answers as CORRECT or INCORRECT with confidence the data does not support.

The single source of truth for this number: anything else in the pipeline that needs
to know whether a transcription is trustworthy enough to act on (e.g. the M2.3
rendering distinction between "I could not read this one clearly" and "I don't have an
answer key for this one yet") imports this constant rather than restating 0.95."""


def normalise(answer: str) -> str:
    """Canonicalise an answer string for comparison.

    Deliberately conservative. Ambiguity resolves to a mismatch, which routes to
    NEEDS_HUMAN rather than to a wrong mark against the student.
    """
    text = answer.strip().lower()
    text = text.replace("−", "-").replace("×", "*").replace("÷", "/")
    text = _WHITESPACE.sub("", text)
    return text.rstrip(".")


_NUMERIC_PREFIX = re.compile(r"(-?\d+(?:\.\d+)?(?:/-?\d+(?:\.\d+)?)?)(?:\s+[^\d]+)?")


def numeric_part(answer: str) -> str | None:
    """The leading numeric token of an answer, ignoring anything after it --
    a unit, e.g. "496 ft²" -> "496". None if the answer doesn't start with a
    number at all, or if what follows isn't unambiguously a separate unit.

    Three things must hold for "the rest" to count as an ignorable unit
    rather than part of the answer:

    1. It's separated from the number by whitespace. "28y" (a real algebra
       key on this project's own page 61 -- "simplify 4(7y)") is answer
       *"28y"*, not "28" with a unit "y"; a bare "28" against that key is a
       genuinely incomplete answer, not a match. Real units in this corpus's
       key data are always space-separated ("496 ft²", "15 cm"), so requiring
       the space costs nothing real and closes this gap.
    2. The number itself was matched in full. A key like "2,122.64 m²" (page
       33, thousands-comma) isn't parsed by this grammar -- greedily matching
       "2" and calling ",122.64 m²" the "unit" would silently truncate the
       real value. Comma-grouped numbers are out of scope here the same way
       fraction-vs-decimal is (see fraction_value); this function returns
       None rather than a value it can't be sure is complete.
    3. What follows the space contains no digit anywhere. A real unit label
       never does ("ft²", "cm", "times", "ounces"). A key like "23 3/5" (a
       real mixed number, page 17) or "4 out of 25, or about 1 in 6" (a real
       prose answer, page 21) both have a digit right there in what would
       otherwise look like "the rest" -- treating either as "23" or "4" with
       a throwaway unit would silently drop part of the real answer.

    All three were caught by testing this function against every real key
    answer already in the production database before wiring anything up to
    it. Whenever any of them fails, this returns None rather than a guessed
    value -- an answer against that key falls through to
    ANSWER_DIFFERS_FROM_KEY instead of a wrong auto-grade, same "ambiguity
    resolves to asking a person" rule as `normalise`.
    """
    text = answer.strip().replace("−", "-")
    match = _NUMERIC_PREFIX.fullmatch(text)
    return match.group(1) if match else None


def looks_numeric(answer: str) -> bool:
    """Whether a key's answer is a number, optionally followed by a unit
    (e.g. "496 ft²") -- the one answer shape where exact-string matching (on
    the number; see `numeric_part`) is sound, because a numeric key has
    exactly one correct value, whatever it's labelled with. Everything else
    (a shape name, a term, a letter code) can have more than one valid
    spelling for the same right answer, so a mismatch there means "different
    from the key", not "wrong" -- see
    k12ta.grading.needs_human.NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY.
    """
    return numeric_part(answer) is not None


_FRACTION = re.compile(r"(-?\d+)\s*/\s*(-?\d+)")


def fraction_value(answer: str) -> Fraction | None:
    """The exact rational value of an answer that is a bare fraction and
    nothing else (e.g. "2/6" -> Fraction(1, 3)). None for anything else --
    a decimal, a fraction with a unit, free text, or a fraction with a zero
    denominator.

    Deliberately narrower than `numeric_part`: this exists only to tell two
    different-looking fractions with the same value apart from two fractions
    that are genuinely different (k12ta.grading.needs_human.decide's
    unsimplified-fraction check), not as a general numeric-equivalence
    engine -- "3/4" vs "0.75" is a different generalisation and stays out of
    scope on purpose.
    """
    text = answer.strip().replace("−", "-")
    match = _FRACTION.fullmatch(text)
    if match is None:
        return None
    denominator = int(match.group(2))
    if denominator == 0:
        return None
    return Fraction(int(match.group(1)), denominator)


def normalise_problem_id(value: str) -> str:
    """Canonicalise a problem identifier for comparison across the two source
    documents a page's grade depends on. Both `prompts/transcribe_page.md` and
    `prompts/transcribe_key_page.md` say "as printed", and both transcriptions
    are faithful to that -- the workbook page prints "5.", the key page prints
    "5". Storage stays faithful to each source document (the right invariant);
    this is where the two get reconciled, at comparison time.

    Deliberately narrow, matching `normalise` above: strips only a trailing
    period, surrounding whitespace, and case. Nothing cleverer -- "5a" and "5"
    are different problems and must never collapse to the same one.
    """
    return value.strip().rstrip(".").lower()


class _KeyEntryIdentifier(Protocol):
    @property
    def problem_number(self) -> str: ...


_KeyEntryT = TypeVar("_KeyEntryT", bound=_KeyEntryIdentifier)


def find_key_entry(entries: Sequence[_KeyEntryT], problem_id: str) -> _KeyEntryT | None:
    """Find the entry among a page's key entries matching a transcribed
    problem_id, comparing with `normalise_problem_id` rather than the exact
    string stored -- see its docstring for why the two sides disagree on
    trailing periods."""
    target = normalise_problem_id(problem_id)
    for entry in entries:
        if normalise_problem_id(entry.problem_number) == target:
            return entry
    return None


def grade_against_key(
    student_answer: str,
    key_answer: str,
    transcription_confidence: float,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> GradeOutcome:
    """Compare one answer to the key.

    A low-confidence transcription can never produce INCORRECT. A confidently wrong
    mark costs far more trust than an escalation to a grown-up. See `CONFIDENCE_FLOOR`
    for why the default is 0.95.
    """
    if transcription_confidence < confidence_floor:
        return GradeOutcome.NEEDS_HUMAN
    if normalise(student_answer) == normalise(key_answer):
        return GradeOutcome.CORRECT
    key_number = numeric_part(key_answer)
    if key_number is not None and numeric_part(student_answer) == key_number:
        # Same number, different (or missing) unit label on either side --
        # never a unit conversion, just the number compared with the label
        # ignored. See numeric_part.
        return GradeOutcome.CORRECT
    if not student_answer.strip():
        return GradeOutcome.NEEDS_HUMAN
    return GradeOutcome.INCORRECT
