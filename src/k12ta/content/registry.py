"""In-memory registry of configured content sources.

Ships with no student-specific data. The examples below are constructed at setup time
from parent input and are here only as reference shapes for tests and seeding.
"""

from __future__ import annotations

from k12ta.content.source import ContentSource, SourceKind
from k12ta.domain.policy import FeedbackMode


class ContentSourceRegistry:
    """Lookup by id. Persistence lands in M2; this keeps the interface stable."""

    def __init__(self, sources: list[ContentSource] | None = None) -> None:
        self._by_id: dict[str, ContentSource] = {s.source_id: s for s in (sources or [])}

    def add(self, source: ContentSource) -> None:
        self._by_id[source.source_id] = source

    def get(self, source_id: str) -> ContentSource | None:
        return self._by_id.get(source_id)

    def all(self) -> list[ContentSource]:
        return list(self._by_id.values())


def example_sources() -> list[ContentSource]:
    """Reference shapes covering the four policy cases. Not seeded automatically."""
    return [
        ContentSource(
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind=SourceKind.WORKBOOK,
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode=FeedbackMode.FULL,
            typical_session_minutes=30,
            standards_frame="state",
        ),
        ContentSource(
            source_id="outside_math_program_hw",
            label="Outside maths programme homework",
            kind=SourceKind.WORKSHEET_PACKET,
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=True,
            default_mode=FeedbackMode.DIAGNOSTIC_ONLY,
            typical_session_minutes=25,
            standards_frame=None,
        ),
        ContentSource(
            source_id="daily_fluency_drill",
            label="Daily timed fluency packet",
            kind=SourceKind.FLUENCY_DRILL,
            subject="reading",
            has_answer_key=True,
            graded_by_someone_else=True,
            default_mode=FeedbackMode.FLUENCY,
            typical_session_minutes=10,
            standards_frame=None,
        ),
        ContentSource(
            source_id="school_homework",
            label="School homework",
            kind=SourceKind.WORKSHEET_PACKET,
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=True,
            default_mode=FeedbackMode.DIAGNOSTIC_ONLY,
            typical_session_minutes=20,
            standards_frame="state",
        ),
    ]
