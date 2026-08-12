"""The two-tap capture surface: student picker, capture screen, submit, reject/retake."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import k12ta.web.app as web_app
from k12ta.config import Settings
from k12ta.ingest.schedule import get_or_create_todays_assignment
from k12ta.store import content, db, migrate, students
from k12ta.store import schedule as store_schedule


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


TOO_SMALL = _jpeg_bytes((10, 10), (255, 255, 255))
TOO_DARK = _jpeg_bytes((1200, 1600), (5, 5, 5))
LOOKS_LIKE_TWO_PAGES = _jpeg_bytes((1600, 1200), (200, 200, 200))
ACCEPTED = _jpeg_bytes((1200, 1600), (200, 200, 200))


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    migrate.apply_migrations(c)
    return c


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Ms. Rivera",
        daily_token_budget_usd=Decimal("1.50"),
        log_level="INFO",
    )


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> Iterator[TestClient]:
    web_app.app.dependency_overrides[web_app.get_conn] = lambda: conn
    web_app.app.dependency_overrides[web_app.get_settings] = lambda: settings
    test_client = TestClient(web_app.app)
    yield test_client
    web_app.app.dependency_overrides.clear()


def _seed_two_students(conn: sqlite3.Connection) -> None:
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


def _seed_todays_schedule(conn: sqlite3.Connection, student_id: str) -> None:
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
    store_schedule.set_default_source(
        conn,
        store_schedule.WeeklyDefaultSourceRow(
            student_id=student_id, weekday=date.today().weekday(), source_id="summer_bridge"
        ),
    )


def test_root_lists_both_students_by_name(client: TestClient, conn: sqlite3.Connection) -> None:
    _seed_two_students(conn)

    response = client.get("/")

    assert response.status_code == 200
    assert "Marcus" in response.text
    assert "Priya" in response.text


def test_capture_screen_shows_todays_default_assignment(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")

    response = client.get("/capture/s-marcus")

    assert response.status_code == 200
    assert "Summer bridge workbook" in response.text
    assert "One page" in response.text
    assert "Two pages" in response.text


def test_capture_screen_without_a_scheduled_source_shows_fallback(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_two_students(conn)

    response = client.get("/capture/s-marcus")

    assert response.status_code == 200
    assert "No assignment" in response.text


def test_capture_screen_for_unknown_student_is_404(client: TestClient) -> None:
    response = client.get("/capture/does-not-exist")
    assert response.status_code == 404


def test_post_capture_with_a_good_photo_is_accepted_and_persisted(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    assert response.status_code == 200
    assert "Got it" in response.text
    assert "Ms. Rivera" in response.text

    cur = conn.execute(
        "SELECT capture_id, image_path FROM page_captures WHERE student_id = ?", ("s-marcus",)
    )
    row = cur.fetchone()
    assert row is not None
    assert Path(row["image_path"]).exists()


@pytest.mark.parametrize(
    ("image_bytes", "expected_phrase"),
    [
        (TOO_SMALL, "small"),
        (TOO_DARK, "dark"),
        (LOOKS_LIKE_TWO_PAGES, "two pages"),
    ],
)
def test_post_capture_with_a_bad_photo_is_rejected_and_nothing_is_persisted(
    client: TestClient,
    conn: sqlite3.Connection,
    image_bytes: bytes,
    expected_phrase: str,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", image_bytes, "image/jpeg")},
    )

    assert response.status_code == 200
    assert expected_phrase in response.text.lower()
    assert "Retake" in response.text

    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", ("s-marcus",))
    assert cur.fetchone()[0] == 0
