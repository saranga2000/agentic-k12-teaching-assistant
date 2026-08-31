"""Gap O (docs/USER_WORKFLOWS.md): "a grown-up changed how pages are
identified for this program" -- a local, in-app notice, same honest
limitation as k12ta.store.program_requests (no email/SMS infra to page
anyone). Set by k12ta.keys.app when a parent's correction to a not-yet-
confirmed identity schema retroactively changes what some already-graded
pages resolved to; cleared by the child's own "Got it" tap on
k12ta.web.app's source_home. One row per (student, source) -- a fresh
correction before the last one was acknowledged overwrites the timestamp
rather than stacking a second notice, same reasoning as
sessions.request_reminder.
"""

from __future__ import annotations

import sqlite3


def record_correction(
    conn: sqlite3.Connection, student_id: str, source_id: str, corrected_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO identity_corrections (student_id, source_id, corrected_at)
        VALUES (:student_id, :source_id, :corrected_at)
        ON CONFLICT (student_id, source_id) DO UPDATE SET corrected_at = excluded.corrected_at
        """,
        {"student_id": student_id, "source_id": source_id, "corrected_at": corrected_at},
    )
    conn.commit()


def get_correction(conn: sqlite3.Connection, student_id: str, source_id: str) -> str | None:
    cur = conn.execute(
        "SELECT corrected_at FROM identity_corrections WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])


def dismiss_correction(conn: sqlite3.Connection, student_id: str, source_id: str) -> None:
    """A no-op, not an error, when there is nothing to dismiss -- same
    "stale action, nothing happens" honesty as k12ta.store.policy_overrides.
    clear_override."""
    conn.execute(
        "DELETE FROM identity_corrections WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    conn.commit()
