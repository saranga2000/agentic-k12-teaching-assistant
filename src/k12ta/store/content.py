"""Content sources and the assignments drawn from them.

An assignment's foreign key is `(student_id, source_id)`, so an assignment can only
ever point at a content source configured for the same student.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ContentSourceRow:
    student_id: str
    source_id: str
    label: str
    kind: str
    subject: str
    has_answer_key: bool
    graded_by_someone_else: bool
    default_mode: str
    typical_session_minutes: int
    standards_frame: str | None = None


def insert_content_source(conn: sqlite3.Connection, row: ContentSourceRow) -> None:
    conn.execute(
        """
        INSERT INTO content_sources
            (student_id, source_id, label, kind, subject, has_answer_key,
             graded_by_someone_else, default_mode, typical_session_minutes, standards_frame)
        VALUES
            (:student_id, :source_id, :label, :kind, :subject, :has_answer_key,
             :graded_by_someone_else, :default_mode, :typical_session_minutes, :standards_frame)
        """,
        vars(row),
    )
    conn.commit()


def get_content_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> ContentSourceRow | None:
    cur = conn.execute(
        "SELECT * FROM content_sources WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_content_source(row)


def list_content_sources(conn: sqlite3.Connection, student_id: str) -> list[ContentSourceRow]:
    """Every content source configured for one student, for the M2.2 "change
    assignment" picker. Not the exception `list_students` is: this still takes
    student_id and only ever returns that student's rows."""
    cur = conn.execute(
        "SELECT * FROM content_sources WHERE student_id = ? ORDER BY label", (student_id,)
    )
    return [_row_to_content_source(row) for row in cur.fetchall()]


def _row_to_content_source(row: sqlite3.Row) -> ContentSourceRow:
    data = dict(row)
    data["has_answer_key"] = bool(data["has_answer_key"])
    data["graded_by_someone_else"] = bool(data["graded_by_someone_else"])
    return ContentSourceRow(**data)


@dataclass(frozen=True)
class AssignmentRow:
    student_id: str
    assignment_id: str
    source_id: str
    created_at: str
    label: str | None = None


def insert_assignment(conn: sqlite3.Connection, row: AssignmentRow) -> None:
    conn.execute(
        """
        INSERT INTO assignments (student_id, assignment_id, source_id, label, created_at)
        VALUES (:student_id, :assignment_id, :source_id, :label, :created_at)
        """,
        vars(row),
    )
    conn.commit()


def get_assignment(
    conn: sqlite3.Connection, student_id: str, assignment_id: str
) -> AssignmentRow | None:
    cur = conn.execute(
        "SELECT * FROM assignments WHERE student_id = ? AND assignment_id = ?",
        (student_id, assignment_id),
    )
    row = cur.fetchone()
    return None if row is None else AssignmentRow(**dict(row))
