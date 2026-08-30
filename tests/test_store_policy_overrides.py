"""k12ta.store.policy_overrides and policy_override_audit: the persisted
supply for k12ta.domain.policy.resolve_mode's parent_override parameter,
which has existed since M3.2 with nothing ever calling it with a real value
(docs/ROADMAP.md M3's own remaining bullet).
"""

from __future__ import annotations

import sqlite3

from k12ta.store import content, db, migrate, policy_override_audit, policy_overrides, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_source(conn: sqlite3.Connection) -> None:
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
            default_mode="diagnostic_only",
            typical_session_minutes=30,
        ),
    )


def test_get_override_is_none_with_nothing_set() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    assert policy_overrides.get_override(conn, "s-marcus", "summer_bridge") is None


def test_set_override_round_trips() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    policy_overrides.set_override(
        conn,
        policy_overrides.PolicyOverrideRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            mode="full",
            set_at="2026-08-29T10:00:00+00:00",
        ),
    )

    row = policy_overrides.get_override(conn, "s-marcus", "summer_bridge")
    assert row is not None
    assert row.mode == "full"


def test_set_override_overwrites_a_previous_one() -> None:
    conn = _migrated_connection()
    _seed_source(conn)
    policy_overrides.set_override(
        conn,
        policy_overrides.PolicyOverrideRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            mode="full",
            set_at="2026-08-29T10:00:00+00:00",
        ),
    )

    policy_overrides.set_override(
        conn,
        policy_overrides.PolicyOverrideRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            mode="fluency",
            set_at="2026-08-29T11:00:00+00:00",
        ),
    )

    row = policy_overrides.get_override(conn, "s-marcus", "summer_bridge")
    assert row is not None
    assert row.mode == "fluency"


def test_clear_override_removes_it() -> None:
    conn = _migrated_connection()
    _seed_source(conn)
    policy_overrides.set_override(
        conn,
        policy_overrides.PolicyOverrideRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            mode="full",
            set_at="2026-08-29T10:00:00+00:00",
        ),
    )

    policy_overrides.clear_override(conn, "s-marcus", "summer_bridge")

    assert policy_overrides.get_override(conn, "s-marcus", "summer_bridge") is None


def test_clear_override_is_a_no_op_with_nothing_set() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    policy_overrides.clear_override(conn, "s-marcus", "summer_bridge")  # must not raise

    assert policy_overrides.get_override(conn, "s-marcus", "summer_bridge") is None


def test_audit_log_records_every_change_in_order() -> None:
    conn = _migrated_connection()
    _seed_source(conn)

    policy_override_audit.insert_audit_row(
        conn,
        policy_override_audit.PolicyOverrideAuditRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            previous_mode=None,
            new_mode="full",
            recorded_at="2026-08-29T10:00:00+00:00",
        ),
    )
    policy_override_audit.insert_audit_row(
        conn,
        policy_override_audit.PolicyOverrideAuditRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            previous_mode="full",
            new_mode=None,
            recorded_at="2026-08-29T11:00:00+00:00",
        ),
    )

    log = policy_override_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [(r.previous_mode, r.new_mode) for r in log] == [(None, "full"), ("full", None)]
