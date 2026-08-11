from __future__ import annotations

from alc.content.registry import ContentSourceRegistry, example_sources
from alc.domain.policy import FeedbackMode, resolve_mode


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
