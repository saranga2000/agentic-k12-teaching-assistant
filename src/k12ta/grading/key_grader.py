"""Grade against a stored answer key.

This is the high-confidence path and should be preferred wherever a key exists.
It never consults a model for the verdict; the model only supplies the transcription
and, separately, the diagnosis of an already-established error.
"""

from __future__ import annotations

import re

from k12ta.domain.models import GradeOutcome

_WHITESPACE = re.compile(r"\s+")

CONFIDENCE_FLOOR = 0.95
"""Not 0.85. The 2026-08-12 transcription eval
(evals/results/2026-08-12-0825-vision_llm.md) measured calibration by confidence band:
the 0.85-0.95 band was only 60% accurate (n=5), while the 0.95-1.01 band was 100%
accurate (n=13). A floor of 0.85 would admit that 0.85-0.95 band and grade wrong
answers as CORRECT or INCORRECT with confidence the data does not support.

The single source of truth for this number: anything else in the pipeline that needs
to know whether a transcription is trustworthy enough to act on (e.g. the M2.3
rendering distinction between "I could not read this one clearly" and "I don't have an
answer key for this one yet") imports this constant rather than restating 0.95."""


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
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> GradeOutcome:
    """Compare one answer to the key.

    A low-confidence transcription can never produce INCORRECT. A confidently wrong
    mark costs far more trust than an escalation to a grown-up. See `CONFIDENCE_FLOOR`
    for why the default is 0.95.
    """
    if transcription_confidence < confidence_floor:
        return GradeOutcome.NEEDS_HUMAN
    if normalise(student_answer) == normalise(key_answer):
        return GradeOutcome.CORRECT
    if not student_answer.strip():
        return GradeOutcome.NEEDS_HUMAN
    return GradeOutcome.INCORRECT
