"""An append-only log of every change to a policy override: setting one,
replacing one, or clearing one back to automatic. Never updated, only
inserted into -- a record of what happened, not current state (that's
k12ta.store.policy_overrides). Same shape and reasoning as
k12ta.store.answer_key_audit.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyOverrideAuditRow:
    student_id: str
    source_id: str
    previous_mode: str | None
    """None means no override was in effect before this change."""
    new_mode: str | None
    """None means this change cleared the override, back to automatic
    resolution."""
    recorded_at: str


def insert_audit_row(conn: sqlite3.Connection, row: PolicyOverrideAuditRow) -> None:
    conn.execute(
        """
        INSERT INTO policy_override_audit_log
            (student_id, source_id, previous_mode, new_mode, recorded_at)
        VALUES
            (:student_id, :source_id, :previous_mode, :new_mode, :recorded_at)
        """,
        vars(row),
    )
    conn.commit()


def list_audit_log_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[PolicyOverrideAuditRow]:
    cur = conn.execute(
        """
        SELECT student_id, source_id, previous_mode, new_mode, recorded_at
        FROM policy_override_audit_log
        WHERE student_id = ? AND source_id = ?
        ORDER BY id
        """,
        (student_id, source_id),
    )
    return [PolicyOverrideAuditRow(**dict(row)) for row in cur.fetchall()]
