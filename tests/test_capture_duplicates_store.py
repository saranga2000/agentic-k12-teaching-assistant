"""k12ta.store.capture_duplicates: a parent's manual "this photo is the same page
as that one," the fallback for unresolved captures automatic dedup can't reach.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import capture_duplicates, captures, content, db, migrate, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_captures(conn: sqlite3.Connection, *capture_ids: str) -> None:
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
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    for capture_id in capture_ids:
        captures.insert_page_capture(
            conn,
            captures.PageCaptureRow(
                student_id="s-marcus",
                capture_id=capture_id,
                assignment_id="a-1",
                captured_at="2026-08-13T08:00:00+00:00",
                image_path="/tmp/does-not-matter.jpg",
            ),
        )


def test_get_duplicate_map_is_empty_with_nothing_marked() -> None:
    conn = _migrated_connection()
    _seed_captures(conn, "c-a", "c-b")

    assert capture_duplicates.get_duplicate_map(conn, "s-marcus") == {}


def test_mark_duplicate_round_trips() -> None:
    conn = _migrated_connection()
    _seed_captures(conn, "c-a", "c-b")

    capture_duplicates.mark_duplicate(
        conn,
        capture_duplicates.CaptureDuplicateRow(
            student_id="s-marcus",
            capture_id="c-b",
            duplicate_of_capture_id="c-a",
            marked_at="2026-08-22T00:00:00+00:00",
        ),
    )

    assert capture_duplicates.get_duplicate_map(conn, "s-marcus") == {"c-b": "c-a"}


def test_re_marking_overwrites_the_previous_target() -> None:
    conn = _migrated_connection()
    _seed_captures(conn, "c-a", "c-b", "c-c")
    capture_duplicates.mark_duplicate(
        conn,
        capture_duplicates.CaptureDuplicateRow(
            student_id="s-marcus",
            capture_id="c-b",
            duplicate_of_capture_id="c-a",
            marked_at="2026-08-22T00:00:00+00:00",
        ),
    )

    capture_duplicates.mark_duplicate(
        conn,
        capture_duplicates.CaptureDuplicateRow(
            student_id="s-marcus",
            capture_id="c-b",
            duplicate_of_capture_id="c-c",
            marked_at="2026-08-22T01:00:00+00:00",
        ),
    )

    assert capture_duplicates.get_duplicate_map(conn, "s-marcus") == {"c-b": "c-c"}


def test_duplicate_map_is_scoped_to_student() -> None:
    conn = _migrated_connection()
    _seed_captures(conn, "c-a", "c-b")
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-other",
            display_name="Other",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    capture_duplicates.mark_duplicate(
        conn,
        capture_duplicates.CaptureDuplicateRow(
            student_id="s-marcus",
            capture_id="c-b",
            duplicate_of_capture_id="c-a",
            marked_at="2026-08-22T00:00:00+00:00",
        ),
    )

    assert capture_duplicates.get_duplicate_map(conn, "s-other") == {}
