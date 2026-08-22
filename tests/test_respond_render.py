"""M3.2: k12ta.respond.render is the only place a GradedProblemRow becomes text a
student sees, and the filter (FeedbackRules) cannot be forgotten -- it is a
required, keyword-only, no-default parameter of render_student_result.

M3.2b: neither can a problem's attempt history (prior_attempts), for the same
reason -- a caller that forgot it would silently behave as if every attempt were
the first, which is exactly the multi-attempt oracle it exists to close."""

from __future__ import annotations

import inspect

import pytest

from k12ta.domain.attempts import PastAttempt
from k12ta.domain.policy import FeedbackMode, rules_for
from k12ta.pipeline.process import AMBIGUOUS_PROBLEM_ID_PREFIX
from k12ta.respond.render import (
    AMBIGUOUS_PROBLEM_ID_MESSAGE,
    StudentResultView,
    render_student_result,
    summarize_results,
)
from k12ta.store.sessions import GradedProblemRow

_FULL = rules_for(FeedbackMode.FULL)
_DIAGNOSTIC_ONLY = rules_for(FeedbackMode.DIAGNOSTIC_ONLY)
_FLUENCY = rules_for(FeedbackMode.FLUENCY)


def _row(**overrides: object) -> GradedProblemRow:
    base = dict(
        student_id="s-1",
        session_id="sess-1",
        capture_id="c-1",
        problem_id="1",
        outcome="incorrect",
        grader_confidence=0.99,
        expected_answer="42",
    )
    base.update(overrides)
    return GradedProblemRow(**base)  # type: ignore[arg-type]


def test_rules_parameter_is_required_and_keyword_only_with_no_default() -> None:
    """The structural guarantee: a caller with no FeedbackRules in scope cannot
    call this function at all -- not merely a lint warning."""
    sig = inspect.signature(render_student_result)
    rules_param = sig.parameters["rules"]

    assert rules_param.default is inspect.Parameter.empty
    assert rules_param.kind is inspect.Parameter.KEYWORD_ONLY


def test_prior_attempts_parameter_is_required_and_keyword_only_with_no_default() -> None:
    sig = inspect.signature(render_student_result)
    prior_param = sig.parameters["prior_attempts"]

    assert prior_param.default is inspect.Parameter.empty
    assert prior_param.kind is inspect.Parameter.KEYWORD_ONLY


def test_calling_without_rules_raises_type_error() -> None:
    with pytest.raises(TypeError):
        render_student_result(_row(), "12 + 7", "18", prior_attempts=())  # type: ignore[call-arg]


def test_calling_without_prior_attempts_raises_type_error() -> None:
    with pytest.raises(TypeError):
        render_student_result(_row(), "12 + 7", "18", rules=_FULL)  # type: ignore[call-arg]


def test_correct_outcome_says_correct_regardless_of_mode() -> None:
    for rules in (_FULL, _DIAGNOSTIC_ONLY, _FLUENCY):
        view = render_student_result(
            _row(outcome="correct"), "12 + 7", "19", rules=rules, prior_attempts=()
        )
        assert view.message == "Correct!"


def test_unsimplified_correct_answer_says_so_and_still_counts_as_correct() -> None:
    """ "2/6" matched a key of "1/3" by value, not by string
    (k12ta.grading.needs_human.decide) -- numerically right, so outcome stays
    "correct" (mastery, oracle suppression, everything downstream sees a real
    correct mark), but the message says it's unreduced rather than looking
    identical to a fully-simplified answer."""
    view = render_student_result(
        _row(outcome="correct", unsimplified=True),
        "What fraction is shaded?",
        "2/6",
        rules=_FULL,
        prior_attempts=(),
    )
    assert view.outcome == "correct"
    assert view.glyph == "✓"
    assert "simplif" in view.message.lower()
    assert view.message != "Correct!"


def test_incorrect_in_full_mode_reveals_the_expected_answer() -> None:
    view = render_student_result(
        _row(expected_answer="42"), "12 + 7", "18", rules=_FULL, prior_attempts=()
    )

    assert "42" in view.message


@pytest.mark.parametrize("rules", [_DIAGNOSTIC_ONLY, _FLUENCY])
def test_incorrect_in_restricted_modes_never_reveals_the_expected_answer(
    rules: object,
) -> None:
    view = render_student_result(
        _row(expected_answer="42"),
        "12 + 7",
        "18",
        rules=rules,  # type: ignore[arg-type]
        prior_attempts=(),
    )

    assert "42" not in view.message


def test_needs_human_message_is_identical_across_every_mode() -> None:
    """The needs-human causes are honest in every feedback mode -- the branch
    that produces their copy never reads rules."""
    row = _row(outcome="needs_human", needs_human_cause="low_confidence", expected_answer=None)

    messages = {
        render_student_result(row, "12 + 7", "18", rules=rules, prior_attempts=()).message
        for rules in (_FULL, _DIAGNOSTIC_ONLY, _FLUENCY)
    }

    assert len(messages) == 1


def test_needs_human_message_does_not_change_with_expected_answer_present() -> None:
    """Even when an expected_answer happens to be set on a needs_human row, the
    needs_human message never surfaces it."""
    row = _row(outcome="needs_human", needs_human_cause="low_confidence", expected_answer="42")

    view = render_student_result(row, "12 + 7", "18", rules=_FULL, prior_attempts=())

    assert "42" not in view.message


def test_ambiguous_problem_id_says_which_question_not_which_answer() -> None:
    """Actionable, not just a diagnosis: the gap is which question this answer
    belongs to, not whether the writing was legible -- must read differently
    from LOW_CONFIDENCE's "I could not read your writing."."""
    row = _row(outcome="needs_human", needs_human_cause="ambiguous_problem_id")

    view = render_student_result(row, "12 + 7", "19", rules=_FULL, prior_attempts=())

    assert view.message == AMBIGUOUS_PROBLEM_ID_MESSAGE
    assert "question" in view.message.lower()


def test_answer_differs_from_key_shows_both_answers_and_marks_nothing() -> None:
    """The one deliberate exception to the invariant above: this cause exists
    specifically so a name that differs from the key (a rhombus vs.
    "quadrilateral") isn't asserted wrong -- both sides need to be visible so a
    parent can judge, and the message must not imply a verdict either way."""
    row = _row(
        outcome="needs_human",
        needs_human_cause="answer_differs_from_key",
        expected_answer="quadrilateral",
    )

    view = render_student_result(row, "shape?", "rhombus", rules=_FULL, prior_attempts=())

    assert "quadrilateral" in view.message
    assert "rhombus" in (view.student_answer_raw,)  # her own answer, shown as-is
    assert "not" not in view.message.lower()
    assert "wrong" not in view.message.lower()
    assert "incorrect" not in view.message.lower()


def test_view_carries_no_raw_expected_answer_field() -> None:
    """StudentResultView has no expected_answer attribute at all -- a template
    author has nothing sensitive to accidentally render."""
    view = render_student_result(_row(), "12 + 7", "18", rules=_FULL, prior_attempts=())

    assert not hasattr(view, "expected_answer")


# -- Multi-attempt oracle (M3.2b) --------------------------------------------


def test_first_attempt_is_never_suppressed_even_in_a_restricted_mode() -> None:
    view = render_student_result(
        _row(outcome="correct"),
        "12 + 7",
        "19",
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=(),
    )

    assert view.message == "Correct!"
    assert view.outcome == "correct"


def test_second_distinct_guess_correct_is_suppressed_in_a_restricted_mode() -> None:
    prior = (PastAttempt(outcome="incorrect", student_answer_raw="18"),)

    view = render_student_result(
        _row(outcome="correct"),
        "12 + 7",
        "19",  # a new, different guess from the prior "18"
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=prior,
    )

    assert view.message != "Correct!"
    assert view.outcome == "repeat"


def test_suppressed_correct_and_suppressed_incorrect_are_byte_identical() -> None:
    """The direct test of the core requirement: a response that varies with
    correctness is itself the oracle. Both branches must produce the same
    (glyph, message, outcome) once a genuinely new second guess is made."""
    prior = (PastAttempt(outcome="incorrect", student_answer_raw="18"),)

    correct_view = render_student_result(
        _row(outcome="correct", expected_answer="19"),
        "12 + 7",
        "19",
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=prior,
    )
    incorrect_view = render_student_result(
        _row(outcome="incorrect", expected_answer="19"),
        "12 + 7",
        "20",  # also a new, different guess from "18"
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=prior,
    )

    assert (correct_view.glyph, correct_view.message, correct_view.outcome) == (
        incorrect_view.glyph,
        incorrect_view.message,
        incorrect_view.outcome,
    )


def test_unchanged_resubmission_is_not_suppressed() -> None:
    """The whole-page-recapture case at the render layer: resubmitting the same
    answer is not a new attempt, so it renders exactly as a first attempt would."""
    prior = (PastAttempt(outcome="incorrect", student_answer_raw="18"),)

    view = render_student_result(
        _row(outcome="incorrect"),
        "12 + 7",
        "18",  # same answer as the prior attempt
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=prior,
    )

    assert view.outcome == "incorrect"
    assert view.message == "This one needs another look."


def test_full_mode_ignores_attempt_history_entirely() -> None:
    """In FULL mode the answer is already disclosed outright on attempt one, so
    there is nothing left to oracle -- suppression never applies."""
    prior = (PastAttempt(outcome="incorrect", student_answer_raw="18"),)

    view = render_student_result(
        _row(outcome="correct"),
        "12 + 7",
        "19",
        rules=_FULL,
        prior_attempts=prior,
    )

    assert view.message == "Correct!"
    assert view.outcome == "correct"


# -- Results table display bucket (eight NeedsHumanCause values -> six glance-able
# states: correct, correct-but-unsimplified, incorrect, could-not-read,
# waiting-on-a-key, needs-a-person) -----------------------------------------


@pytest.mark.parametrize(
    "cause,expected_bucket",
    [
        ("low_confidence", "could_not_read"),
        ("unknown_page", "could_not_read"),
        ("conflicting_page_markers", "could_not_read"),
        ("partial_page_markers", "could_not_read"),
        ("ambiguous_problem_id", "could_not_read"),
        ("no_key_for_page", "waiting_on_key"),
        ("needs_person", "needs_a_person"),
        ("answer_differs_from_key", "needs_a_person"),
    ],
)
def test_every_needs_human_cause_maps_to_one_of_three_display_buckets(
    cause: str, expected_bucket: str
) -> None:
    row = _row(outcome="needs_human", needs_human_cause=cause)

    view = render_student_result(row, "12 + 7", "18", rules=_FULL, prior_attempts=())

    assert view.display_bucket == expected_bucket


def test_a_needs_human_row_with_no_claimed_cause_falls_to_needs_a_person() -> None:
    """A row graded before the needs_human_cause column existed (migration 0006)
    -- genuinely unknown, so it goes to the bucket a grown-up must resolve,
    never guessed into could-not-read or waiting-on-key."""
    row = _row(outcome="needs_human", needs_human_cause=None)

    view = render_student_result(row, "12 + 7", "18", rules=_FULL, prior_attempts=())

    assert view.display_bucket == "needs_a_person"


def test_correct_and_unsimplified_share_a_bucket_but_not_a_message() -> None:
    plain = render_student_result(
        _row(outcome="correct"), "12 + 7", "19", rules=_FULL, prior_attempts=()
    )
    unsimplified = render_student_result(
        _row(outcome="correct", unsimplified=True), "2/6?", "2/6", rules=_FULL, prior_attempts=()
    )

    assert plain.display_bucket == unsimplified.display_bucket == "correct"
    assert plain.message != unsimplified.message


def test_display_number_is_the_real_problem_id() -> None:
    view = render_student_result(
        _row(problem_id="4"), "12 + 7", "18", rules=_FULL, prior_attempts=()
    )

    assert view.display_number == "4"


def test_display_number_hides_a_synthetic_ambiguous_placeholder() -> None:
    """AMBIGUOUS_PROBLEM_ID_PREFIX ids (k12ta.pipeline.process) are never a real
    printed label -- showing one in the results table's "#" column would be
    more confusing than the honest "no number to show" it actually is."""
    row = _row(problem_id=f"{AMBIGUOUS_PROBLEM_ID_PREFIX}0")

    view = render_student_result(row, "12 + 7", "18", rules=_FULL, prior_attempts=())

    assert view.display_number == "?"


def test_suppressed_repeat_still_gets_its_own_display_bucket() -> None:
    """Not "correct" or "incorrect" -- a bucket that never varies with the true
    outcome, mirroring the glyph/message suppression itself."""
    prior = (PastAttempt(outcome="incorrect", student_answer_raw="18"),)

    view = render_student_result(
        _row(outcome="correct"), "12 + 7", "19", rules=_DIAGNOSTIC_ONLY, prior_attempts=prior
    )

    assert view.display_bucket == "repeat"


# -- summarize_results: the page's summary counts and encouragement ----------


def _view(
    problem_id: str, prompt_text: str = "12 + 7", answer: str = "19", **overrides: object
) -> StudentResultView:
    row = _row(problem_id=problem_id, **overrides)  # type: ignore[arg-type]
    return render_student_result(row, prompt_text, answer, rules=_FULL, prior_attempts=())


def test_summary_counts_right_to_look_at_and_waiting_on_a_grownup() -> None:
    items = [
        _view("1", outcome="correct"),
        _view("2", outcome="correct", unsimplified=True),
        _view("3", outcome="incorrect"),
        _view("4", outcome="needs_human", needs_human_cause="low_confidence"),
        _view("5", outcome="needs_human", needs_human_cause="no_key_for_page"),
        _view("6", outcome="needs_human", needs_human_cause="needs_person"),
    ]

    summary = summarize_results(items)

    assert summary.right == 2
    assert summary.to_look_at == 2
    assert summary.waiting_on_grownup == 2


def test_all_correct_encouragement_names_no_problems() -> None:
    items = [_view(str(i), outcome="correct") for i in range(1, 4)]

    summary = summarize_results(items)

    assert summary.encouragement == "All 3 correct."


def test_encouragement_names_the_real_problem_numbers_to_look_at() -> None:
    items = [
        _view("1", outcome="correct"),
        _view("4", outcome="incorrect"),
        _view("7", outcome="incorrect"),
    ]

    summary = summarize_results(items)

    assert summary.encouragement == "1 of 3 correct. Problems 4 and 7 are worth another look."


def test_encouragement_never_names_a_synthetic_ambiguous_number() -> None:
    items = [
        _view("1", outcome="correct"),
        _view(
            f"{AMBIGUOUS_PROBLEM_ID_PREFIX}0",
            outcome="needs_human",
            needs_human_cause="ambiguous_problem_id",
        ),
    ]

    summary = summarize_results(items)

    assert AMBIGUOUS_PROBLEM_ID_PREFIX not in summary.encouragement
    assert summary.encouragement == "1 of 2 correct."


def test_encouragement_mentions_waiting_on_a_grownup_separately() -> None:
    items = [
        _view("1", outcome="correct"),
        _view("2", outcome="needs_human", needs_human_cause="no_key_for_page"),
    ]

    summary = summarize_results(items)

    assert summary.encouragement == "1 of 2 correct. One needs a grown-up to check."


def test_needs_human_prior_attempts_do_not_count_toward_suppression() -> None:
    prior = (PastAttempt(outcome="needs_human", student_answer_raw="???"),)

    view = render_student_result(
        _row(outcome="correct"),
        "12 + 7",
        "19",
        rules=_DIAGNOSTIC_ONLY,
        prior_attempts=prior,
    )

    assert view.message == "Correct!"
    assert view.outcome == "correct"
