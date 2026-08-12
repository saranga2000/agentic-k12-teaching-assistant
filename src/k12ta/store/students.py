"""Student rows. The root table every other table's student_id points at."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StudentRow:
    student_id: str
    display_name: str
    grade_level: int
    state_code: str
    coach_name: str
    birth_year: int | None = None


def insert_student(conn: sqlite3.Connection, row: StudentRow) -> None:
    conn.execute(
        """
        INSERT INTO students
            (student_id, display_name, grade_level, state_code, coach_name, birth_year)
        VALUES
            (:student_id, :display_name, :grade_level, :state_code, :coach_name, :birth_year)
        """,
        vars(row),
    )
    conn.commit()


def get_student(conn: sqlite3.Connection, student_id: str) -> StudentRow | None:
    cur = conn.execute("SELECT * FROM students WHERE student_id = ?", (student_id,))
    row = cur.fetchone()
    return None if row is None else StudentRow(**dict(row))


def list_students(conn: sqlite3.Connection) -> list[StudentRow]:
    """Every student. The one deliberate exception to student_id-scoped reads: this
    is the student picker's data source, and enumerating across students is the
    point, not a leak. See tests/test_store_scoping.py."""
    cur = conn.execute("SELECT * FROM students ORDER BY display_name")
    return [StudentRow(**dict(row)) for row in cur.fetchall()]
