"""Content sources and the assignments drawn from them.

An assignment's foreign key is `(student_id, source_id)`, so an assignment can only
ever point at a content source configured for the same student.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ContentSourceRow:
    student_id: str
    source_id: str
    label: str
    kind: str
    subject: str
    has_answer_key: bool
    """docs/ROADMAP.md's V1 "two program paths": True is **keyed** (the parent
    supplies answers; a page with no key on file is never evaluated, it waits
    and the parent is notified), False is **keyless** (the AI generates the
    answers itself, V1's core evaluation capability, not a fallback). Asked
    at enrollment setup, switchable later via set_has_answer_key -- switching
    never retroactively regrades anything already on file, since it is a
    plain field update with no regrade call anywhere near it."""
    graded_by_someone_else: bool
    default_mode: str
    typical_session_minutes: int
    standards_frame: str | None = None
    page_identity_kind: str | None = None
    """One of "day_or_unit_banner", "printed_worksheet_code", "unique_problem_ids",
    "printed_page_number", or None if not yet configured for this source. Per-source,
    never a global assumption -- see docs/ROADMAP.md's page-identity discussion and
    k12ta.grading.page_identity, the only place this value is interpreted."""
    archived: bool = False
    """docs/ROADMAP.md's V1 "Archiving" (migration 0025): a parent's answer to
    a school-year rollover, or any program that's simply done. Once true, the
    child can no longer upload to this source (k12ta.web.app.submit_capture's
    own check) -- everything already evaluated stays fully visible to both
    parent and child, and the parent's review queue on it stays workable, so
    archiving never strands a pending item. Never inferred, never set at
    creation -- see set_archived below."""


def insert_content_source(conn: sqlite3.Connection, row: ContentSourceRow) -> None:
    conn.execute(
        """
        INSERT INTO content_sources
            (student_id, source_id, label, kind, subject, has_answer_key,
             graded_by_someone_else, default_mode, typical_session_minutes,
             standards_frame, page_identity_kind)
        VALUES
            (:student_id, :source_id, :label, :kind, :subject, :has_answer_key,
             :graded_by_someone_else, :default_mode, :typical_session_minutes,
             :standards_frame, :page_identity_kind)
        """,
        vars(row),
    )
    conn.commit()


def set_page_identity_kind(
    conn: sqlite3.Connection, student_id: str, source_id: str, page_identity_kind: str | None
) -> None:
    """The parent-facing enrollment screen's save action, and the only intended
    way this column is ever written after initial insert -- never by hand-editing
    the database. `None` is a real, re-selectable choice ("not sure yet"), not an
    error: it restores the honest NOT_FOUND refusal `k12ta.grading.page_identity
    .resolve` already gives a source with no configured kind."""
    conn.execute(
        "UPDATE content_sources SET page_identity_kind = ? WHERE student_id = ? AND source_id = ?",
        (page_identity_kind, student_id, source_id),
    )
    conn.commit()


def set_has_answer_key(
    conn: sqlite3.Connection, student_id: str, source_id: str, has_answer_key: bool
) -> None:
    """A parent switching a program between keyed and keyless (docs/ROADMAP.md's
    V1 "two program paths") -- a parent who gives up chasing a key, or a school
    that finally sends one. Never retroactively regrades: this is a plain field
    update with no call anywhere near it to replay_source or any other regrade
    path, per this codebase's standing rule that a regrade is always a
    deliberate, separate parent action, not a side effect of a settings change."""
    conn.execute(
        "UPDATE content_sources SET has_answer_key = ? WHERE student_id = ? AND source_id = ?",
        (has_answer_key, student_id, source_id),
    )
    conn.commit()


def set_archived(conn: sqlite3.Connection, student_id: str, source_id: str, archived: bool) -> None:
    """docs/ROADMAP.md's V1 "Archiving". Blocking new child uploads is enforced
    at the actual upload route (k12ta.web.app.submit_capture), not here -- this
    function only flips the flag every read path checks. Reversible (a parent
    can un-archive), unlike delete_content_source below."""
    conn.execute(
        "UPDATE content_sources SET archived = ? WHERE student_id = ? AND source_id = ?",
        (archived, student_id, source_id),
    )
    conn.commit()


def update_content_source_label(
    conn: sqlite3.Connection, student_id: str, source_id: str, label: str
) -> None:
    """Renaming carries no data-loss risk, unlike delete_content_source below
    -- always allowed. Fixes the seeded-placeholder-label gap found
    2026-08-22 (docs/ROADMAP.md): `seed_dev_data` creates sources like
    "Outside maths programme homework" whether or not a family uses them,
    and there was no way to correct one to its real name."""
    conn.execute(
        "UPDATE content_sources SET label = ? WHERE student_id = ? AND source_id = ?",
        (label, student_id, source_id),
    )
    conn.commit()


def source_has_real_activity(conn: sqlite3.Connection, student_id: str, source_id: str) -> bool:
    """A photographed page or a confirmed answer-key entry is irreplaceable
    child/parent effort; an empty `assignments` row is not -- one gets
    created every day a student opens the capture screen for a scheduled
    source, whether or not she ever takes a photo (k12ta.ingest.schedule.
    get_or_create_todays_assignment), so its mere existence must not block
    deleting an otherwise untouched source."""
    capture_count = conn.execute(
        """
        SELECT COUNT(*) FROM page_captures pc
        JOIN assignments a ON a.student_id = pc.student_id AND a.assignment_id = pc.assignment_id
        WHERE pc.student_id = ? AND a.source_id = ?
        """,
        (student_id, source_id),
    ).fetchone()[0]
    if capture_count > 0:
        return True
    key_count = conn.execute(
        "SELECT COUNT(*) FROM answer_key_entries WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    ).fetchone()[0]
    return bool(key_count > 0)


def delete_content_source(conn: sqlite3.Connection, student_id: str, source_id: str) -> bool:
    """Removes a source and its inert scaffolding (empty assignments, a
    weekly-schedule entry, an identity schema or manual mapping never used
    to grade anything, a feedback-mode override) -- but refuses outright,
    leaving everything untouched, the moment `source_has_real_activity`
    finds a real photographed page or a confirmed answer key. This is the
    fix for the gap found 2026-08-22 (docs/ROADMAP.md): `seed_dev_data`
    creates sources (`daily_fluency_drill`, `school_homework`) whether or
    not a family uses them, and a generic placeholder sitting in the
    enrollment list as if it were real content is worse than an empty one --
    but nothing here will ever discard a child's real work to get there.
    Returns whether the delete happened."""
    if source_has_real_activity(conn, student_id, source_id):
        return False
    for table in (
        "weekly_default_sources",
        "page_identity_schemas",
        "page_identities",
        "page_identity_resolutions",
        "answer_key_audit_log",
        "key_page_images",
        "policy_overrides",
        "policy_override_audit_log",
        "assignments",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE student_id = ? AND source_id = ?",
            (student_id, source_id),
        )
    conn.execute(
        "DELETE FROM content_sources WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    conn.commit()
    return True


def get_content_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> ContentSourceRow | None:
    cur = conn.execute(
        "SELECT * FROM content_sources WHERE student_id = ? AND source_id = ?",
        (student_id, source_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return _row_to_content_source(row)


def list_content_sources(conn: sqlite3.Connection, student_id: str) -> list[ContentSourceRow]:
    """Every content source configured for one student, for the M2.2 "change
    assignment" picker. Not the exception `list_students` is: this still takes
    student_id and only ever returns that student's rows."""
    cur = conn.execute(
        "SELECT * FROM content_sources WHERE student_id = ? ORDER BY label", (student_id,)
    )
    return [_row_to_content_source(row) for row in cur.fetchall()]


def _row_to_content_source(row: sqlite3.Row) -> ContentSourceRow:
    data = dict(row)
    data["has_answer_key"] = bool(data["has_answer_key"])
    data["graded_by_someone_else"] = bool(data["graded_by_someone_else"])
    data["archived"] = bool(data["archived"])
    return ContentSourceRow(**data)


@dataclass(frozen=True)
class AssignmentRow:
    student_id: str
    assignment_id: str
    source_id: str
    created_at: str
    label: str | None = None


def insert_assignment(conn: sqlite3.Connection, row: AssignmentRow) -> None:
    conn.execute(
        """
        INSERT INTO assignments (student_id, assignment_id, source_id, label, created_at)
        VALUES (:student_id, :assignment_id, :source_id, :label, :created_at)
        """,
        vars(row),
    )
    conn.commit()


def get_assignment(
    conn: sqlite3.Connection, student_id: str, assignment_id: str
) -> AssignmentRow | None:
    cur = conn.execute(
        "SELECT * FROM assignments WHERE student_id = ? AND assignment_id = ?",
        (student_id, assignment_id),
    )
    row = cur.fetchone()
    return None if row is None else AssignmentRow(**dict(row))
