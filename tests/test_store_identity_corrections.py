"""Gap O (docs/USER_WORKFLOWS.md): the child-facing "a grown-up changed how
pages are identified" notice. No test hits the network -- pure store logic.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import content, db, identity_corrections, migrate, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_marcus_with_source(conn: sqlite3.Connection) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-marcus",
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="rsm",
            label="RSM",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=True,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )


def test_get_correction_is_none_when_nothing_was_recorded() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_source(conn)

    assert identity_corrections.get_correction(conn, "s-marcus", "rsm") is None


def test_record_and_get_correction_round_trips() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_source(conn)

    identity_corrections.record_correction(conn, "s-marcus", "rsm", "2026-08-30T10:00:00+00:00")

    result = identity_corrections.get_correction(conn, "s-marcus", "rsm")
    assert result == "2026-08-30T10:00:00+00:00"


def test_a_fresh_correction_overwrites_rather_than_stacks() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_source(conn)
    identity_corrections.record_correction(conn, "s-marcus", "rsm", "2026-08-30T10:00:00+00:00")

    identity_corrections.record_correction(conn, "s-marcus", "rsm", "2026-08-30T11:00:00+00:00")

    result = identity_corrections.get_correction(conn, "s-marcus", "rsm")
    assert result == "2026-08-30T11:00:00+00:00"


def test_dismiss_correction_clears_it() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_source(conn)
    identity_corrections.record_correction(conn, "s-marcus", "rsm", "2026-08-30T10:00:00+00:00")

    identity_corrections.dismiss_correction(conn, "s-marcus", "rsm")

    assert identity_corrections.get_correction(conn, "s-marcus", "rsm") is None


def test_dismiss_correction_with_nothing_to_dismiss_is_a_no_op() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_source(conn)

    identity_corrections.dismiss_correction(conn, "s-marcus", "rsm")  # must not raise

    assert identity_corrections.get_correction(conn, "s-marcus", "rsm") is None
