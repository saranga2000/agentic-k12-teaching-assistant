from __future__ import annotations

from k12ta.domain.models import GradeOutcome
from k12ta.grading.key_grader import grade_against_key, normalise
from k12ta.grading.needs_human import NeedsHumanCause, decide


def test_normalisation_handles_unicode_minus_and_spacing() -> None:
    assert normalise(" −14 ") == normalise("-14")


def test_low_confidence_never_produces_an_incorrect_mark() -> None:
    assert grade_against_key("14", "-14", transcription_confidence=0.4) is GradeOutcome.NEEDS_HUMAN


def test_blank_answer_escalates_rather_than_failing_the_student() -> None:
    assert grade_against_key("", "-14", transcription_confidence=0.99) is GradeOutcome.NEEDS_HUMAN


def test_confident_match_is_correct() -> None:
    assert grade_against_key("-14", "-14", transcription_confidence=0.99) is GradeOutcome.CORRECT


def test_confident_mismatch_is_incorrect() -> None:
    assert grade_against_key("14", "-14", transcription_confidence=0.99) is GradeOutcome.INCORRECT


# --- needs-human cause decision (Scope A) ------------------------------------


def _key(answer: str | None = "42", ungradeable: str | None = None) -> object:
    """A minimal stand-in for an AnswerKeyEntryRow, exposing just the two fields
    `decide` is allowed to read (answer_text, ungradeable_reason)."""
    return type("Key", (), {"answer_text": answer, "ungradeable_reason": ungradeable})()


def test_low_confidence_is_its_own_honest_cause_even_when_a_key_exists() -> None:
    decision = decide("42", transcription_confidence=0.4, page_number=5, key_entry=_key("42"))
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.LOW_CONFIDENCE


def test_unknown_page_is_stated_not_guessed() -> None:
    decision = decide("42", transcription_confidence=0.99, page_number=None, key_entry=None)
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.UNKNOWN_PAGE


def test_known_page_with_no_key_entry_is_no_key_for_page() -> None:
    decision = decide("42", transcription_confidence=0.99, page_number=7, key_entry=None)
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.NO_KEY_FOR_PAGE


def test_key_marking_ungradeable_routes_to_needs_a_person() -> None:
    for reason in ("answers_vary", "graph_or_table"):
        decision = decide(
            "42",
            transcription_confidence=0.99,
            page_number=7,
            key_entry=_key(answer=None, ungradeable=reason),
        )
        assert decision.outcome is GradeOutcome.NEEDS_HUMAN
        assert decision.needs_human_cause is NeedsHumanCause.NEEDS_PERSON


def test_real_key_correct_mark_has_no_cause() -> None:
    decision = decide("42", transcription_confidence=0.99, page_number=7, key_entry=_key("42"))
    assert decision.outcome is GradeOutcome.CORRECT
    assert decision.needs_human_cause is None


def test_real_key_wrong_mark_has_no_cause() -> None:
    decision = decide("99", transcription_confidence=0.99, page_number=7, key_entry=_key("42"))
    assert decision.outcome is GradeOutcome.INCORRECT
    assert decision.needs_human_cause is None


def test_blank_answer_with_real_key_still_needs_a_person() -> None:
    decision = decide("", transcription_confidence=0.99, page_number=7, key_entry=_key("42"))
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.NEEDS_PERSON
