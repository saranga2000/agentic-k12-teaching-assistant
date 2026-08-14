"""k12ta.store.page_identities (the day/code -> page_number mapping) and
k12ta.store.page_identity_resolutions (the resolution-outcome log). No test hits
the network.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import content, db, migrate, page_identities, page_identity_resolutions, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_marcus_with_summer_bridge(conn: sqlite3.Connection) -> None:
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
            page_identity_kind="day_or_unit_banner",
        ),
    )


# --- page_identities -----------------------------------------------------------


def test_get_page_number_for_unknown_marker_is_none() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11") is None


def test_upsert_then_get_round_trips() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            identifier_value="Day 11",
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11") == 33


def test_reconfirming_the_same_marker_updates_rather_than_duplicates() -> None:
    """A parent re-scanning an overlapping key page shouldn't produce two
    conflicting mappings for the same marker -- the later confirm wins, matching
    answer_keys.upsert_entry's own reasoning."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    row = page_identities.PageIdentityRow(
        student_id="s-marcus",
        source_id="summer_bridge",
        page_number=33,
        identifier_value="Day 11",
        confirmed_at="2026-08-14T08:00:00+00:00",
    )
    page_identities.upsert_identity(conn, row)

    corrected = page_identities.PageIdentityRow(
        student_id="s-marcus",
        source_id="summer_bridge",
        page_number=35,  # a correction, not a new day
        identifier_value="Day 11",
        confirmed_at="2026-08-14T08:05:00+00:00",
    )
    page_identities.upsert_identity(conn, corrected)

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11") == 35
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 1


def test_upsert_defaults_source_to_model() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            identifier_value="Day 11",
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE student_id = ? AND identifier_value = ?",
        ("s-marcus", "Day 11"),
    ).fetchone()
    assert row[0] == "model"


def test_upsert_persists_manual_source_and_a_correction_can_overwrite_it() -> None:
    """A parent correcting a confidently-extracted-but-wrong identifier must be
    recorded as manual -- the model being confident does not make it right, and
    the eval must not count the correction as a model success."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            identifier_value="Day 11",
            confirmed_at="2026-08-14T08:00:00+00:00",
            source="model",
        ),
    )

    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=35,
            identifier_value="Day 11",
            confirmed_at="2026-08-14T08:05:00+00:00",
            source="manual",
        ),
    )

    row = conn.execute(
        "SELECT page_number, source FROM page_identities "
        "WHERE student_id = ? AND identifier_value = ?",
        ("s-marcus", "Day 11"),
    ).fetchone()
    assert row[0] == 35
    assert row[1] == "manual"


def test_lookup_is_scoped_to_student_and_source() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-priya",
            display_name="Priya",
            grade_level=4,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-priya",
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
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            identifier_value="Day 11",
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11") == 33
    assert page_identities.get_page_number(conn, "s-priya", "summer_bridge", "Day 11") is None


# --- page_identity_resolutions ---------------------------------------------------


def test_count_outcomes_with_no_rows_is_empty() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert (
        page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge") == {}
    )


def test_count_outcomes_groups_by_outcome() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    rows = [
        ("resolved", 33),
        ("resolved", 35),
        ("below_floor", None),
        ("not_found", None),
        ("not_found", None),
        ("conflicting", None),
    ]
    for i, (outcome, page_number) in enumerate(rows):
        page_identity_resolutions.insert_resolution(
            conn,
            page_identity_resolutions.PageIdentityResolutionRow(
                student_id="s-marcus",
                source_id="summer_bridge",
                capture_id=f"c-{i}",
                outcome=outcome,
                resolved_page_number=page_number,
                created_at="2026-08-14T08:00:00+00:00",
            ),
        )

    counts = page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge")

    assert counts == {"resolved": 2, "below_floor": 1, "not_found": 2, "conflicting": 1}


def test_counts_are_scoped_to_student_and_source() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            capture_id="c-1",
            outcome="resolved",
            resolved_page_number=33,
            created_at="2026-08-14T08:00:00+00:00",
        ),
    )

    assert (
        page_identity_resolutions.count_outcomes_for_source(conn, "s-other", "summer_bridge") == {}
    )
