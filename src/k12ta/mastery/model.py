"""Mastery with decay and retrieval scheduling.

Design position: a mastery model that only accumulates is wrong for a multi-year
system. A skill demonstrated in September must not still read as mastered in February
without fresh evidence, and the system must be able to say *when* it stops believing.

The representation is a two-parameter memory trace per skill:

    p(t) = floor + (p_last - floor) * 0.5 ** (days_since_review / stability_days)

`stability_days` is the half-life of the trace. Correct retrieval multiplies stability
(spacing effect, harder items more so). A lapse cuts stability back and drops the
retention estimate, which is what makes the skill resurface quickly.

`floor` encodes partial permanence: a skill practised many times does not decay to
zero, it decays to a residual.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta

INITIAL_STABILITY_DAYS = 2.0
MIN_STABILITY_DAYS = 1.0
MAX_STABILITY_DAYS = 365.0
MASTERY_THRESHOLD = 0.80
LEARNING_GAIN = 0.55
"""How much of the remaining gap one successful retrieval closes."""
MAX_STABILITY_GROWTH = 1.2
"""Ceiling on extra half-life from one review, at maximum spacing and difficulty."""
DUE_THRESHOLD = 0.70


@dataclass(frozen=True)
class SkillMastery:
    """The memory trace for one student and one skill."""

    student_id: str
    skill_id: str
    p_at_last_review: float
    stability_days: float
    last_reviewed_on: date
    review_count: int = 0
    correct_count: int = 0

    @staticmethod
    def new(student_id: str, skill_id: str, on: date) -> SkillMastery:
        """A skill with no evidence yet. Not mastered, and due immediately."""
        return SkillMastery(
            student_id=student_id,
            skill_id=skill_id,
            p_at_last_review=0.0,
            stability_days=INITIAL_STABILITY_DAYS,
            last_reviewed_on=on,
            review_count=0,
            correct_count=0,
        )

    @property
    def floor(self) -> float:
        """Residual retention. Grows slowly with successful reviews, caps at 0.4."""
        return min(0.4, 0.05 * self.correct_count)

    def retention_on(self, on: date) -> float:
        """Estimated probability of correct retrieval on a given date."""
        elapsed = max(0, (on - self.last_reviewed_on).days)
        decay_factor = math.pow(0.5, elapsed / self.stability_days)
        decayed = (self.p_at_last_review - self.floor) * decay_factor
        return round(min(1.0, max(0.0, self.floor + decayed)), 4)

    def is_mastered_on(self, on: date, threshold: float = MASTERY_THRESHOLD) -> bool:
        """Mastery is always evaluated as of a date. There is no timeless 'mastered'."""
        return self.retention_on(on) >= threshold

    def due_on(self, threshold: float = DUE_THRESHOLD) -> date:
        """The date this skill should resurface for a spaced check."""
        if self.p_at_last_review <= threshold or self.floor >= threshold:
            return self.last_reviewed_on + timedelta(days=1)
        ratio = (threshold - self.floor) / (self.p_at_last_review - self.floor)
        days = self.stability_days * math.log2(1.0 / ratio)
        return self.last_reviewed_on + timedelta(days=max(1, round(days)))

    def is_due(self, on: date, threshold: float = DUE_THRESHOLD) -> bool:
        return self.retention_on(on) < threshold

    def observe(self, *, correct: bool, on: date, difficulty: float = 0.5) -> SkillMastery:
        """Fold in one observation and return the updated trace.

        Correct: stability grows, more so for harder items and longer gaps.
        Incorrect: stability is cut back and retention drops, so the skill returns soon.
        """
        if not 0.0 <= difficulty <= 1.0:
            raise ValueError("difficulty must be in [0, 1]")

        retrieved = self.retention_on(on)

        if correct:
            # Growth is always >= 1.0: a correct answer must never shrink the trace.
            # A review while retention is still high earns little (spacing effect);
            # a harder item earns more.
            spacing_factor = 1.0 - retrieved
            difficulty_factor = 0.5 + difficulty
            growth = 1.0 + MAX_STABILITY_GROWTH * spacing_factor * difficulty_factor
            stability = self.stability_days * growth
            p_new = min(1.0, retrieved + LEARNING_GAIN * (1.0 - retrieved))
        else:
            stability = self.stability_days * 0.45
            p_new = max(0.0, retrieved * 0.5)

        return replace(
            self,
            p_at_last_review=round(p_new, 4),
            stability_days=round(min(MAX_STABILITY_DAYS, max(MIN_STABILITY_DAYS, stability)), 4),
            last_reviewed_on=on,
            review_count=self.review_count + 1,
            correct_count=self.correct_count + (1 if correct else 0),
        )
