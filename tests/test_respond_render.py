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
from k12ta.respond.render import render_student_result
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
