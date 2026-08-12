"""Skill mastery traces. One row per (student, skill), upserted on every review."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SkillMasteryRow:
    student_id: str
    skill_id: str
    p_at_last_review: float
    stability_days: float
    last_reviewed_on: str
    review_count: int
    correct_count: int


def upsert_skill_mastery(conn: sqlite3.Connection, row: SkillMasteryRow) -> None:
    conn.execute(
        """
        INSERT INTO skill_mastery_traces
            (student_id, skill_id, p_at_last_review, stability_days, last_reviewed_on,
             review_count, correct_count)
        VALUES
            (:student_id, :skill_id, :p_at_last_review, :stability_days, :last_reviewed_on,
             :review_count, :correct_count)
        ON CONFLICT (student_id, skill_id) DO UPDATE SET
            p_at_last_review = excluded.p_at_last_review,
            stability_days = excluded.stability_days,
            last_reviewed_on = excluded.last_reviewed_on,
            review_count = excluded.review_count,
            correct_count = excluded.correct_count
        """,
        vars(row),
    )
    conn.commit()


def get_skill_mastery(
    conn: sqlite3.Connection, student_id: str, skill_id: str
) -> SkillMasteryRow | None:
    cur = conn.execute(
        "SELECT * FROM skill_mastery_traces WHERE student_id = ? AND skill_id = ?",
        (student_id, skill_id),
    )
    row = cur.fetchone()
    return None if row is None else SkillMasteryRow(**dict(row))
