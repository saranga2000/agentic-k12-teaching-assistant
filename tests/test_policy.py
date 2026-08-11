"""The academic integrity rail. These tests are load-bearing, not illustrative."""

from __future__ import annotations

import pytest

from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for


def test_graded_work_never_reveals_the_answer() -> None:
    mode = resolve_mode(source_default_mode=FeedbackMode.FULL, work_will_be_graded_by_someone_else=True)
    assert mode is FeedbackMode.DIAGNOSTIC_ONLY
    assert rules_for(mode).forbids_answer()


def test_unknown_source_fails_closed() -> None:
    mode = resolve_mode(source_default_mode=None, work_will_be_graded_by_someone_else=False)
    assert mode is FeedbackMode.DIAGNOSTIC_ONLY


def test_self_directed_practice_teaches_fully() -> None:
    mode = resolve_mode(
        source_default_mode=FeedbackMode.FULL, work_will_be_graded_by_someone_else=False
    )
    rules = rules_for(mode)
    assert rules.reveal_final_answer and rules.reveal_worked_steps


def test_parent_override_is_the_only_way_out_of_diagnostic_mode() -> None:
    mode = resolve_mode(
        source_default_mode=None,
        work_will_be_graded_by_someone_else=True,
        parent_override=FeedbackMode.FULL,
    )
    assert mode is FeedbackMode.FULL


@pytest.mark.parametrize("mode", [FeedbackMode.DIAGNOSTIC_ONLY, FeedbackMode.FLUENCY])
def test_no_worked_steps_outside_full_mode(mode: FeedbackMode) -> None:
    assert rules_for(mode).reveal_worked_steps is False


def test_diagnostic_mode_still_locates_the_error() -> None:
    rules = rules_for(FeedbackMode.DIAGNOSTIC_ONLY)
    assert rules.name_error_location and rules.name_concept


def test_fluency_mode_does_not_interrupt_the_drill() -> None:
    rules = rules_for(FeedbackMode.FLUENCY)
    assert rules.is_timed
    assert not rules.offer_hint_ladder
