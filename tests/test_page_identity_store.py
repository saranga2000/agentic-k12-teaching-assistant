"""k12ta.store.page_identity_schemas (the per-source, versioned, ordered component
list), k12ta.store.page_identities (the composite-key -> page_number mapping), and
k12ta.store.page_identity_resolutions (the resolution-outcome log). No test hits
the network.
"""

from __future__ import annotations

import sqlite3

from k12ta.store import (
    answer_keys,
    content,
    db,
    key_page_images,
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


def test_resolve_or_assign_returns_an_existing_mapping_unchanged() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=33,
            composite_key="CH.4\x1f4",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    page_number, was_new = page_identities.resolve_or_assign_page_number(
        conn, "s-marcus", "summer_bridge", "CH.4\x1f4", 1
    )

    assert (page_number, was_new) == (33, False)


def test_resolve_or_assign_mints_one_for_a_brand_new_composite_on_an_empty_source() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_number, was_new = page_identities.resolve_or_assign_page_number(
        conn, "s-marcus", "summer_bridge", "CH.4\x1f4", 1
    )

    assert (page_number, was_new) == (1, True)


def test_resolve_or_assign_mints_one_past_the_current_max_for_a_new_composite() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=7,
            composite_key="CH.4\x1f4",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    page_number, was_new = page_identities.resolve_or_assign_page_number(
        conn, "s-marcus", "summer_bridge", "CH.4\x1f13", 1
    )

    assert (page_number, was_new) == (8, True)


def test_resolve_or_assign_avoids_colliding_with_answer_key_entries_alone() -> None:
    """A manual answer-entry row can carry an arbitrary page_number even with no
    page_identities row at all (k12ta.keys.app.submit_manual_answers) -- a fresh
    surrogate must never collide with that table either."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=50,
            problem_number="1",
            answer_text="42",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    page_number, was_new = page_identities.resolve_or_assign_page_number(
        conn, "s-marcus", "summer_bridge", "CH.4\x1f4", 1
    )

    assert (page_number, was_new) == (51, True)


def test_resolve_or_assign_is_scoped_to_student_and_source() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-priya",
            display_name="Priya",
            grade_level=5,
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
            student_id="s-priya",
            source_id="summer_bridge",
            page_number=99,
            composite_key="CH.4\x1f4",
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    page_number, was_new = page_identities.resolve_or_assign_page_number(
        conn, "s-marcus", "summer_bridge", "CH.4\x1f4", 1
    )

    assert (page_number, was_new) == (1, True)


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


# --- get_schema_at_version --------------------------------------------------------


def test_get_schema_at_version_returns_an_older_version_unchanged_by_later_edits() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("page_number", "Page number", "15")]
    )

    v1 = page_identity_schemas.get_schema_at_version(conn, "s-marcus", "summer_bridge", 1)
    v2 = page_identity_schemas.get_schema_at_version(conn, "s-marcus", "summer_bridge", 2)

    assert [c.component_name for c in v1] == ["day"]
    assert [c.component_name for c in v2] == ["page_number"]


def test_get_schema_at_version_is_empty_for_a_version_never_saved() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert page_identity_schemas.get_schema_at_version(conn, "s-marcus", "summer_bridge", 5) == ()


# --- Gap O: schema provenance (docs/USER_WORKFLOWS.md) ----------------------------


def test_provenance_is_none_when_no_schema_exists_yet() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        is None
    )


def test_save_new_schema_defaults_to_parent_provenance() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "parent"
    )


def test_save_new_schema_records_an_explicit_provenance() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")], provenance="unconfirmed"
    )

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "unconfirmed"
    )


def test_provenance_tracks_whichever_version_is_current() -> None:
    """A later, ordinary parent-authored version overrides an earlier
    unconfirmed one -- provenance is a property of the current version, not
    something that lingers from an older one."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")], provenance="unconfirmed"
    )

    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("lesson", "Lesson", "Lesson 5")]
    )

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "parent"
    )


def test_confirm_current_schema_flips_provenance_in_place_without_a_new_version() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")], provenance="unconfirmed"
    )

    page_identity_schemas.confirm_current_schema(conn, "s-marcus", "summer_bridge")

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "parent"
    )
    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 1


def test_confirm_current_schema_is_a_no_op_with_no_schema_at_all() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    page_identity_schemas.confirm_current_schema(conn, "s-marcus", "summer_bridge")  # no raise

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        is None
    )


# --- Gap O: page_identity_resolutions.get_outcome_for_capture ---------------------


def test_get_outcome_for_capture_round_trips() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            capture_id="c-1",
            outcome="no_schema",
            resolved_page_number=None,
            created_at="2026-08-30T09:00:00+00:00",
        ),
    )

    assert (
        page_identity_resolutions.get_outcome_for_capture(conn, "s-marcus", "c-1") == "no_schema"
    )


def test_get_outcome_for_capture_is_none_when_never_resolved() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert page_identity_resolutions.get_outcome_for_capture(conn, "s-marcus", "c-1") is None


# --- backfill_page_number_schema --------------------------------------------------


def test_backfill_derives_page_number_composites_from_an_older_schema() -> None:
    """The whole point: no re-scanning, no re-typing -- every confirmed page's
    own page_number becomes the new schema's one-component composite key."""
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )
    for composite, page in (
        ("Section 1\x1fDay 1", 13),
        ("Section 1\x1fDay 2", 15),
        ("Section 2\x1fDay 1", 61),
    ):
        page_identities.upsert_identity(
            conn,
            page_identities.PageIdentityRow(
                student_id="s-marcus",
                source_id="summer_bridge",
                page_number=page,
                composite_key=composite,
                schema_version=v1,
                confirmed_at="2026-08-15T00:00:00+00:00",
            ),
        )
    v2 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("page_number", "Page number", "15")]
    )

    backfilled = page_identities.backfill_page_number_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        new_schema_version=v2,
        from_schema_version=v1,
        confirmed_at="2026-08-22T00:00:00+00:00",
    )

    assert backfilled == 3
    rows = {
        row.composite_key: row.page_number
        for row in page_identities.list_for_source_at_version(conn, "s-marcus", "summer_bridge", v2)
    }
    assert rows == {"13": 13, "15": 15, "61": 61}
    assert all(
        row.source == "backfill"
        for row in page_identities.list_for_source_at_version(conn, "s-marcus", "summer_bridge", v2)
    )


def test_backfill_is_idempotent() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=v1,
            confirmed_at="2026-08-15T00:00:00+00:00",
        ),
    )
    v2 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("page_number", "Page number", "15")]
    )

    page_identities.backfill_page_number_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        new_schema_version=v2,
        from_schema_version=v1,
        confirmed_at="2026-08-22T00:00:00+00:00",
    )
    second_run_count = page_identities.backfill_page_number_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        new_schema_version=v2,
        from_schema_version=v1,
        confirmed_at="2026-08-22T01:00:00+00:00",
    )

    assert second_run_count == 1
    rows = page_identities.list_for_source_at_version(conn, "s-marcus", "summer_bridge", v2)
    assert len(rows) == 1


# --- get_schema_version_for_capture -----------------------------------------------


def test_get_schema_version_for_capture_round_trips() -> None:
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
            created_at="2026-08-22T00:00:00+00:00",
            seen_values_json='{"day": "Day 2"}',
            schema_version=1,
        ),
    )

    assert page_identity_resolutions.get_schema_version_for_capture(conn, "s-marcus", "c-1") == 1


def test_get_schema_version_for_capture_is_none_for_a_row_without_one() -> None:
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
            created_at="2026-08-22T00:00:00+00:00",
        ),
    )

    assert page_identity_resolutions.get_schema_version_for_capture(conn, "s-marcus", "c-1") is None


def test_get_schema_version_for_capture_is_none_when_never_resolved() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert (
        page_identity_resolutions.get_schema_version_for_capture(conn, "s-marcus", "no-such")
        is None
    )


# --- key_page_images ---------------------------------------------------------


def test_key_page_image_round_trips() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)

    assert key_page_images.get_image_path(conn, "s-marcus", "summer_bridge", 15) is None

    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            image_path="/data/key_captures/abc.jpg",
            confirmed_at="2026-08-22T00:00:00+00:00",
        ),
    )

    assert (
        key_page_images.get_image_path(conn, "s-marcus", "summer_bridge", 15)
        == "/data/key_captures/abc.jpg"
    )


def test_key_page_image_re_scan_replaces_the_stored_path() -> None:
    conn = _migrated_connection()
    _seed_marcus_with_summer_bridge(conn)
    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            image_path="/data/key_captures/old.jpg",
            confirmed_at="2026-08-22T00:00:00+00:00",
        ),
    )

    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            image_path="/data/key_captures/new.jpg",
            confirmed_at="2026-08-22T01:00:00+00:00",
        ),
    )

    assert (
        key_page_images.get_image_path(conn, "s-marcus", "summer_bridge", 15)
        == "/data/key_captures/new.jpg"
    )
