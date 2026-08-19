from __future__ import annotations

from fractions import Fraction

from k12ta.domain.models import GradeOutcome
from k12ta.grading.key_grader import (
    find_key_entry,
    fraction_value,
    grade_against_key,
    looks_numeric,
    normalise,
    normalise_problem_id,
    numeric_part,
)
from k12ta.grading.needs_human import NeedsHumanCause, decide


def test_normalisation_handles_unicode_minus_and_spacing() -> None:
    assert normalise(" −14 ") == normalise("-14")


# --- numeric-answer detection (which key answers exact-match can trust) -----


def test_looks_numeric_accepts_negative_and_decimal() -> None:
    assert looks_numeric("-14")
    assert looks_numeric(" 3.5 ")
    assert looks_numeric("−3.5")  # unicode minus, same as normalise handles


def test_looks_numeric_accepts_a_simple_fraction() -> None:
    assert looks_numeric("3/4")
    assert looks_numeric("-3/4")


def test_looks_numeric_rejects_free_text() -> None:
    assert not looks_numeric("quadrilateral")
    assert not looks_numeric("rectangle")
    assert not looks_numeric("N")  # a multiple-choice letter code, not a number


# --- numeric_part: a number followed by a unit is still numeric ("496 ft²") --


def test_numeric_part_strips_a_trailing_unit() -> None:
    assert numeric_part("496 ft²") == "496"
    assert numeric_part("3.5 cm") == "3.5"
    assert numeric_part("-3.5 kg") == "-3.5"


def test_numeric_part_is_unchanged_for_a_bare_number() -> None:
    assert numeric_part("496") == "496"
    assert numeric_part("-3.5") == "-3.5"


def test_numeric_part_is_none_for_free_text() -> None:
    assert numeric_part("quadrilateral") is None


def test_numeric_part_requires_a_space_before_the_unit() -> None:
    # "28y" (a real key on this project's own page 61, "simplify 4(7y)") is the
    # answer "28y", not "28" with a unit "y" -- a bare "28" against that key is
    # genuinely incomplete, not a match. Every real unit in this project's key
    # data is space-separated, so requiring the space loses nothing real.
    assert numeric_part("28y") is None
    assert numeric_part("-2c") is None


def test_numeric_part_rejects_a_thousands_comma_rather_than_truncate() -> None:
    # A real key on this project's own page 33. Greedily matching "2" and
    # calling ",122.64 m²" the "unit" would silently turn the key into the
    # wrong number -- must refuse rather than guess.
    assert numeric_part("2,122.64 m²") is None


def test_numeric_part_rejects_a_mixed_number_rather_than_drop_the_fraction() -> None:
    # A real key on this project's own page 17. "3/5" here is the rest of the
    # value (23 and 3/5), not a unit -- must not truncate to "23".
    assert numeric_part("23 3/5") is None
    assert numeric_part("-1 3/27") is None


def test_numeric_part_rejects_a_digit_anywhere_in_the_trailing_text() -> None:
    # A real key on this project's own page 21: a prose answer that happens to
    # start with a number. "out of 25, or about 1 in 6" has digits throughout
    # -- not a unit label, must not truncate to "4".
    assert numeric_part("4 out of 25, or about 1 in 6") is None


def test_looks_numeric_accepts_a_number_with_a_unit() -> None:
    assert looks_numeric("496 ft²")


def test_grade_against_key_ignores_a_unit_difference_when_the_number_matches() -> None:
    # Key has a unit, student doesn't.
    assert grade_against_key("496", "496 ft²", transcription_confidence=0.99) is (
        GradeOutcome.CORRECT
    )
    # Student adds a unit the key doesn't have.
    assert grade_against_key("496 sq ft", "496", transcription_confidence=0.99) is (
        GradeOutcome.CORRECT
    )


def test_grade_against_key_still_incorrect_when_the_number_itself_differs() -> None:
    assert (
        grade_against_key("500", "496 ft²", transcription_confidence=0.99) is GradeOutcome.INCORRECT
    )


def test_grade_against_key_does_not_credit_a_bare_number_against_an_algebra_key() -> None:
    # "28" is missing the variable against a key of "28y" -- genuinely
    # incomplete, must not be credited as "same number, different unit".
    assert grade_against_key("28", "28y", transcription_confidence=0.99) is GradeOutcome.INCORRECT


# --- fraction value equivalence: "2/6" and "1/3" are the same number ---------


def test_fraction_value_parses_a_pure_fraction() -> None:
    assert fraction_value("2/6") == Fraction(1, 3)
    assert fraction_value("1/3") == Fraction(1, 3)
    assert fraction_value("-2/4") == Fraction(-1, 2)


def test_fraction_value_rejects_a_fraction_with_a_unit_or_other_text() -> None:
    # Deliberately narrow: unit handling and fraction-value handling don't mix.
    assert fraction_value("3/4 cup") is None
    assert fraction_value("quadrilateral") is None


def test_fraction_value_rejects_division_by_zero() -> None:
    assert fraction_value("4/0") is None


# --- problem-id normalisation (workbook prints "5.", the key prints "5") ----


def test_normalise_problem_id_strips_trailing_period_whitespace_and_case() -> None:
    assert normalise_problem_id(" 5. ") == normalise_problem_id("5")


def test_normalise_problem_id_does_not_collapse_a_lettered_suffix() -> None:
    # "5a" and "5" are different problems and must not be treated as a match.
    assert normalise_problem_id("5a") != normalise_problem_id("5")


def _key_entry(problem_number: str) -> object:
    """A minimal stand-in exposing just the field find_key_entry reads."""
    return type("Entry", (), {"problem_number": problem_number})()


def test_find_key_entry_matches_workbook_period_against_key_without_one() -> None:
    # The real asymmetry: student side transcribes "5.", the key was entered as "5".
    entries = [_key_entry("5")]
    found = find_key_entry(entries, "5.")
    assert found is not None
    assert found.problem_number == "5"


def test_find_key_entry_does_not_match_a_different_lettered_problem() -> None:
    entries = [_key_entry("5")]
    assert find_key_entry(entries, "5a") is None


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


# --- non-numeric mismatch: a name that differs from the key isn't necessarily
# wrong (a rhombus is a quadrilateral), so exact-match alone can't call it
# INCORRECT the way it safely can for a number. --------------------------------


def test_numeric_key_mismatch_is_still_incorrect() -> None:
    decision = decide("99", transcription_confidence=0.99, page_number=7, key_entry=_key("42"))
    assert decision.outcome is GradeOutcome.INCORRECT
    assert decision.needs_human_cause is None


def test_non_numeric_key_mismatch_escalates_instead_of_marking_incorrect() -> None:
    decision = decide(
        "rhombus", transcription_confidence=0.99, page_number=15, key_entry=_key("quadrilateral")
    )
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY
    assert decision.expected_answer == "quadrilateral"


def test_comma_grouped_number_key_escalates_rather_than_truncating() -> None:
    # A real key on this project's own page 33 ("2,122.64 m²"). numeric_part
    # refuses to guess at a truncated value, so this key counts as non-numeric
    # here too -- any mismatch asks a person instead of comparing against "2".
    decision = decide(
        "5", transcription_confidence=0.99, page_number=33, key_entry=_key("2,122.64 m²")
    )
    assert decision.outcome is GradeOutcome.NEEDS_HUMAN
    assert decision.needs_human_cause is NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY


def test_non_numeric_exact_match_is_still_correct() -> None:
    decision = decide(
        "quadrilateral",
        transcription_confidence=0.99,
        page_number=15,
        key_entry=_key("quadrilateral"),
    )
    assert decision.outcome is GradeOutcome.CORRECT


# --- unsimplified fraction: numerically right, not in lowest terms -- CORRECT,
# flagged, never silently indistinguishable from a fully-reduced answer --------


def test_unreduced_fraction_is_correct_but_flagged_unsimplified() -> None:
    decision = decide("2/6", transcription_confidence=0.99, page_number=21, key_entry=_key("1/3"))
    assert decision.outcome is GradeOutcome.CORRECT
    assert decision.needs_human_cause is None
    assert decision.unsimplified is True


def test_already_reduced_fraction_match_is_not_flagged() -> None:
    decision = decide("1/3", transcription_confidence=0.99, page_number=21, key_entry=_key("1/3"))
    assert decision.outcome is GradeOutcome.CORRECT
    assert decision.unsimplified is False


def test_genuinely_wrong_fraction_is_still_incorrect_not_flagged() -> None:
    decision = decide("1/4", transcription_confidence=0.99, page_number=21, key_entry=_key("1/3"))
    assert decision.outcome is GradeOutcome.INCORRECT
    assert decision.unsimplified is False


def test_non_numeric_correct_mark_is_never_flagged_unsimplified() -> None:
    decision = decide(
        "quadrilateral",
        transcription_confidence=0.99,
        page_number=15,
        key_entry=_key("quadrilateral"),
    )
    assert decision.unsimplified is False
