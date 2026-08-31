"""A child's "please add a program for me" flag -- Gap A
(docs/USER_WORKFLOWS.md): the empty state at `program_picker` (zero content
sources) has a way to reach the parent app now, in-app only. Current state
only, one row per student; there is nothing to audit-log here the way
k12ta.store.policy_overrides does, since this flag carries no consequential
decision, only a request.
"""

from __future__ import annotations

import sqlite3


def request_program(conn: sqlite3.Connection, student_id: str, requested_at: str) -> None:
    """Insert or overwrite -- re-tapping just updates the timestamp, same
    reasoning as k12ta.store.sessions.request_reminder: there is no
    "already requested" state worth protecting."""
    conn.execute(
        """
        INSERT INTO program_requests (student_id, requested_at)
        VALUES (:student_id, :requested_at)
        ON CONFLICT (student_id) DO UPDATE SET requested_at = excluded.requested_at
        """,
        {"student_id": student_id, "requested_at": requested_at},
    )
    conn.commit()


def get_requested_at(conn: sqlite3.Connection, student_id: str) -> str | None:
    cur = conn.execute(
        "SELECT requested_at FROM program_requests WHERE student_id = ?", (student_id,)
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])
