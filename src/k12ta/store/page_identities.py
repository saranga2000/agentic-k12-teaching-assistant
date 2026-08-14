"""The day/worksheet-code/marker -> page_number mapping a confirmed key page
establishes. Nothing enters this table until a parent confirms a scanned key page,
same rule as `k12ta.store.answer_keys` -- this is what lets a student capture that
reads "Day 11" resolve to a real page_number instead of staying UNKNOWN_PAGE.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageIdentityRow:
    student_id: str
    source_id: str
    page_number: int
    identifier_value: str
    """The raw marker text as printed, e.g. "Day 11" or "All 168a" -- not
    normalized or parsed further than the model already does."""
    confirmed_at: str
    source: str = "model"
    """"model" when the parent confirmed the value the transcriber extracted
    unchanged, "manual" when the parent typed or corrected it on the confirm
    screen (k12ta.keys.app). Distinct from confidence: a low-confidence value the
    parent leaves as-is is still "model" -- this field is about who supplied the
    value, not how sure anyone was. Lets page-identity accuracy be measured
    against only what the model actually produced."""


def upsert_identity(conn: sqlite3.Connection, row: PageIdentityRow) -> None:
    """Insert or correct one mapping. Re-confirming an already-stored marker
    (overlapping photo boundaries in a scanning sitting) updates it rather than
    erroring -- same reasoning as `answer_keys.upsert_entry`."""
    conn.execute(
        """
        INSERT INTO page_identities
            (student_id, source_id, page_number, identifier_value, confirmed_at, source)
        VALUES
            (:student_id, :source_id, :page_number, :identifier_value, :confirmed_at, :source)
        ON CONFLICT (student_id, source_id, identifier_value) DO UPDATE SET
            page_number = excluded.page_number,
            confirmed_at = excluded.confirmed_at,
            source = excluded.source
        """,
        vars(row),
    )
    conn.commit()


def get_page_number(
    conn: sqlite3.Connection, student_id: str, source_id: str, identifier_value: str
) -> int | None:
    cur = conn.execute(
        """
        SELECT page_number FROM page_identities
        WHERE student_id = ? AND source_id = ? AND identifier_value = ?
        """,
        (student_id, source_id, identifier_value),
    )
    row = cur.fetchone()
    return None if row is None else int(row[0])
