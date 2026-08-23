"""A parent's explicit "this photo is the same page as that one" -- the manual
fallback for unresolved captures that automatic dedup (grouped by resolved
page_number, k12ta.keys.app._group_pending_by_capture) can never reach, since an
unresolved capture has no page_number to group by at all. See docs/ROADMAP.md's
M3.9. Deletes and regrades nothing -- only changes which block a capture's pending
items get folded into on screen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureDuplicateRow:
    student_id: str
    capture_id: str
    duplicate_of_capture_id: str
    """The capture this one's items should be folded into on screen -- a
    parent's claim, never validated for correctness beyond "both captures
    exist" (k12ta.keys.app.submit_mark_duplicate checks that much before
    writing)."""
    marked_at: str


def mark_duplicate(conn: sqlite3.Connection, row: CaptureDuplicateRow) -> None:
    """Re-marking the same capture overwrites its previous target -- a parent
    changing her mind is a correction, not a second claim to reconcile."""
    conn.execute(
        """
        INSERT INTO capture_duplicates
            (student_id, capture_id, duplicate_of_capture_id, marked_at)
        VALUES
            (:student_id, :capture_id, :duplicate_of_capture_id, :marked_at)
        ON CONFLICT (student_id, capture_id) DO UPDATE SET
            duplicate_of_capture_id = excluded.duplicate_of_capture_id,
            marked_at = excluded.marked_at
        """,
        vars(row),
    )
    conn.commit()


def get_duplicate_map(conn: sqlite3.Connection, student_id: str) -> dict[str, str]:
    """{capture_id: duplicate_of_capture_id} for every capture this student has
    ever marked, one hop only -- not resolved transitively. Chain-following
    and cycle guarding are a display concern, done by the caller
    (k12ta.keys.app._group_pending_by_capture), not a repository one."""
    cur = conn.execute(
        "SELECT capture_id, duplicate_of_capture_id FROM capture_duplicates WHERE student_id = ?",
        (student_id,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}
