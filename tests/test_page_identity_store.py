"""k12ta.store.page_identity_schemas (the per-source, versioned, ordered component
list), k12ta.store.page_identities (the composite-key -> page_number mapping), and
k12ta.store.page_identity_resolutions (the resolution-outcome log). No test hits
the network.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import (
    content,
    db,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    students,
)


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
        ),
    )


# --- page_identity_schemas -------------------------------------------------------


def test_a_source_with_no_schema_has_version_zero_and_no_components() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 0
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()


def test_saving_the_first_schema_is_version_one_in_position_order() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    version = page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    assert version == 1
    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 1
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["section", "day"]
    assert [c.label for c in schema] == ["Section", "Day"]
    assert [c.example for c in schema] == ["Section 1", "Day 5"]
    assert [c.position for c in schema] == [0, 1]


def test_editing_a_schema_adds_a_new_version_without_touching_the_old_one() -> None:
    """A schema is revisable, not a one-shot commitment -- editing it must not
    mutate or delete the version that was already in use."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    second_version = page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    assert second_version == 2
    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 2
    current = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in current] == ["section", "day"]
    # Version 1 still exists, untouched, in the table -- just no longer "current".
    old_rows = conn.execute(
        "SELECT component_name FROM page_identity_schemas "
        "WHERE student_id = ? AND source_id = ? AND schema_version = 1",
        ("s-marcus", "summer_bridge"),
    ).fetchall()
    assert [row[0] for row in old_rows] == ["day"]


def test_schemas_are_scoped_to_student_and_source() -> None:
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
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    assert page_identity_schemas.get_current_version(conn, "s-priya", "summer_bridge") == 0
    assert page_identity_schemas.get_current_schema(conn, "s-priya", "summer_bridge") == ()


# --- page_identities (composite key) ---------------------------------------------


def test_get_page_number_for_unknown_composite_is_none() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 11", 1)
        is None
    )


def test_upsert_then_get_round_trips() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            composite_key="Section 1\x1fDay 11",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 11", 1)
        == 33
    )


def test_a_mapping_confirmed_under_an_older_schema_version_is_invisible_but_not_deleted() -> None:
    """The explicit requirement: a schema change must flag an existing mapping for
    review, never drop it or silently reinterpret it under the new shape."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            composite_key="Day 11",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    # The schema was edited (e.g. "section" added), bumping the current version to 2.
    # A lookup at the new version must not find the old-version mapping...
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11", 2) is None
    # ...but the row itself is still there, untouched, at its original version.
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11", 1) == 33
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 1


def test_count_stale_counts_only_rows_below_the_current_version() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key="Section 1\x1fDay 2",
            schema_version=2,
            confirmed_at="2026-08-14T08:05:00+00:00",
        ),
    )

    assert page_identities.count_stale_for_source(conn, "s-marcus", "summer_bridge", 2) == 1
    assert page_identities.count_stale_for_source(conn, "s-marcus", "summer_bridge", 1) == 0


def test_list_for_source_at_version_returns_only_rows_at_that_version() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Section 1\x1fDay 1",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key="Section 1\x1fDay 2",
            schema_version=2,
            confirmed_at="2026-08-14T08:05:00+00:00",
        ),
    )

    rows = page_identities.list_for_source_at_version(conn, "s-marcus", "summer_bridge", 2)

    assert [r.page_number for r in rows] == [15]


def test_list_for_source_at_version_is_scoped_to_student_and_source() -> None:
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
            page_number=13,
            composite_key="Day 1",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-priya",
            source_id="summer_bridge",
            page_number=99,
            composite_key="Day 1",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    rows = page_identities.list_for_source_at_version(conn, "s-marcus", "summer_bridge", 1)

    assert [r.page_number for r in rows] == [13]


def test_reconfirming_the_same_composite_updates_rather_than_duplicates() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    row = page_identities.PageIdentityRow(
        student_id="s-marcus",
        source_id="summer_bridge",
        page_number=33,
        composite_key="Day 11",
        schema_version=1,
        confirmed_at="2026-08-14T08:00:00+00:00",
    )
    page_identities.upsert_identity(conn, row)

    corrected = page_identities.PageIdentityRow(
        student_id="s-marcus",
        source_id="summer_bridge",
        page_number=35,  # a correction, not a new day
        composite_key="Day 11",
        schema_version=1,
        confirmed_at="2026-08-14T08:05:00+00:00",
    )
    page_identities.upsert_identity(conn, corrected)

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11", 1) == 35
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
            composite_key="Day 11",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE student_id = ? AND composite_key = ?",
        ("s-marcus", "Day 11"),
    ).fetchone()
    assert row[0] == "model"


def test_upsert_persists_manual_source_and_a_correction_can_overwrite_it() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            composite_key="Day 11",
            schema_version=1,
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
            composite_key="Day 11",
            schema_version=1,
            confirmed_at="2026-08-14T08:05:00+00:00",
            source="manual",
        ),
    )

    row = conn.execute(
        "SELECT page_number, source FROM page_identities "
        "WHERE student_id = ? AND composite_key = ?",
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
            composite_key="Day 11",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 11", 1) == 33
    assert page_identities.get_page_number(conn, "s-priya", "summer_bridge", "Day 11", 1) is None


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
        ("no_mapping", None),
        ("no_mapping", None),
        ("conflicting", None),
        ("partial", None),
        ("no_markers", None),
        ("no_schema", None),
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

    assert counts == {
        "resolved": 2,
        "below_floor": 1,
        "no_mapping": 2,
        "conflicting": 1,
        "partial": 1,
        "no_markers": 1,
        "no_schema": 1,
    }


def test_seen_values_json_round_trips_and_defaults_to_none() -> None:
    """Only populated for the "ask" case (a PARTIAL identity with exactly one
    missing component and something to offer) -- every other outcome leaves
    it None, not an empty string or an empty object."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            capture_id="c-1",
            outcome="partial",
            resolved_page_number=None,
            created_at="2026-08-14T08:00:00+00:00",
            seen_values_json='{"day": "Day 2"}',
        ),
    )
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            capture_id="c-2",
            outcome="resolved",
            resolved_page_number=15,
            created_at="2026-08-14T08:05:00+00:00",
        ),
    )

    assert (
        page_identity_resolutions.get_seen_values_for_capture(conn, "s-marcus", "c-1")
        == '{"day": "Day 2"}'
    )
    assert page_identity_resolutions.get_seen_values_for_capture(conn, "s-marcus", "c-2") is None


def test_get_seen_values_for_an_unknown_capture_is_none() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert (
        page_identity_resolutions.get_seen_values_for_capture(conn, "s-marcus", "no-such") is None
    )


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
