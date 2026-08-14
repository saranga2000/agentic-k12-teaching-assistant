"""M3.2b: k12ta.domain.attempts decides how many genuine attempts a student has
made at one problem -- pure, no I/O, tested against plain lists."""

from __future__ import annotations

from k12ta.domain.attempts import PastAttempt, already_disclosed, attempt_number


def test_first_ever_attempt_is_never_disclosed() -> None:
    assert attempt_number([], "14") == 1
    assert not already_disclosed([], "14")


def test_a_differing_second_answer_is_a_new_attempt_and_is_disclosed() -> None:
    prior = [PastAttempt(outcome="incorrect", student_answer_raw="14")]

    assert attempt_number(prior, "-14") == 2
    assert already_disclosed(prior, "-14")


def test_an_unchanged_resubmission_is_not_a_new_attempt() -> None:
    """The whole-page-recapture case: resubmitting the same answer (the problem
    she didn't revise) must not advance the count or trigger suppression."""
    prior = [PastAttempt(outcome="incorrect", student_answer_raw="14")]

    assert attempt_number(prior, "14") == 1
    assert not already_disclosed(prior, "14")


def test_needs_human_between_two_real_attempts_is_invisible_to_the_comparison() -> None:
    """A blurry retake is free: it neither burns the exempt first attempt nor
    breaks the same-answer comparison against the last real graded attempt."""
    prior = [
        PastAttempt(outcome="incorrect", student_answer_raw="14"),
        PastAttempt(outcome="needs_human", student_answer_raw="???"),
    ]

    # Resubmitting the same real answer after a blurry photo is still no new attempt.
    assert attempt_number(prior, "14") == 1
    assert not already_disclosed(prior, "14")

    # A genuinely new guess after the blurry photo still counts as attempt two.
    assert attempt_number(prior, "-14") == 2
    assert already_disclosed(prior, "-14")


def test_needs_human_as_the_only_prior_attempt_does_not_burn_the_exemption() -> None:
    prior = [PastAttempt(outcome="needs_human", student_answer_raw="???")]

    assert attempt_number(prior, "14") == 1
    assert not already_disclosed(prior, "14")


def test_reverting_to_an_earlier_answer_still_counts_as_a_new_attempt() -> None:
    """Documents the chosen tie-break: comparison is against the most recent
    distinct guess, not the full history, so going back to an old guess is
    treated as a new probe rather than a free pass."""
    prior = [
        PastAttempt(outcome="incorrect", student_answer_raw="14"),
        PastAttempt(outcome="incorrect", student_answer_raw="-14"),
    ]

    assert attempt_number(prior, "14") == 3
    assert already_disclosed(prior, "14")


def test_a_correct_first_attempt_is_still_not_disclosed_as_a_repeat() -> None:
    """A single confirmed correct answer isn't a probing sequence -- nothing to
    suppress on attempt one, regardless of outcome."""
    assert not already_disclosed([], "19")
