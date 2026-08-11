from __future__ import annotations

from datetime import date, timedelta

from alc.mastery.model import SkillMastery
from alc.mastery.scheduler import select_for_session


def build(skill: str, on: date, successes: int) -> SkillMastery:
    t = SkillMastery.new("s1", skill, on)
    d = on
    for _ in range(successes):
        t = t.observe(correct=True, on=d)
        d = t.due_on()
    return t


def test_session_is_not_all_weakest_skills(sept: date) -> None:
    traces = [build(f"skill-{i}", sept, successes=i * 2) for i in range(6)]
    later = sept + timedelta(days=20)
    picked = select_for_session(traces, later, max_items=4)
    assert len(picked) == 4
    assert any(not t.is_due(later) for t in picked)


def test_empty_input_is_safe(sept: date) -> None:
    assert select_for_session([], sept) == []


def test_session_is_not_padded_with_mastered_work(sept: date) -> None:
    """One due skill and many solid ones must not produce a full session of revision."""
    traces = [build(f"solid-{i}", sept, successes=12) for i in range(8)]
    traces.append(SkillMastery.new("s1", "brand-new", sept))
    picked = select_for_session(traces, sept, max_items=6)
    assert len(picked) == 3
    assert sum(1 for t in picked if not t.is_due(sept)) == 2


def test_session_opens_with_something_winnable(sept: date) -> None:
    traces = [build(f"solid-{i}", sept, successes=12) for i in range(2)]
    traces += [SkillMastery.new("s1", f"weak-{i}", sept) for i in range(4)]
    picked = select_for_session(traces, sept, max_items=6)
    assert not picked[0].is_due(sept)
    assert not picked[-1].is_due(sept)


def test_no_duplicates(sept: date) -> None:
    traces = [build(f"skill-{i}", sept, successes=i * 2) for i in range(6)]
    picked = select_for_session(traces, sept + timedelta(days=20), max_items=6)
    assert len({t.skill_id for t in picked}) == len(picked)
