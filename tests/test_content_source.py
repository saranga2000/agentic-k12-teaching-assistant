from __future__ import annotations

from k12ta.content.registry import ContentSourceRegistry, example_sources
from k12ta.domain.policy import FeedbackMode, resolve_mode


def test_registry_round_trips() -> None:
    reg = ContentSourceRegistry(example_sources())
    src = reg.get("school_homework")
    assert src is not None and src.graded_by_someone_else


def test_every_externally_graded_source_resolves_to_diagnostic_or_fluency() -> None:
    for src in example_sources():
        if not src.graded_by_someone_else:
            continue
        mode = resolve_mode(
            source_default_mode=src.default_mode,
            work_will_be_graded_by_someone_else=True,
        )
        assert mode in {FeedbackMode.DIAGNOSTIC_ONLY, FeedbackMode.FLUENCY}


def test_sources_with_a_key_are_marked_as_ground_truth() -> None:
    keyed = [s for s in example_sources() if s.has_answer_key]
    assert keyed and all(s.key_is_ground_truth() for s in keyed)


def test_the_seeded_fluency_source_actually_resolves_to_fluency_mode() -> None:
    """resolve_mode's precedence checks graded_by_someone_else before the source's
    own default_mode, unconditionally and correctly (see test_policy.py) -- a
    fluency drill scored by the coach itself, not by anyone else, must not be
    marked graded_by_someone_else=True, or its own default_mode=FLUENCY is
    unreachable configuration. This was exactly wrong until now: the flag, not
    resolve_mode's precedence, was the bug."""
    reg = ContentSourceRegistry(example_sources())
    src = reg.get("daily_fluency_drill")
    assert src is not None

    mode = resolve_mode(
        source_default_mode=src.default_mode,
        work_will_be_graded_by_someone_else=src.graded_by_someone_else,
    )

    assert mode is FeedbackMode.FLUENCY
