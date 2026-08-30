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
    screen, or through the no-photo manual-mapping entry route), "backfill"
    when neither happened -- an older schema's already-confirmed mapping was
    mechanically re-expressed under a new schema's shape, nothing freshly
    extracted or typed (see `backfill_page_number_schema`). Distinct from
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


def resolve_or_assign_page_number(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    composite_key: str,
    schema_version: int,
) -> tuple[int, bool]:
    """The page_number a 2+-component schema's composite should be stored under
    -- looked up if already confirmed, minted fresh otherwise. Exists because a
    single schema component's raw printed value (a chapter's page footer digit,
    say) is not safe to trust as this source's page_number once a second
    component is needed to disambiguate it: two different chapters' printed
    "page 4" would otherwise collide in answer_key_entries' primary key and one
    would silently overwrite the other's answer key. A 0/1-component schema
    never calls this -- the human confirming it keeps typing/editing a literal
    integer directly, exactly as today, since that raw value already *is*
    source-wide unique in that case (Summer Bridge).

    A fresh assignment is `1 + the highest page_number already used anywhere for
    this source`, scanned across both this table and answer_key_entries -- a
    manual answer-entry row (k12ta.keys.app.submit_manual_answers) can exist
    with an arbitrary parent-typed page_number even with no schema at all, so a
    new surrogate must never collide with that table either, not only this one.

    Does not call upsert_identity itself -- same convention as every other
    function here; the caller owns the actual write, along with confirmed_at
    and source. Single SQLite writer, one process: calling this and then
    upsert_identity within the same request, before the implicit commit, is
    all the concurrency safety this needs -- there is no worker pool or
    multi-process writer anywhere in this codebase to race against.

    Returns (page_number, was_newly_assigned)."""
    existing = get_page_number(conn, student_id, source_id, composite_key, schema_version)
    if existing is not None:
        return existing, False
    cur = conn.execute(
        """
        SELECT COALESCE(MAX(page_number), 0) FROM (
            SELECT page_number FROM page_identities
                WHERE student_id = ? AND source_id = ?
            UNION ALL
            SELECT page_number FROM answer_key_entries
                WHERE student_id = ? AND source_id = ?
        )
        """,
        (student_id, source_id, student_id, source_id),
    )
    (current_max,) = cur.fetchone()
    return int(current_max) + 1, True


def list_for_source_at_version(
    conn: sqlite3.Connection, student_id: str, source_id: str, schema_version: int
) -> list[PageIdentityRow]:
    """Every confirmed mapping for this source at exactly `schema_version` --
    for k12ta.grading.page_identity.resolve_partial, which needs to see every
    composite this source's parent has already confirmed (decomposed back into
    per-component values) to judge whether a PARTIAL identity's one missing
    component is safely inferable. A stale (older-version) mapping is excluded
    for the same reason get_page_number never matches one: it was confirmed
    under a schema shape that no longer applies."""
    cur = conn.execute(
        """
        SELECT student_id, source_id, page_number, composite_key, schema_version,
               confirmed_at, source
        FROM page_identities
        WHERE student_id = ? AND source_id = ? AND schema_version = ?
        """,
        (student_id, source_id, schema_version),
    )
    return [PageIdentityRow(*row) for row in cur.fetchall()]


def backfill_page_number_schema(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    new_schema_version: int,
    from_schema_version: int,
    confirmed_at: str,
) -> int:
    """Derives confirmed mappings for a brand-new single-component
    "page_number" schema entirely from what an older schema has already
    confirmed -- every row at `from_schema_version` already carries the real
    page_number as a plain column, so its own value *is* the new schema's
    one-component composite key (`str(page_number)`, no separator needed).
    No parent re-entry, no re-scanning: the physical page and its printed
    page number never changed, only which marker the schema now leads with
    (docs/ROADMAP.md's M3.7 -- Summer Bridge's Day+Section schema replaced by
    a page-number-primary one because Section is never printed on an
    exercise page).

    `source="model"` would overstate it -- nothing was freshly extracted and
    confirmed here, an existing confirmation was mechanically re-expressed
    under a new schema's shape -- so every backfilled row is written with
    source="backfill" instead, its own distinct provenance value, so a later
    accuracy measurement can still tell "the model actually read this page
    number" apart from "this page number was inherited from an older
    schema's confirmation" once real page-number extractions start landing
    alongside it.

    Returns how many rows were backfilled. Safe to re-run: upsert_identity's
    own ON CONFLICT (student_id, source_id, composite_key) makes this
    idempotent for the same new_schema_version."""
    source_rows = list_for_source_at_version(conn, student_id, source_id, from_schema_version)
    for row in source_rows:
        upsert_identity(
            conn,
            PageIdentityRow(
                student_id=student_id,
                source_id=source_id,
                page_number=row.page_number,
                composite_key=str(row.page_number),
                schema_version=new_schema_version,
                confirmed_at=confirmed_at,
                source="backfill",
            ),
        )
    return len(source_rows)


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
