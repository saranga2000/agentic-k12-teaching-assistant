"""The per-source, versioned, ordered list of identity components a parent has
taught the system -- e.g. Summer Bridge's (section, day). Never mutated in place:
saving a new schema always inserts the next version rather than updating an old
one, so an old version stays queryable after an edit -- same additive philosophy as
k12ta.store.answer_key_audit. A source's current schema is whichever version is
highest; zero rows means no schema has been learned yet, and
k12ta.grading.page_identity.resolve() refuses honestly (NO_SCHEMA) until one has.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SchemaComponent:
    component_name: str
    """Stable internal key, e.g. "section" -- what a candidates dict and a confirm
    screen's field names key off of. Never shown to a parent directly."""
    label: str
    """Parent-facing name, e.g. "Section" -- what the enrollment screen and a
    needs-human message use."""
    example: str | None
    """A concrete example as printed, e.g. "Section 1", shown alongside the label
    on the schema editor so a parent recognises what they're naming."""
    position: int
    """Display and composite-key order, 0-indexed."""


def get_current_version(conn: sqlite3.Connection, student_id: str, source_id: str) -> int:
    """0 if no schema has ever been saved for this source -- the NO_SCHEMA case."""
    cur = conn.execute(
        "SELECT MAX(schema_version) FROM page_identity_schemas "
        "WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    version = cur.fetchone()[0]
    return int(version) if version is not None else 0


def get_current_schema(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> tuple[SchemaComponent, ...]:
    """Ordered by position. Empty when the source has no schema yet."""
    version = get_current_version(conn, student_id, source_id)
    if version == 0:
        return ()
    cur = conn.execute(
        """
        SELECT component_name, label, example, position FROM page_identity_schemas
        WHERE student_id = ? AND source_id = ? AND schema_version = ?
        ORDER BY position
        """,
        (student_id, source_id, version),
    )
    return tuple(SchemaComponent(*row) for row in cur.fetchall())


def get_schema_at_version(
    conn: sqlite3.Connection, student_id: str, source_id: str, schema_version: int
) -> tuple[SchemaComponent, ...]:
    """Same shape as `get_current_schema`, but a specific version rather than
    whichever is highest -- for a caller that deliberately wants to resolve
    against a source's *older* schema, e.g. as a fallback when the current
    schema's own markers aren't legible on a given photo but a prior schema's
    are (see `k12ta.grading.page_identity.resolve_with_schema_history`).
    Empty if nothing was ever saved at this version."""
    cur = conn.execute(
        """
        SELECT component_name, label, example, position FROM page_identity_schemas
        WHERE student_id = ? AND source_id = ? AND schema_version = ?
        ORDER BY position
        """,
        (student_id, source_id, schema_version),
    )
    return tuple(SchemaComponent(*row) for row in cur.fetchall())


def save_new_schema(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    components: Sequence[tuple[str, str, str | None]],
    provenance: str = "parent",
) -> int:
    """Insert `components` (component_name, label, example, in the desired display
    order) as the next schema_version for this source -- 1 if none existed, one
    past whatever the current version was otherwise. Returns the new version.
    Never touches a prior version's rows: a schema is revisable, not a one-shot
    commitment, and an old version must stay exactly as it was so mappings
    confirmed under it remain honestly attributable, not silently reinterpreted.

    `provenance` (Gap O, docs/USER_WORKFLOWS.md) is "parent" for every
    ordinary caller -- k12ta.web.app's bootstrap-schema submission is the one
    exception, passing "unconfirmed" for a child/app-proposed first schema
    that hasn't been reviewed. Recorded on every row of this version (a
    version-level fact, not a per-component one) purely so
    get_current_schema_provenance can read it back without a second table."""
    next_version = get_current_version(conn, student_id, source_id) + 1
    for position, (component_name, label, example) in enumerate(components):
        conn.execute(
            """
            INSERT INTO page_identity_schemas
                (student_id, source_id, schema_version, component_name, label, example,
                 position, provenance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_id,
                source_id,
                next_version,
                component_name,
                label,
                example,
                position,
                provenance,
            ),
        )
    conn.commit()
    return next_version


def get_current_schema_provenance(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> str | None:
    """"parent" or "unconfirmed" for the source's current schema version, or
    None if it has no schema at all yet. Gap O: this is what decides whether
    a result graded under this schema is shown to the child as provisional,
    and whether k12ta.keys's identity_schema_screen shows the "not yet
    checked" banner. Safe to compute from the current version alone -- see
    docs/USER_WORKFLOWS.md §3.4: bootstrapping only ever happens at
    schema_version 1 (NO_SCHEMA), so a source can never have an already-
    parent-confirmed later version sitting on top of an unconfirmed one; the
    moment a parent acts on version 1, every later version is "parent" too."""
    version = get_current_version(conn, student_id, source_id)
    if version == 0:
        return None
    cur = conn.execute(
        """
        SELECT provenance FROM page_identity_schemas
        WHERE student_id = ? AND source_id = ? AND schema_version = ?
        LIMIT 1
        """,
        (student_id, source_id, version),
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])


def confirm_current_schema(conn: sqlite3.Connection, student_id: str, source_id: str) -> None:
    """Gap O: a parent accepting a child/app-proposed schema exactly as-is --
    flips the current version's provenance to "parent" in place, no new
    version, no regrade needed (every capture already graded under this
    schema graded correctly; only the trust label changes). A no-op, not an
    error, when the source has no schema or is already parent-authored."""
    version = get_current_version(conn, student_id, source_id)
    if version == 0:
        return
    conn.execute(
        """
        UPDATE page_identity_schemas SET provenance = 'parent'
        WHERE student_id = ? AND source_id = ? AND schema_version = ?
        """,
        (student_id, source_id, version),
    )
    conn.commit()
