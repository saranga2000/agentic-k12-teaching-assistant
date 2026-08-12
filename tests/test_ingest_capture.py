"""The M2.2 quality gate and default-assignment resolution, no HTTP involved."""

from __future__ import annotations

import io
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image

from k12ta.config import Settings
from k12ta.ingest import capture, schedule
from k12ta.store import captures, content, db, migrate, students
from k12ta.store import schedule as store_schedule


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


TOO_SMALL = _jpeg_bytes((10, 10), (255, 255, 255))
TOO_DARK = _jpeg_bytes((1200, 1600), (5, 5, 5))
LOOKS_LIKE_TWO_PAGES = _jpeg_bytes((1600, 1200), (200, 200, 200))
ACCEPTED = _jpeg_bytes((1200, 1600), (200, 200, 200))


def test_rejects_an_image_that_is_too_small() -> None:
    verdict = capture.evaluate_image_quality(TOO_SMALL)
    assert verdict.accepted is False
    assert verdict.reason == "too_small"


def test_rejects_an_image_that_is_too_dark() -> None:
    verdict = capture.evaluate_image_quality(TOO_DARK)
    assert verdict.accepted is False
    assert verdict.reason == "too_dark"


def test_rejects_a_landscape_image_as_a_likely_two_page_spread() -> None:
    verdict = capture.evaluate_image_quality(LOOKS_LIKE_TWO_PAGES)
    assert verdict.accepted is False
    assert verdict.reason == "looks_like_two_pages"


def test_accepts_a_large_bright_portrait_image() -> None:
    verdict = capture.evaluate_image_quality(ACCEPTED)
    assert verdict.accepted is True
    assert verdict.reason is None


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_student_with_source(conn: sqlite3.Connection, student_id: str) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
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


def test_resolve_default_source_returns_none_when_nothing_is_scheduled() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)

    assert schedule.resolve_default_source(conn, "s-marcus", on) is None


def test_resolve_default_source_returns_the_scheduled_content_source() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)
    store_schedule.set_default_source(
        conn,
        store_schedule.WeeklyDefaultSourceRow(
            student_id="s-marcus", weekday=on.weekday(), source_id="summer_bridge"
        ),
    )

    resolved = schedule.resolve_default_source(conn, "s-marcus", on)

    assert resolved is not None
    assert resolved.source_id == "summer_bridge"


def test_get_or_create_todays_assignment_is_idempotent_for_the_same_day() -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)

    first = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)
    second = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)

    assert first.assignment_id == second.assignment_id
    cur = conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE student_id = ? AND source_id = ?",
        ("s-marcus", "summer_bridge"),
    )
    assert cur.fetchone()[0] == 1


def test_save_capture_writes_the_image_and_a_page_captures_row(tmp_path: Path) -> None:
    conn = _migrated_connection()
    _seed_student_with_source(conn, "s-marcus")
    on = date(2026, 8, 12)
    assignment = schedule.get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", on)
    settings = Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        log_level="INFO",
    )

    row = capture.save_capture(conn, settings, "s-marcus", assignment.assignment_id, ACCEPTED)

    assert Path(row.image_path).exists()
    assert Path(row.image_path).read_bytes() == ACCEPTED

    fetched = captures.get_page_capture(conn, "s-marcus", row.capture_id)
    assert fetched is not None
    assert fetched.assignment_id == assignment.assignment_id
