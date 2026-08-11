from __future__ import annotations

from k12ta.domain.models import GradeOutcome
from k12ta.grading.key_grader import grade_against_key, normalise


def test_normalisation_handles_unicode_minus_and_spacing() -> None:
    assert normalise(" −14 ") == normalise("-14")


def test_low_confidence_never_produces_an_incorrect_mark() -> None:
    assert (
        grade_against_key("14", "-14", transcription_confidence=0.4) is GradeOutcome.NEEDS_HUMAN
    )


def test_blank_answer_escalates_rather_than_failing_the_student() -> None:
    assert grade_against_key("", "-14", transcription_confidence=0.99) is GradeOutcome.NEEDS_HUMAN


def test_confident_match_is_correct() -> None:
    assert grade_against_key("-14", "-14", transcription_confidence=0.99) is GradeOutcome.CORRECT


def test_confident_mismatch_is_incorrect() -> None:
    assert grade_against_key("14", "-14", transcription_confidence=0.99) is GradeOutcome.INCORRECT
