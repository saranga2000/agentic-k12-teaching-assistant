"""Memory with decay. The property being protected is that mastery expires."""

from __future__ import annotations

from datetime import date, timedelta

from k12ta.mastery.model import SkillMastery


def trace(on: date) -> SkillMastery:
    return SkillMastery.new("student-1", "ratios.unit_rate", on)


def test_a_new_skill_is_not_mastered_and_is_due(sept: date) -> None:
    t = trace(sept)
    assert not t.is_mastered_on(sept)
    assert t.is_due(sept)


def test_september_mastery_does_not_survive_to_february(sept: date, feb: date) -> None:
    t = trace(sept)
    for i in range(3):
        t = t.observe(correct=True, on=sept + timedelta(days=i * 3))
    assert t.is_mastered_on(t.last_reviewed_on)
    assert not t.is_mastered_on(feb)


def test_repeated_success_lengthens_the_half_life(sept: date) -> None:
    t = trace(sept)
    first = t.observe(correct=True, on=sept)
    second = first.observe(correct=True, on=first.due_on())
    assert second.stability_days > first.stability_days


def test_a_lapse_shortens_the_half_life_and_drops_retention(sept: date) -> None:
    t = trace(sept).observe(correct=True, on=sept)
    lapsed = t.observe(correct=False, on=sept + timedelta(days=1))
    assert lapsed.stability_days < t.stability_days
    assert lapsed.retention_on(lapsed.last_reviewed_on) < t.retention_on(t.last_reviewed_on)


def test_retention_is_monotonically_non_increasing_between_reviews(sept: date) -> None:
    t = trace(sept).observe(correct=True, on=sept)
    values = [t.retention_on(sept + timedelta(days=d)) for d in range(0, 60, 5)]
    assert all(b <= a + 1e-9 for a, b in zip(values, values[1:], strict=False))


def test_well_practised_skills_decay_to_a_residual_not_to_zero(sept: date) -> None:
    t = trace(sept)
    d = sept
    for _ in range(8):
        t = t.observe(correct=True, on=d)
        d = t.due_on()
    far_future = sept + timedelta(days=1000)
    assert t.retention_on(far_future) >= 0.3


def test_due_date_is_in_the_future_for_a_fresh_success(sept: date) -> None:
    t = trace(sept).observe(correct=True, on=sept)
    assert t.due_on() > sept


def test_a_correct_answer_never_shrinks_the_trace(sept: date) -> None:
    """Regression: an easy item answered correctly while retention is still high used
    to reduce stability, contradicting the documented design."""
    t = trace(sept)
    for difficulty in (0.0, 0.25, 0.5, 0.75, 1.0):
        for gap in (0, 1, 3, 10, 30):
            on = t.last_reviewed_on + timedelta(days=gap)
            after = t.observe(correct=True, on=on, difficulty=difficulty)
            assert after.stability_days >= t.stability_days


def test_harder_items_and_longer_gaps_earn_more_stability(sept: date) -> None:
    base = trace(sept).observe(correct=True, on=sept)
    on = base.last_reviewed_on
    easy = base.observe(correct=True, on=on, difficulty=0.0)
    hard = base.observe(correct=True, on=on, difficulty=1.0)
    assert hard.stability_days > easy.stability_days

    soon = base.observe(correct=True, on=on + timedelta(days=1))
    later = base.observe(correct=True, on=on + timedelta(days=20))
    assert later.stability_days > soon.stability_days


def test_difficulty_outside_range_is_rejected(sept: date) -> None:
    import pytest

    with pytest.raises(ValueError):
        trace(sept).observe(correct=True, on=sept, difficulty=1.5)
