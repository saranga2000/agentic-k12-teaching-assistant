"""Grade against a stored answer key.

This is the high-confidence path and should be preferred wherever a key exists.
It never consults a model for the verdict; the model only supplies the transcription
and, separately, the diagnosis of an already-established error.
"""

from __future__ import annotations

import re

from k12ta.domain.models import GradeOutcome

_WHITESPACE = re.compile(r"\s+")


def normalise(answer: str) -> str:
    """Canonicalise an answer string for comparison.

    Deliberately conservative. Ambiguity resolves to a mismatch, which routes to
    NEEDS_HUMAN rather than to a wrong mark against the student.
    """
    text = answer.strip().lower()
    text = text.replace("−", "-").replace("×", "*").replace("÷", "/")
    text = _WHITESPACE.sub("", text)
    return text.rstrip(".")


def grade_against_key(
    student_answer: str,
    key_answer: str,
    transcription_confidence: float,
    confidence_floor: float = 0.85,
) -> GradeOutcome:
    """Compare one answer to the key.

    A low-confidence transcription can never produce INCORRECT. A confidently wrong
    mark costs far more trust than an escalation to a grown-up.
    """
    if transcription_confidence < confidence_floor:
        return GradeOutcome.NEEDS_HUMAN
    if normalise(student_answer) == normalise(key_answer):
        return GradeOutcome.CORRECT
    if not student_answer.strip():
        return GradeOutcome.NEEDS_HUMAN
    return GradeOutcome.INCORRECT
