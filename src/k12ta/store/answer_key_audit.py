"""An append-only log of every answer-key confirm action: a brand new entry, an
identical re-scan, or an explicitly resolved conflict between a stored answer and a
newly scanned one. Never updated, only inserted into -- it's a record of what
happened, not current state (that's `k12ta.store.answer_keys`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerKeyAuditRow:
    student_id: str
    source_id: str
    page_number: int
    problem_number: str
    action: str
    """One of "created", "matched", "conflict_resolved"."""
    old_answer_text: str | None
    old_ungradeable_reason: str | None
    new_answer_text: str | None
    new_ungradeable_reason: str | None
    resolution: str | None
    """One of "kept_old", "used_new"; None for "created" / "matched"."""
    recorded_at: str


def insert_audit_row(conn: sqlite3.Connection, row: AnswerKeyAuditRow) -> None:
    conn.execute(
        """
        INSERT INTO answer_key_audit_log
            (student_id, source_id, page_number, problem_number, action,
             old_answer_text, old_ungradeable_reason, new_answer_text,
             new_ungradeable_reason, resolution, recorded_at)
        VALUES
            (:student_id, :source_id, :page_number, :problem_number, :action,
             :old_answer_text, :old_ungradeable_reason, :new_answer_text,
             :new_ungradeable_reason, :resolution, :recorded_at)
        """,
        vars(row),
    )
    conn.commit()


def list_audit_log_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[AnswerKeyAuditRow]:
    cur = conn.execute(
        """
        SELECT student_id, source_id, page_number, problem_number, action,
               old_answer_text, old_ungradeable_reason, new_answer_text,
               new_ungradeable_reason, resolution, recorded_at
        FROM answer_key_audit_log
        WHERE student_id = ? AND source_id = ?
        ORDER BY id
        """,
        (student_id, source_id),
    )
    return [AnswerKeyAuditRow(**dict(row)) for row in cur.fetchall()]
