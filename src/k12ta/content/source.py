"""Content source abstraction.

A content source is *where the work comes from*, and it carries the rules that travel
with that work: whether an answer key exists, whether someone else grades it, what
feedback mode applies, and what cadence is realistic.

Adding a new tutoring programme, workbook, or teacher packet must be a row of data,
never a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from k12ta.domain.policy import FeedbackMode


class SourceKind(Enum):
    WORKBOOK = "workbook"
    WORKSHEET_PACKET = "worksheet_packet"
    TEXTBOOK = "textbook"
    FLUENCY_DRILL = "fluency_drill"
    GENERATED = "generated"
    """Produced by the coach itself, e.g. a targeted follow-up quiz."""


@dataclass(frozen=True)
class ContentSource:
    """One place work comes from, plus the policy attached to it."""

    source_id: str
    label: str
    kind: SourceKind
    subject: str
    has_answer_key: bool
    graded_by_someone_else: bool
    default_mode: FeedbackMode
    typical_session_minutes: int
    standards_frame: str | None = None
    """e.g. a state standards code family. None where the programme has its own
    internal sequence that does not map cleanly to state standards."""

    def key_is_ground_truth(self) -> bool:
        """True when grading should read from a stored key rather than solving."""
        return self.has_answer_key
