"""A parent's explicit override of one enrollment's feedback mode -- the
persisted supply for k12ta.domain.policy.resolve_mode's `parent_override`
parameter, which has existed since M3.2 with nothing ever passing it a real
value (docs/ROADMAP.md, M3's own remaining bullet). Current state only, one
row per (student, source); k12ta.store.policy_override_audit is the
append-only history of every change. Setting or clearing an override is
PIN-gated in k12ta.keys.app -- this module only persists what it's given,
same boundary k12ta.keys' answer-key ingestion already draws for itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyOverrideRow:
    student_id: str
    source_id: str
    mode: str
    set_at: str


def get_override(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> PolicyOverrideRow | None:
    cur = conn.execute(
        "SELECT * FROM policy_overrides WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    row = cur.fetchone()
    return None if row is None else PolicyOverrideRow(**dict(row))


def set_override(conn: sqlite3.Connection, row: PolicyOverrideRow) -> None:
    """Insert or replace -- a second override for the same enrollment
    replaces the first rather than erroring; the audit log (inserted by the
    caller alongside this, never here) is what keeps the history."""
    conn.execute(
        """
        INSERT INTO policy_overrides (student_id, source_id, mode, set_at)
        VALUES (:student_id, :source_id, :mode, :set_at)
        ON CONFLICT (student_id, source_id) DO UPDATE SET
            mode = excluded.mode,
            set_at = excluded.set_at
        """,
        vars(row),
    )
    conn.commit()


def clear_override(conn: sqlite3.Connection, student_id: str, source_id: str) -> None:
    """Back to automatic resolution. A no-op, not an error, when nothing was
    overridden -- same "stale action, nothing happens" honesty as the rest
    of k12ta.keys' parent-facing actions."""
    conn.execute(
        "DELETE FROM policy_overrides WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    conn.commit()
