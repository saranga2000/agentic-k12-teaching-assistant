"""Which content source a student's capture defaults to on a given weekday."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class WeeklyDefaultSourceRow:
    student_id: str
    weekday: int
    """`date.weekday()`: 0 = Monday .. 6 = Sunday."""
    source_id: str


def set_default_source(conn: sqlite3.Connection, row: WeeklyDefaultSourceRow) -> None:
    conn.execute(
        """
        INSERT INTO weekly_default_sources (student_id, weekday, source_id)
        VALUES (:student_id, :weekday, :source_id)
        ON CONFLICT (student_id, weekday) DO UPDATE SET source_id = excluded.source_id
        """,
        vars(row),
    )
    conn.commit()


def get_default_source(
    conn: sqlite3.Connection, student_id: str, weekday: int
) -> WeeklyDefaultSourceRow | None:
    cur = conn.execute(
        "SELECT * FROM weekly_default_sources WHERE student_id = ? AND weekday = ?",
        (student_id, weekday),
    )
    row = cur.fetchone()
    return None if row is None else WeeklyDefaultSourceRow(**dict(row))
