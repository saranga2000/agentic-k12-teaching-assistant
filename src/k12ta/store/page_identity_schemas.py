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


def save_new_schema(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    components: Sequence[tuple[str, str, str | None]],
) -> int:
    """Insert `components` (component_name, label, example, in the desired display
    order) as the next schema_version for this source -- 1 if none existed, one
    past whatever the current version was otherwise. Returns the new version.
    Never touches a prior version's rows: a schema is revisable, not a one-shot
    commitment, and an old version must stay exactly as it was so mappings
    confirmed under it remain honestly attributable, not silently reinterpreted."""
    next_version = get_current_version(conn, student_id, source_id) + 1
    for position, (component_name, label, example) in enumerate(components):
        conn.execute(
            """
            INSERT INTO page_identity_schemas
                (student_id, source_id, schema_version, component_name, label, example, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, source_id, next_version, component_name, label, example, position),
        )
    conn.commit()
    return next_version
