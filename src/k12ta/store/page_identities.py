"""The composite-identity -> page_number mapping a confirmed key page establishes.
Nothing enters this table until a parent confirms a scanned key page, same rule as
k12ta.store.answer_keys -- this is what lets a student capture that reads "Section
1, Day 11" resolve to a real page_number instead of staying UNKNOWN_PAGE.

`composite_key` is built from a source's current identity schema
(k12ta.store.page_identity_schemas) -- see k12ta.grading.page_identity.build_
composite_key, the one place that construction happens, shared by resolution and
confirmation so the two sides can never drift apart on how a key is built.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageIdentityRow:
    student_id: str
    source_id: str
    page_number: int
    composite_key: str
    """This source's schema components' values, joined in schema position order --
    not normalized or parsed further than the model or a parent already did."""
    schema_version: int
    """Which version of the source's identity schema this mapping was confirmed
    under. A lookup always filters on the source's *current* version -- a mapping
    confirmed under an older version is never deleted or reinterpreted when the
    schema changes, it just stops being eligible for auto-resolution until a
    parent re-confirms it under the new schema."""
    confirmed_at: str
    source: str = "model"
    """"model" when the parent confirmed the value the transcriber extracted
    unchanged, "manual" when the parent typed or corrected it (on the confirm
    screen, or through the no-photo manual-mapping entry route). Distinct from
    confidence: a low-confidence value the parent leaves as-is is still "model" --
    this field is about who supplied the value, not how sure anyone was. Lets
    page-identity accuracy be measured against only what the model actually
    produced."""


def upsert_identity(conn: sqlite3.Connection, row: PageIdentityRow) -> None:
    """Insert or correct one mapping. Re-confirming an already-stored composite
    (overlapping photo boundaries in a scanning sitting, or a parent's later
    correction) updates it rather than erroring -- same reasoning as
    `answer_keys.upsert_entry`."""
    conn.execute(
        """
        INSERT INTO page_identities
            (student_id, source_id, page_number, composite_key, schema_version,
             confirmed_at, source)
        VALUES
            (:student_id, :source_id, :page_number, :composite_key, :schema_version,
             :confirmed_at, :source)
        ON CONFLICT (student_id, source_id, composite_key) DO UPDATE SET
            page_number = excluded.page_number,
            schema_version = excluded.schema_version,
            confirmed_at = excluded.confirmed_at,
            source = excluded.source
        """,
        vars(row),
    )
    conn.commit()


def get_page_number(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    composite_key: str,
    schema_version: int,
) -> int | None:
    """Only matches a mapping confirmed at exactly `schema_version` -- a mapping
    confirmed under an older schema is invisible here (by design, not deleted;
    see `count_stale_for_source`), so a caller always passes the source's
    *current* version, never assumes one."""
    cur = conn.execute(
        """
        SELECT page_number FROM page_identities
        WHERE student_id = ? AND source_id = ? AND composite_key = ? AND schema_version = ?
        """,
        (student_id, source_id, composite_key, schema_version),
    )
    row = cur.fetchone()
    return None if row is None else int(row[0])


def count_stale_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str, current_version: int
) -> int:
    """Mappings confirmed under an older schema version than the source's current
    one -- "needs review," surfaced on the enrollment screen, never silently
    dropped or reused under the new shape."""
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM page_identities
        WHERE student_id = ? AND source_id = ? AND schema_version < ?
        """,
        (student_id, source_id, current_version),
    )
    return int(cur.fetchone()[0])
