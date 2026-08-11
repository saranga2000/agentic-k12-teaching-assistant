"""Core domain objects.

Every persisted entity carries `student_id`. There is no single-student assumption
anywhere in this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from uuid import UUID, uuid4


class GradeOutcome(Enum):
    """Result of grading one problem."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIAL = "partial"
    NEEDS_HUMAN = "needs_human"
    """Transcription or grading confidence below threshold. Never guess instead."""


@dataclass(frozen=True)
class Student:
    student_id: UUID
    display_name: str
    grade_level: int
    state_code: str
    coach_name: str
    birth_year: int | None = None

    @staticmethod
    def new(display_name: str, grade_level: int, state_code: str, coach_name: str) -> Student:
        return Student(uuid4(), display_name, grade_level, state_code, coach_name)


@dataclass(frozen=True)
class Skill:
    """An atom of the mastery model, keyed to a state standard where one exists."""

    skill_id: str
    label: str
    standard_code: str | None = None
    subject: str = "math"


@dataclass(frozen=True)
class Problem:
    """One transcribed problem and the student's answer as written."""

    problem_id: str
    prompt_text: str
    student_answer_raw: str
    transcription_confidence: float
    skill_ids: tuple[str, ...] = ()
    page_region: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class PageCapture:
    """A single photographed page of work."""

    capture_id: UUID
    student_id: UUID
    assignment_id: str
    captured_at: datetime
    image_path: str
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class Diagnosis:
    """Why the error happened, not merely that it happened."""

    misconception_id: str
    explanation: str
    error_location: str
    skill_ids: tuple[str, ...]


@dataclass(frozen=True)
class GradedProblem:
    problem_id: str
    outcome: GradeOutcome
    expected_answer: str | None
    diagnosis: Diagnosis | None = None
    grader_confidence: float = 0.0


@dataclass
class Session:
    session_id: UUID
    student_id: UUID
    started_at: datetime
    assignment_id: str
    graded: list[GradedProblem] = field(default_factory=list)
    ended_at: datetime | None = None

    @property
    def duration_minutes(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds() / 60.0


@dataclass(frozen=True)
class SkillEvidence:
    """One observation feeding the mastery model."""

    student_id: UUID
    skill_id: str
    observed_on: date
    correct: bool
    difficulty: float = 0.5
