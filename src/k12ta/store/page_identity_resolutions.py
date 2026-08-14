"""An append-only log of every automatic page-identity resolution attempt during a
student capture. Never written for a caller-supplied page_number (a test, a manual
override) -- the point is measuring what real extraction actually does, not
padding the count with cases that never went through it.

Four honest outcomes (`k12ta.grading.page_identity.PageIdentityOutcome`), not a
single pass/fail: "resolved", "below_floor", "not_found", and "conflicting" each
call for a different fix, and a parent needs to be able to tell them apart -- see
the counts surfaced on `k12ta.keys`'s enrollment page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageIdentityResolutionRow:
    student_id: str
    source_id: str
    capture_id: str
    outcome: str
    """One of PageIdentityOutcome's values: "resolved", "below_floor",
    "not_found", "conflicting"."""
    resolved_page_number: int | None
    """Set only when outcome is "resolved"."""
    created_at: str


def insert_resolution(conn: sqlite3.Connection, row: PageIdentityResolutionRow) -> None:
    conn.execute(
        """
        INSERT INTO page_identity_resolutions
            (student_id, source_id, capture_id, outcome, resolved_page_number, created_at)
        VALUES
            (:student_id, :source_id, :capture_id, :outcome, :resolved_page_number, :created_at)
        """,
        vars(row),
    )
    conn.commit()


def count_outcomes_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> dict[str, int]:
    """Every outcome that has fired at least once, keyed by outcome value. An
    outcome with zero rows is simply absent from the dict -- the caller decides
    how to render "never happened," not this function."""
    cur = conn.execute(
        """
        SELECT outcome, COUNT(*) FROM page_identity_resolutions
        WHERE student_id = ? AND source_id = ?
        GROUP BY outcome
        """,
        (student_id, source_id),
    )
    return {row[0]: row[1] for row in cur.fetchall()}
