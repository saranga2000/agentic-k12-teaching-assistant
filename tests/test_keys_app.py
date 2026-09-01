"""The parent-only answer-key ingestion app: picker, upload+transcribe+confirm,
persist. No test hits the network -- the transcriber is monkeypatched, matching
tests/test_web_capture.py's pattern (get_transcriber is deliberately not a FastAPI
dependency, for the same reason k12ta.web's isn't: the quota gate must run before a
live adapter is ever built).
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import k12ta.keys.app as keys_app
import k12ta.web.app as web_app
from k12ta.config import Settings
from k12ta.evals.fixtures import FixtureProvenance, load_fixture_pages
from k12ta.grading.page_identity import build_composite_key
from k12ta.llm.base import DataRetention
from k12ta.store import (
    answer_key_audit,
    answer_keys,
    capture_duplicates,
    captures,
    content,
    db,
    disputes,
    identity_corrections,
    key_page_images,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    policy_override_audit,
    policy_overrides,
    program_requests,
    quota,
    sessions,
    students,
    verdict_correction_audit,
)
from k12ta.transcribe.base import FailureKind, PageIdentityExtraction, TranscriptionResult
from k12ta.transcribe.key_page import KeyPageEntry, KeyPageResult
from tests.fakes import FakeKeyTranscriber, FakeTranscriber


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


A_KEY_PHOTO = _jpeg_bytes((1200, 1600), (210, 210, 210))


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
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="INFO",
    )


@pytest.fixture
def transcriber() -> FakeKeyTranscriber:
    return FakeKeyTranscriber()


@pytest.fixture
def client(
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[TestClient]:
    keys_app.app.dependency_overrides[keys_app.get_conn] = lambda: conn
    keys_app.app.dependency_overrides[keys_app.get_settings] = lambda: settings
    monkeypatch.setattr(keys_app, "get_transcriber", lambda _settings: transcriber)
    # docs/ROADMAP.md's M5 fixture promotion writes real files -- never into
    # this repo's actual, git-tracked evals/fixtures/ directory during a
    # test run, regardless of what any individual test's image_path is.
    monkeypatch.setattr(keys_app, "get_fixtures_dir", lambda: tmp_path / "fixtures")
    test_client = TestClient(keys_app.app)
    yield test_client
    keys_app.app.dependency_overrides.clear()


def _final_html(response: httpx.Response) -> str:
    """/upload's response is newline-delimited JSON (progress lines, then one
    final line) rather than a single HTML blob -- see test_upload_streams_
    progress_updates_then_the_final_confirm_screen. Most tests only care about
    the eventual rendered page, not the progress lines along the way."""
    lines = [json.loads(line) for line in response.text.strip().split("\n")]
    assert lines[-1]["type"] == "final"
    html: str = lines[-1]["html"]
    return html


def _seed_marcus(conn: sqlite3.Connection) -> None:
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


def test_home_shows_a_badge_when_a_child_has_requested_a_program(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap A (docs/USER_WORKFLOWS.md): the child app's request-program tap
    must be visible here without drilling into anything -- the whole point
    is a parent sees it on the one screen they already open daily."""
    _seed_marcus(conn)

    before = client.get("/")
    assert before.status_code == 200
    assert "asked for a program to be added" not in before.text

    program_requests.request_program(conn, "s-marcus", "2026-08-30T09:00:00+00:00")

    after = client.get("/")
    assert "Marcus asked for a program to be added." in after.text


def test_student_setup_screen_renders_a_blank_form(client: TestClient) -> None:
    response = client.get("/students/new")
    assert response.status_code == 200
    assert 'name="display_name"' in response.text
    assert 'name="grade_level"' in response.text


def test_submit_student_setup_creates_a_child_and_redirects_home(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap E (docs/USER_WORKFLOWS.md): a student only ever came into
    existence via scripts/seed_dev_data.py before this -- now the web app
    can do it, deriving a student_id from the typed name the same way an
    enrollment derives a source_id from its label."""
    response = client.post(
        "/students/new",
        data={"display_name": "Priya", "grade_level": "3"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    created = students.get_student(conn, "priya")
    assert created is not None
    assert created.display_name == "Priya"
    assert created.grade_level == 3


def test_submit_student_setup_disambiguates_a_repeated_name(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Two children sharing a first name must never collide on student_id --
    same reasoning as _unique_source_id for enrollments."""
    client.post("/students/new", data={"display_name": "Priya", "grade_level": "3"})
    client.post("/students/new", data={"display_name": "Priya", "grade_level": "6"})

    assert students.get_student(conn, "priya") is not None
    assert students.get_student(conn, "priya_2") is not None


def test_submit_student_setup_rejects_a_missing_name_or_bad_grade(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    response = client.post("/students/new", data={"display_name": "", "grade_level": "3"})
    assert response.status_code == 200
    assert "Name is required." in response.text

    response = client.post("/students/new", data={"display_name": "Priya", "grade_level": "17"})
    assert response.status_code == 200
    assert "Grade must be a number from 0" in response.text

    assert students.list_students(conn) == []


def test_home_review_queue_lists_every_child_and_program_with_something_pending(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap G (docs/USER_WORKFLOWS.md): a cross-child, cross-program rollup on
    the landing page itself, not only visible after drilling into one
    enrollment -- pure aggregation of sessions.list_pending_for_source."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-review", problem_id="1", cause="needs_person", page_number=15
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "Needs your attention" in response.text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations"' in response.text
    assert "Marcus — Summer bridge workbook" in response.text


def test_home_review_queue_is_absent_when_nothing_is_pending(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/")

    assert response.status_code == 200
    assert "Needs your attention" not in response.text


def test_home_does_not_show_a_stale_request_once_a_source_exists(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A program request only makes sense while there is nothing enrolled
    yet -- once a parent adds a source, the badge must disappear on its own,
    with no separate "clear the request" step needed."""
    _seed_marcus_with_source(conn)
    program_requests.request_program(conn, "s-marcus", "2026-08-30T09:00:00+00:00")

    response = client.get("/")

    assert "asked for a program to be added" not in response.text


def _seed_marcus_with_source(conn: sqlite3.Connection) -> None:
    _seed_marcus(conn)
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


def _success_result() -> KeyPageResult:
    return KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2",
                answer_text=None,
                ungradeable_reason="answers_vary",
                confidence=0.9,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(reason: str, kind: FailureKind) -> KeyPageResult:
    return KeyPageResult(
        entries=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=800,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=reason,
        failure_kind=kind,
    )


def test_picker_lists_students_and_their_enrollments(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/")

    assert response.status_code == 200
    assert "Marcus" in response.text
    assert "Summer bridge workbook" in response.text
    # Scanning a key is not a top-level action any more -- the picker links to the
    # enrollment, not straight to /upload.
    assert 'href="/keys/s-marcus/summer_bridge"' in response.text
    assert 'href="/keys/s-marcus/summer_bridge/upload"' not in response.text


def test_picker_with_no_students_shows_an_intelligible_message(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "No students" in response.text


def test_picker_shows_an_add_enrollment_link_per_student(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/keys/s-marcus/enrollments/new"' in response.text


# --- M3.1: content source ("enrollment") setup flow ------------------------------


def test_enrollment_setup_screen_shows_a_blank_form(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.get("/keys/s-marcus/enrollments/new")

    assert response.status_code == 200
    assert 'name="label"' in response.text
    assert 'name="subject"' in response.text
    assert 'name="has_answer_key"' in response.text
    assert 'name="graded_by_someone_else"' in response.text
    assert 'name="typical_session_minutes"' in response.text
    # Plain-language options, not the internal enum values.
    assert "Workbook" in response.text
    assert "Worksheet packet" in response.text
    assert "Online exercise" in response.text
    # "generated" is the coach's own mechanism, never a parent's setup choice.
    assert "generated" not in response.text.lower()
    # graded_by_someone_else's real consequence, stated plainly, not left implicit.
    assert "diagnostic-only" in response.text.lower()


def test_enrollment_setup_screen_for_unknown_student_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/enrollments/new").status_code == 404


def test_submit_enrollment_setup_creates_the_source_and_redirects_to_describe_its_structure(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "RSM",
            "kind": "worksheet_packet",
            "subject": "math",
            "has_answer_key": "1",
            "graded_by_someone_else": "1",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "45",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/rsm/identity-schema"
    row = content.get_content_source(conn, "s-marcus", "rsm")
    assert row is not None
    assert row.label == "RSM"
    assert row.kind == "worksheet_packet"
    assert row.subject == "math"
    assert row.has_answer_key is True
    assert row.graded_by_someone_else is True
    assert row.default_mode == "diagnostic_only"
    assert row.typical_session_minutes == 45


def test_submit_enrollment_setup_accepts_online_exercise_kind(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "Reading eggs",
            "kind": "online_exercise",
            "subject": "reading",
            "default_mode": "full",
            "typical_session_minutes": "15",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = content.get_content_source(conn, "s-marcus", "reading_eggs")
    assert row is not None
    assert row.kind == "online_exercise"


def test_submit_enrollment_setup_without_the_two_checkboxes_stores_them_false(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """An unchecked HTML checkbox sends nothing at all -- absence must mean
    False, not a missing-field error."""
    _seed_marcus(conn)

    client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "Kumon",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "20",
        },
    )

    row = content.get_content_source(conn, "s-marcus", "kumon")
    assert row is not None
    assert row.has_answer_key is False
    assert row.graded_by_someone_else is False


def test_submit_enrollment_setup_derives_source_id_from_the_label(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "Outside Math Program, Level 3!",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "20",
        },
    )

    assert content.get_content_source(conn, "s-marcus", "outside_math_program_level_3") is not None


def test_submit_enrollment_setup_dedupes_a_colliding_source_id(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)  # already has source_id "summer_bridge"

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "Summer Bridge",
            "kind": "workbook",
            "subject": "reading",
            "default_mode": "full",
            "typical_session_minutes": "30",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge_2/identity-schema"
    row = content.get_content_source(conn, "s-marcus", "summer_bridge_2")
    assert row is not None
    assert row.subject == "reading"


def test_submit_enrollment_setup_with_a_blank_label_rerenders_with_an_error(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "",
            "kind": "workbook",
            "subject": "math",
            "default_mode": "full",
            "typical_session_minutes": "30",
        },
    )

    assert response.status_code == 200
    assert "label" in response.text.lower()
    assert content.list_content_sources(conn, "s-marcus") == []


def test_submit_enrollment_setup_with_non_numeric_minutes_rerenders_with_an_error_and_keeps_input(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "RSM",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "not a number",
        },
    )

    assert response.status_code == 200
    assert 'value="RSM"' in response.text
    assert content.list_content_sources(conn, "s-marcus") == []


def test_submit_enrollment_setup_with_zero_minutes_rerenders_with_an_error(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "RSM",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "0",
        },
    )

    assert response.status_code == 200
    assert content.list_content_sources(conn, "s-marcus") == []


def test_submit_enrollment_setup_with_an_unrecognised_kind_rerenders_with_an_error(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "RSM",
            "kind": "made_up_kind",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "30",
        },
    )

    assert response.status_code == 200
    assert content.list_content_sources(conn, "s-marcus") == []


def test_submit_enrollment_setup_with_an_unrecognised_default_mode_rerenders_with_an_error(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)

    response = client.post(
        "/keys/s-marcus/enrollments/new",
        data={
            "label": "RSM",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "made_up_mode",
            "typical_session_minutes": "30",
        },
    )

    assert response.status_code == 200
    assert content.list_content_sources(conn, "s-marcus") == []


def test_submit_enrollment_setup_for_unknown_student_is_404(client: TestClient) -> None:
    response = client.post(
        "/keys/does-not-exist/enrollments/new",
        data={
            "label": "RSM",
            "kind": "worksheet_packet",
            "subject": "math",
            "default_mode": "diagnostic_only",
            "typical_session_minutes": "30",
        },
    )
    assert response.status_code == 404


def test_enrollment_detail_shows_scan_link_and_says_plainly_what_is_not_built_yet(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Recent sessions is still a real gap (M5); the "waiting on a key" section
    this test used to check for a placeholder on is real now (see the
    pending-item tests) -- checked here only for what's genuinely still
    unbuilt, and with an honest empty state for what is. The landing page
    (parent nav restructure) and the evaluations page it links to split what
    this test used to check on one response into two."""
    _seed_marcus_with_source(conn)

    landing = client.get("/keys/s-marcus/summer_bridge")
    assert landing.status_code == 200
    assert "Summer bridge workbook" in landing.text
    assert 'href="/keys/s-marcus/summer_bridge/upload"' in landing.text

    evaluations = client.get("/keys/s-marcus/summer_bridge/evaluations")
    assert evaluations.status_code == 200
    # No dashboard, no invented metrics -- a plain line for each thing that has no
    # data behind it yet, not an empty panel.
    assert "not shown here yet" in evaluations.text.lower()
    assert "nothing pending right now" in evaluations.text.lower()


def test_enrollment_detail_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge").status_code == 404
    assert client.get("/keys/s-marcus/does-not-exist").status_code == 404


def test_enrollment_detail_links_to_the_identity_schema_editor(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert 'href="/keys/s-marcus/summer_bridge/identity-schema"' in response.text


def _seed_diagnostic_only_source_with_attempts(
    conn: sqlite3.Connection, *, second_answer: str
) -> None:
    """A restricted-mode source with one problem attempted twice (a genuinely
    new second guess) -- the scenario the "Repeated attempts" panel exists for."""
    _seed_marcus(conn)
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
            typical_session_minutes=45,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-rsm",
            source_id="rsm",
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    for capture_id, session_id, captured_at, answer in (
        ("c-1", "sess-1", "2026-08-12T08:00:00+00:00", "18"),
        ("c-2", "sess-2", "2026-08-12T08:10:00+00:00", second_answer),
    ):
        captures.insert_page_capture(
            conn,
            captures.PageCaptureRow(
                student_id="s-marcus",
                capture_id=capture_id,
                assignment_id="a-rsm",
                captured_at=captured_at,
                image_path="/tmp/does-not-matter.jpg",
            ),
        )
        captures.insert_problem(
            conn,
            captures.ProblemRow(
                student_id="s-marcus",
                capture_id=capture_id,
                problem_id="1",
                prompt_text="2x + 5 = 43",
                student_answer_raw=answer,
                transcription_confidence=0.98,
            ),
        )
        sessions.insert_session(
            conn,
            sessions.SessionRow(
                student_id="s-marcus",
                session_id=session_id,
                assignment_id="a-rsm",
                started_at=captured_at,
                ended_at=captured_at,
            ),
        )
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id="s-marcus",
                session_id=session_id,
                capture_id=capture_id,
                problem_id="1",
                outcome="correct" if answer == "19" else "incorrect",
                grader_confidence=0.98,
                expected_answer="19",
                page_number=5,
            ),
        )


def test_enrollment_detail_shows_repeated_attempts_for_a_restricted_mode_source(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_diagnostic_only_source_with_attempts(conn, second_answer="19")

    response = client.get("/keys/s-marcus/rsm/evaluations")

    assert response.status_code == 200
    assert "Repeated attempts" in response.text
    assert "Page 5, problem 1: 2 attempts" in response.text


def test_enrollment_detail_says_no_repeated_attempts_yet_with_only_one_attempt(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus(conn)
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
            typical_session_minutes=45,
        ),
    )

    response = client.get("/keys/s-marcus/rsm/evaluations")

    assert response.status_code == 200
    assert "Repeated attempts" in response.text
    assert "No repeated attempts yet" in response.text


def test_enrollment_detail_omits_repeated_attempts_for_a_full_mode_source(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """FULL mode discloses the answer on attempt one -- a repeat count there
    isn't the signal it is under a restricted mode, so the section is absent
    entirely rather than shown empty."""
    _seed_marcus_with_source(conn)  # default_mode="full", graded_by_someone_else=False

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Repeated attempts" not in response.text


def test_enrollment_detail_never_shows_an_unchanged_resubmission_as_repeated(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_diagnostic_only_source_with_attempts(conn, second_answer="18")  # unchanged

    response = client.get("/keys/s-marcus/rsm/evaluations")

    assert response.status_code == 200
    assert "No repeated attempts yet" in response.text


def test_identity_schema_screen_for_a_source_with_no_schema_shows_blank_rows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert response.status_code == 200
    assert 'name="component_name_0"' in response.text


def test_identity_schema_screen_prefills_the_current_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert response.status_code == 200
    assert 'value="section"' in response.text
    assert 'value="Section"' in response.text
    assert 'value="day"' in response.text


def test_submit_identity_schema_saves_components_and_redirects(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "2",
            "component_name_0": "section",
            "component_label_0": "Section",
            "component_example_0": "Section 1",
            "component_name_1": "day",
            "component_label_1": "Day",
            "component_example_1": "Day 5",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge"
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["section", "day"]


def test_submit_identity_schema_with_no_components_leaves_the_schema_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "3",
            "component_name_0": "",
            "component_name_1": "",
            "component_name_2": "",
        },
    )

    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]


def test_editing_the_schema_bumps_the_version_and_does_not_touch_existing_mappings(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The explicit requirement: a schema change must flag an existing mapping
    for review, never drop it or silently reinterpret it -- see
    k12ta.store.page_identities' staleness rule."""
    _seed_marcus_with_source(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", None)]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=v1,
            confirmed_at="2026-08-14T00:00:00+00:00",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "2",
            "component_name_0": "section",
            "component_label_0": "Section",
            "component_name_1": "day",
            "component_label_1": "Day",
        },
    )

    new_version = page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge")
    assert new_version == 2
    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 1", 1) == 13
    )  # untouched, still there under the old version
    assert (
        page_identities.count_stale_for_source(conn, "s-marcus", "summer_bridge", new_version) == 1
    )


def test_resubmitting_an_unchanged_schema_does_not_bump_the_version(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The standalone editor pre-fills the form with the current schema (see
    `identity_schema_screen`), so a parent opening it and hitting Save without
    changing anything is an ordinary path, not a mistake to design against with a
    confirmation dialog -- but it must not be a schema *change*. Before this test,
    it was: every resubmission called `save_new_schema` unconditionally, which
    stranded every mapping confirmed under the old version even though nothing
    about the schema differed. This is the exact incident that happened to the
    real household database: two byte-identical schema versions, 40 confirmed
    mappings orphaned under the first one."""
    _seed_marcus_with_source(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("day", "Day", "Day 1"), ("section", "Section", "Section 1")],
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1\x1fSection 1",
            schema_version=v1,
            confirmed_at="2026-08-14T00:00:00+00:00",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "2",
            "component_name_0": "day",
            "component_label_0": "Day",
            "component_example_0": "Day 1",
            "component_name_1": "section",
            "component_label_1": "Section",
            "component_example_1": "Section 1",
        },
    )

    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == v1
    assert page_identities.count_stale_for_source(conn, "s-marcus", "summer_bridge", v1) == 0
    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 1\x1fSection 1", v1)
        == 13
    )


def _seed_unconfirmed_bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Gap O (docs/USER_WORKFLOWS.md): the state right after a child confirms
    a brand-new program's app-guessed structure -- one schema version, not
    yet parent-authored, with one confirmed mapping and one resolved
    capture, so a parent's correction has something real to re-check."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("chapter", "chapter", "CH.4")],
        provenance="unconfirmed",
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            composite_key=build_composite_key(["CH.4"]),
            schema_version=1,
            confirmed_at="2026-08-30T08:00:00+00:00",
            source="unconfirmed",
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-30T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-30T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.97,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-30T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-1",
            capture_id="c-1",
            problem_id="1",
            outcome="needs_human",
            grader_confidence=0.97,
            page_number=1,
            needs_human_cause="no_key_for_page",
        ),
    )


def test_identity_schema_screen_shows_a_banner_when_unconfirmed(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_unconfirmed_bootstrap_schema(conn)

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert response.status_code == 200
    assert "Marcus guessed this" in response.text


def test_identity_schema_screen_has_no_banner_once_parent_authored(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert "guessed this" not in response.text


def test_submit_identity_schema_unchanged_confirms_an_unconfirmed_schema_in_place(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap O: re-submitting the pre-filled form exactly as-is is the whole
    "confirm as-is" action -- no new version, and since the capture already
    graded correctly under this schema, nothing needs re-checking."""
    _seed_unconfirmed_bootstrap_schema(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "1",
            "component_name_0": "chapter",
            "component_label_0": "chapter",
            "component_example_0": "CH.4",
        },
    )

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "parent"
    )
    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 1
    assert identity_corrections.get_correction(conn, "s-marcus", "summer_bridge") is None


def test_submit_identity_schema_changed_corrects_an_unconfirmed_schema_and_notifies(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap O: a real correction to a not-yet-confirmed schema is the one
    trigger in this whole app where a regrade fires automatically
    (replay_source) and the child is left a notice -- see
    docs/USER_WORKFLOWS.md §3.5 for why every other regrade stays manual."""
    _seed_unconfirmed_bootstrap_schema(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-30T09:00:00+00:00",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "1",
            "component_name_0": "lesson",
            "component_label_0": "Lesson",
            "component_example_0": "Lesson 4",
        },
    )

    assert (
        page_identity_schemas.get_current_schema_provenance(conn, "s-marcus", "summer_bridge")
        == "parent"
    )
    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 2
    # replay_source ran: the capture, already resolved to page_number=1, is
    # re-decided against the key added above, with zero re-transcription.
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-1")
    assert graded[0].outcome == "correct"
    assert identity_corrections.get_correction(conn, "s-marcus", "summer_bridge") is not None


def test_submit_identity_schema_changed_on_an_already_parent_schema_does_not_auto_regrade(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The general rule, unchanged: an ordinary later edit to an already
    parent-authored schema stays exactly as manual as it always was --
    replay_source only ever fires for the one narrow Gap O trigger above."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-30T08:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            composite_key=build_composite_key(["Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-30T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-30T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.97,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-30T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-1",
            capture_id="c-1",
            problem_id="1",
            outcome="needs_human",
            grader_confidence=0.97,
            page_number=1,
            needs_human_cause="no_key_for_page",
        ),
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-30T09:00:00+00:00",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "1",
            "component_name_0": "lesson",
            "component_label_0": "Lesson",
            "component_example_0": "Lesson 4",
        },
    )

    assert page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge") == 2
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-1")
    assert graded[0].outcome == "needs_human"  # untouched -- no auto-regrade fired
    assert identity_corrections.get_correction(conn, "s-marcus", "summer_bridge") is None


def test_identity_schema_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/identity-schema").status_code == 404
    assert (
        client.post("/keys/does-not-exist/summer_bridge/identity-schema", data={}).status_code
        == 404
    )


def test_manual_mapping_screen_shows_one_field_per_current_schema_component(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    response = client.get("/keys/s-marcus/summer_bridge/identity/manual-entry")

    assert response.status_code == 200
    assert 'name="component_section"' in response.text
    assert 'name="component_day"' in response.text
    # A 2-component schema derives page_number from the composite -- no bare
    # field for a parent to (mis)type one directly.
    assert 'name="page_number"' not in response.text


def test_manual_mapping_screen_for_a_source_with_no_schema_says_so_plainly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Nothing to map against yet -- an honest message, not a blank or broken
    form. Set up the schema first, at /identity-schema."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/identity/manual-entry")

    assert response.status_code == 200
    assert "no identity schema" in response.text.lower()
    assert 'name="component_' not in response.text


def test_submit_manual_mapping_persists_a_composite_marked_manual(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The backfill mechanism: entering a day-to-page mapping you've verified
    against the physical book yourself, no re-scan, no photo -- recorded as
    manual so the eval never mistakes it for a model success. A 2-component
    schema's page_number is a system-assigned surrogate (resolve_or_assign_
    page_number), not the bare "page_number" field a parent might submit --
    a single component's own value (e.g. just "Day 5") is never source-wide
    unique enough to trust directly once a second component exists."""
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"component_section": "Section 1", "component_day": "Day 5"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 5", v)
        == 1
    )
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_submit_manual_mapping_with_a_two_component_schema_reuses_the_same_surrogate_on_reconfirm(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Re-submitting the exact same composite must not mint a second surrogate --
    the whole point of resolve_or_assign_page_number's lookup-first behavior."""
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )
    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"component_section": "Section 1", "component_day": "Day 5"},
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"component_section": "Section 1", "component_day": "Day 5"},
    )

    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 5", v)
        == 1
    )
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 1


def test_submit_manual_mapping_with_a_two_component_schema_assigns_distinct_surrogates(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Two different composites sharing one raw component value (both "Day 5",
    different sections) must land on two different stored page_numbers -- the
    exact collision this whole mechanism exists to prevent."""
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )
    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"component_section": "Section 1", "component_day": "Day 5"},
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"component_section": "Section 2", "component_day": "Day 5"},
    )

    first = page_identities.get_page_number(
        conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 5", v
    )
    second = page_identities.get_page_number(
        conn, "s-marcus", "summer_bridge", "Section 2\x1fDay 5", v
    )
    assert first != second


def test_submit_manual_mapping_with_a_blank_component_persists_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A half-filled composite can never match a future capture's fully-populated
    one -- same rule as the confirm screen's per-row identity fields."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"page_number": "17", "component_section": "", "component_day": "Day 5"},
    )

    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 0


def test_submit_manual_mapping_for_a_source_with_no_schema_is_a_400(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"page_number": "17"},
    )

    assert response.status_code == 400


def test_manual_mapping_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/identity/manual-entry").status_code == 404
    assert (
        client.post("/keys/does-not-exist/summer_bridge/identity/manual-entry", data={}).status_code
        == 404
    )


# --- M3.4: manual answer-key entry -- a parent types a page's answers directly,
# no photograph, no model call. The bridge for a source with no printed key
# (RSM, Kumon) once it also has an identity schema -- see docs/ROADMAP.md's
# M3.4 note on why this alone doesn't help a source with none. -----------------


def test_manual_answers_screen_renders_identity_fields_when_a_schema_exists(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    response = client.get("/keys/s-marcus/summer_bridge/answers/manual-entry")

    assert response.status_code == 200
    assert 'name="component_day"' in response.text
    assert 'name="problem_number_0"' in response.text


def test_manual_answers_screen_hides_the_bare_page_number_field_for_a_two_component_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )

    response = client.get("/keys/s-marcus/summer_bridge/answers/manual-entry")

    assert response.status_code == 200
    assert 'name="component_chapter"' in response.text
    assert 'name="component_page"' in response.text
    assert 'name="page_number"' not in response.text


def test_manual_answers_screen_works_with_no_schema_unlike_identity_only_entry(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The key difference from /identity/manual-entry: a stored answer is not
    useless without a schema, only unreachable from a future photo until one
    exists -- so this screen still renders the answer table, just without any
    identity fields."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/answers/manual-entry")

    assert response.status_code == 200
    assert 'name="component_' not in response.text
    assert 'name="problem_number_0"' in response.text


def test_submit_manual_answers_persists_rows_marked_manual(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "2",
            "page_number": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "problem_number_1": "2",
            "answer_text_1": "",
            "ungradeable_1": "1",
            "ungradeable_reason_1": "answers_vary",
        },
    )

    assert response.status_code == 200
    entries = {
        e.problem_number: e
        for e in answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    }
    assert entries["1"].answer_text == "8 m"
    assert entries["1"].source == "manual"
    assert entries["2"].ungradeable_reason == "answers_vary"
    assert entries["2"].source == "manual"


def test_submit_manual_answers_also_saves_identity_when_schema_and_all_fields_present(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent typing a page's answers from the book already knows that page's
    identity too -- one submission, not a separate trip to /identity/manual-entry."""
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "17",
            "component_day": "Day 5",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5", v) == 17
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_submit_manual_answers_saves_answers_even_with_no_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The gap this task does not close on its own (docs/ROADMAP.md's M3.4 note):
    these answers are stored and correct, just unreachable from a photo until a
    schema exists -- but storing them now means nothing has to be re-typed once
    one does."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].source == "manual"


def test_submit_manual_answers_disagreeing_with_a_stored_value_is_a_conflict(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Never silently overwritten -- same rule as the scanned confirm path,
    reused via the same _save_answer_entry."""
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=17,
            problem_number="1",
            answer_text="8 m",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T08:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "17",
            "problem_number_0": "1",
            "answer_text_0": "9 m",
        },
    )

    assert response.status_code == 200
    assert "conflict" in response.text.lower() or "9 m" in response.text
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "8 m"  # untouched


def test_submit_manual_answers_skips_blank_rows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "3",
            "page_number": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "problem_number_1": "",
            "problem_number_2": "",
        },
    )

    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1


def test_submit_manual_answers_without_a_page_number_is_a_400(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={"row_count": "1", "problem_number_0": "1", "answer_text_0": "8 m"},
    )

    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM answer_key_entries").fetchone()[0] == 0


def test_manual_answers_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/answers/manual-entry").status_code == 404
    assert (
        client.post("/keys/does-not-exist/summer_bridge/answers/manual-entry", data={}).status_code
        == 404
    )


def test_enrollment_detail_with_no_resolutions_says_so_plainly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No invented zeroes dressed up as a dashboard -- a plain line when nothing
    has happened yet, same rule as the other two "not tracked"/"not shown" panels
    on this screen."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "no captures" in response.text.lower()


def test_enrollment_detail_surfaces_page_identity_resolution_counts(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Scope B instrumentation: a parent needs to know which resolution outcome
    dominates in real use, because the fix differs per outcome -- see
    k12ta.store.page_identity_resolutions.count_outcomes_for_source."""
    _seed_marcus_with_source(conn)
    now = "2026-08-13T08:00:00+00:00"
    for i, outcome in enumerate(
        [
            "resolved",
            "resolved",
            "below_floor",
            "no_mapping",
            "no_mapping",
            "no_mapping",
            "conflicting",
            "partial",
            "no_schema",
        ]
    ):
        page_identity_resolutions.insert_resolution(
            conn,
            page_identity_resolutions.PageIdentityResolutionRow(
                student_id="s-marcus",
                source_id="summer_bridge",
                capture_id=f"c-{i}",
                outcome=outcome,
                resolved_page_number=17 if outcome == "resolved" else None,
                created_at=now,
            ),
        )

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "Resolved: 2" in response.text
    assert "Below confidence floor: 1" in response.text
    assert "No mapping yet: 3" in response.text
    assert "Conflicting markers: 1" in response.text
    assert "Partially identified: 1" in response.text
    assert "No identity schema yet: 1" in response.text


def _seed_pending_problem(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
    problem_id: str,
    cause: str | None,
    page_number: int | None,
    prompt_text: str = "12 + 7",
    student_answer_raw: str = "19",
    captured_at: str = "2026-08-13T08:00:00+00:00",
    expected_answer: str | None = None,
) -> None:
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id=capture_id,
            assignment_id="does-not-matter",
            captured_at=captured_at,
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id=capture_id,
            problem_id=problem_id,
            prompt_text=prompt_text,
            student_answer_raw=student_answer_raw,
            transcription_confidence=0.95,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id=f"sess-{capture_id}",
            assignment_id="does-not-matter",
            started_at=captured_at,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id=f"sess-{capture_id}",
            capture_id=capture_id,
            problem_id=problem_id,
            outcome="needs_human",
            grader_confidence=0.95,
            expected_answer=expected_answer,
            page_number=page_number,
            needs_human_cause=cause,
        ),
    )


def test_enrollment_detail_summary_bar_counts_and_links_to_each_section(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-review", problem_id="1", cause="needs_person", page_number=15
    )
    _seed_pending_problem(
        conn, capture_id="c-identity", problem_id="1", cause="unknown_page", page_number=None
    )
    _seed_pending_problem(
        conn, capture_id="c-key", problem_id="1", cause="no_key_for_page", page_number=21
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-graded",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.99,
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="2",
            prompt_text="12 + 8",
            student_answer_raw="21",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="1",
            outcome="correct",
            grader_confidence=0.99,
            page_number=17,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="2",
            outcome="incorrect",
            grader_confidence=0.99,
            page_number=17,
            expected_answer="20",
        ),
    )

    # The summary bar and its jump links live on the landing page (parent nav
    # restructure); the ids they jump to live on the evaluations page it links
    # into -- checked as two responses now, not one.
    evaluations = client.get("/keys/s-marcus/summer_bridge/evaluations")
    assert evaluations.status_code == 200
    eval_text = evaluations.text
    assert 'id="cg-c-review"' in eval_text
    assert 'id="cg-c-identity"' in eval_text
    assert 'id="cg-c-key"' in eval_text
    assert 'id="graded-correct"' in eval_text
    assert 'id="graded-incorrect"' in eval_text

    landing = client.get("/keys/s-marcus/summer_bridge")
    assert landing.status_code == 200
    text = landing.text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations#cg-c-review"' in text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations#cg-c-identity"' in text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations#cg-c-key"' in text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations#graded-correct"' in text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations#graded-incorrect"' in text
    # Each count is real, not just present -- one of each state was seeded.
    assert text.count("needs my review") == 1
    assert "1</strong> needs my review" in text.replace("\n", "").replace("  ", " ")


def test_enrollment_detail_summary_shows_zero_states_as_plain_text_not_dead_links(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert 'href="#graded-correct"' not in response.text
    assert 'href="#graded-incorrect"' not in response.text
    assert "0</strong> graded correct" in response.text.replace("\n", "").replace("  ", " ")


def test_enrollment_detail_lists_graded_correct_and_incorrect_items(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-graded",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="1",
            prompt_text="a correct problem",
            student_answer_raw="19",
            transcription_confidence=0.99,
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="2",
            prompt_text="an incorrect problem",
            student_answer_raw="21",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="1",
            outcome="correct",
            grader_confidence=0.99,
            page_number=17,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="2",
            outcome="incorrect",
            grader_confidence=0.99,
            page_number=17,
            expected_answer="20",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "a correct problem" in response.text
    assert "an incorrect problem" in response.text
    assert "key says &ldquo;20&rdquo;" in response.text


def test_enrollment_detail_lists_graded_partially_correct_items(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """docs/ROADMAP.md's V1 "Verdicts": partially_correct is a real, decisive
    grade (M6's evaluator) -- it must get its own section on the parent's
    evaluations screen, same as correct/incorrect, not be silently dropped
    from list_resolved_for_source's filter."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-graded",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="1",
            prompt_text="explain why the sky is blue",
            student_answer_raw="half of a real explanation",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="1",
            outcome="partially_correct",
            grader_confidence=0.9,
            page_number=17,
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "explain why the sky is blue" in response.text
    assert "Nothing graded partially correct yet." not in response.text


def test_enrollment_detail_groups_pending_items_by_cause(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """needs_person gets its own labelled section, separate from the waiting
    list -- it isn't waiting on more data, it's actionable right now, and a
    parent needs to see it precisely because "the key says answers vary" is
    exactly the case that calls for looking at the work directly."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-no-key", problem_id="1", cause="no_key_for_page", page_number=15
    )
    _seed_pending_problem(
        conn, capture_id="c-unknown", problem_id="1", cause="unknown_page", page_number=None
    )
    _seed_pending_problem(
        conn, capture_id="c-unreadable", problem_id="1", cause="low_confidence", page_number=None
    )
    _seed_pending_problem(
        conn, capture_id="c-person", problem_id="1", cause="needs_person", page_number=21
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Waiting on an answer key" in response.text
    assert "Waiting on page identity" in response.text
    assert "Transcription could not be read" in response.text
    assert "Needs a person to judge" in response.text
    # Each item's actual question is shown, not just a count -- plus two more
    # mentions from the mark-as-duplicate dropdown each of the two unresolved
    # captures (c-unknown, c-unreadable) now offers, listing the other by its
    # own question text since there's no page number to label it with yet.
    assert response.text.count("12 + 7") == 6


def test_enrollment_detail_dedupes_repeated_captures_of_the_same_resolved_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The real page-15 finding (docs/ROADMAP.md's M3.7): three captures of
    the same physical page, each stuck needs_human with a different
    transcription, must not appear as three separate rows -- only the most
    recently captured survives, with a note that earlier attempts exist.
    Nothing is deleted: k12ta.store.sessions.list_pending_for_source still
    returns all three underneath this display collapse."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-first",
        problem_id="10",
        cause="low_confidence",
        page_number=15,
        prompt_text="The clear blue water seemed to beckon to Rafael.",
        student_answer_raw="The clear, blue water seemed to beckon to Rafael.",
        captured_at="2026-08-19T03:14:19+00:00",
    )
    _seed_pending_problem(
        conn,
        capture_id="c-second",
        problem_id="10",
        cause="needs_person",
        page_number=15,
        prompt_text="The clear blue water seemed to beckon to Rafael.",
        student_answer_raw="",
        captured_at="2026-08-19T04:59:49+00:00",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    # Only the most recent capture's row -- the blank answer from c-second,
    # bucketed under "needs a person" -- appears; c-first's full, correctly
    # transcribed answer is folded into the note, not shown as its own row.
    assert response.text.count("The clear") == 1
    assert "1 earlier attempt at this page, not shown" in response.text


def test_enrollment_detail_offers_a_duplicate_picker_for_unresolved_captures(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-first",
        problem_id="1",
        cause="unknown_page",
        page_number=None,
        prompt_text="first unresolved question",
    )
    _seed_pending_problem(
        conn,
        capture_id="c-second",
        problem_id="1",
        cause="unknown_page",
        page_number=None,
        prompt_text="second unresolved question",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert '/keys/s-marcus/summer_bridge/mark-duplicate"' in response.text
    assert 'value="c-second"' in response.text  # c-first's dropdown offers c-second
    assert "second unresolved question" in response.text


def test_enrollment_detail_hides_the_duplicate_picker_with_only_one_unresolved_capture(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-alone", problem_id="1", cause="unknown_page", page_number=None
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "mark-duplicate" not in response.text


def test_submit_mark_duplicate_folds_items_into_the_target_capture(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-target",
        problem_id="1",
        cause="unknown_page",
        page_number=None,
        prompt_text="target question",
    )
    _seed_pending_problem(
        conn,
        capture_id="c-dup",
        problem_id="1",
        cause="unknown_page",
        page_number=None,
        prompt_text="dup question",
    )

    submit_response = client.post(
        "/keys/s-marcus/summer_bridge/mark-duplicate",
        data={"capture_id": "c-dup", "duplicate_of_capture_id": "c-target"},
        follow_redirects=False,
    )
    assert submit_response.status_code == 303

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "target question" in response.text
    assert "dup question" not in response.text
    assert "1 earlier attempt at this page, not shown" in response.text
    # Nothing was deleted or regraded -- the store still has both.
    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    assert {row.capture_id for row in pending} == {"c-target", "c-dup"}


def test_submit_mark_duplicate_follows_a_chain(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """C marked a duplicate of B, B already marked a duplicate of A -- C's
    items belong with A's group, not their own or B's."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-a",
        problem_id="1",
        cause="unknown_page",
        page_number=None,
        prompt_text="root question",
    )
    _seed_pending_problem(
        conn, capture_id="c-b", problem_id="1", cause="unknown_page", page_number=None
    )
    _seed_pending_problem(
        conn, capture_id="c-c", problem_id="1", cause="unknown_page", page_number=None
    )
    client.post(
        "/keys/s-marcus/summer_bridge/mark-duplicate",
        data={"capture_id": "c-b", "duplicate_of_capture_id": "c-a"},
    )

    client.post(
        "/keys/s-marcus/summer_bridge/mark-duplicate",
        data={"capture_id": "c-c", "duplicate_of_capture_id": "c-b"},
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "root question" in response.text
    assert "2 earlier attempts at this page, not shown" in response.text


def test_submit_mark_duplicate_ignores_a_self_reference(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-a", problem_id="1", cause="unknown_page", page_number=None
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/mark-duplicate",
        data={"capture_id": "c-a", "duplicate_of_capture_id": "c-a"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert capture_duplicates.get_duplicate_map(conn, "s-marcus") == {}


def test_submit_mark_duplicate_ignores_an_unknown_capture(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/mark-duplicate",
        data={"capture_id": "no-such-a", "duplicate_of_capture_id": "no-such-b"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert capture_duplicates.get_duplicate_map(conn, "s-marcus") == {}


def test_dedup_tiebreak_prefers_a_real_verdict_over_recency(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Recency alone is not a proxy for quality (2026-08-22): an older capture
    that produced at least one real correct/incorrect verdict anywhere among
    its own items beats a newer capture for the same page that never
    produced one, even though the newer one was taken later."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    # Older capture, page 15: one decisive verdict elsewhere on the same
    # photo (problem 2, correct) alongside its own pending problem 1.
    _seed_pending_problem(
        conn,
        capture_id="c-older-with-verdict",
        problem_id="1",
        cause="low_confidence",
        page_number=15,
        prompt_text="older captures question",
        captured_at="2026-08-19T03:14:19+00:00",
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-older-with-verdict",
            problem_id="2",
            prompt_text="already-graded neighbour",
            student_answer_raw="19",
            transcription_confidence=0.98,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-older-with-verdict",
            capture_id="c-older-with-verdict",
            problem_id="2",
            outcome="correct",
            grader_confidence=0.98,
            page_number=15,
        ),
    )
    # Newer capture, same page 15: no decisive verdict anywhere on it.
    _seed_pending_problem(
        conn,
        capture_id="c-newer-no-verdict",
        problem_id="1",
        cause="needs_person",
        page_number=15,
        prompt_text="newer captures question",
        captured_at="2026-08-20T09:00:00+00:00",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "older captures question" in response.text
    assert "newer captures question" not in response.text
    assert "1 earlier attempt at this page, not shown" in response.text


def test_pending_capture_image_serves_the_real_file(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"not a real jpeg but bytes are bytes for this test")
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-photo",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path=str(image_path),
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/captures/c-photo/image")

    assert response.status_code == 200
    assert response.content == image_path.read_bytes()


def test_pending_capture_image_for_an_unknown_capture_is_404(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/captures/no-such/image")

    assert response.status_code == 404


def test_key_page_image_serves_when_one_is_on_file(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    image_path = tmp_path / "key.jpg"
    image_path.write_bytes(b"a key scan")
    _seed_marcus_with_source(conn)
    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            image_path=str(image_path),
            confirmed_at="2026-08-22T00:00:00+00:00",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/key-image/15")

    assert response.status_code == 200
    assert response.content == image_path.read_bytes()


def test_key_page_image_is_404_when_none_was_ever_saved(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/key-image/15")

    assert response.status_code == 404


def test_enrollment_detail_shows_the_page_entry_ask_for_an_unresolved_capture(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "/keys/s-marcus/summer_bridge/preview-page-entry" in response.text
    assert 'name="capture_id" value="c-unresolved"' in response.text


def test_preview_page_entry_shows_photo_and_key_preview(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/preview-page-entry",
        data={
            "capture_id": "c-unresolved",
            "session_id": "sess-c-unresolved",
            "page_number": "15",
        },
    )

    assert response.status_code == 200
    assert "Is this page 15?" in response.text
    assert "/keys/s-marcus/summer_bridge/captures/c-unresolved/image" in response.text
    assert "Problem 1: 19" in response.text


def test_preview_page_entry_with_no_key_yet_says_so_honestly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/preview-page-entry",
        data={
            "capture_id": "c-unresolved",
            "session_id": "sess-c-unresolved",
            "page_number": "15",
        },
    )

    assert response.status_code == 200
    assert "No answers on file for page 15 yet" in response.text


def test_preview_page_entry_with_a_two_component_schema_looks_up_the_composite(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )
    version = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key=build_composite_key(["CH.4", "4"]),
            schema_version=version,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/preview-page-entry",
        data={
            "capture_id": "c-unresolved",
            "session_id": "sess-c-unresolved",
            "component_chapter": "CH.4",
            "component_page": "4",
        },
    )

    assert response.status_code == 200
    assert "Is this page 15?" in response.text
    assert "Problem 1: 19" in response.text


def test_preview_page_entry_with_a_two_component_schema_refuses_an_unknown_composite(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/preview-page-entry",
        data={
            "capture_id": "c-unresolved",
            "session_id": "sess-c-unresolved",
            "component_chapter": "CH.4",
            "component_page": "4",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 0  # never mints a new mapping, only looks one up


def test_preview_page_entry_rejects_a_non_numeric_page_silently(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/preview-page-entry",
        data={"capture_id": "c-unresolved", "session_id": "sess-x", "page_number": "nope"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_commit_page_entry_grades_and_records_parent_provenance(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Logged as RESOLVED_BY_PARENT_ENTRY -- distinct from both
    RESOLVED_BY_STUDENT_PICK and RESOLVED_BY_STUDENT_ENTRY, so an accuracy
    count can tell who supplied the claim apart."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/commit-page-entry",
        data={
            "capture_id": "c-unresolved",
            "session_id": "sess-c-unresolved",
            "page_number": "15",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-unresolved")
    assert graded[0].outcome == "correct"
    assert graded[0].page_number == 15
    counts = page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge")
    assert counts.get("resolved_by_parent_entry") == 1
    assert counts.get("resolved_by_student_entry") is None
    assert counts.get("resolved_by_student_pick") is None


def test_enrollment_detail_shows_a_trigger_when_a_key_now_covers_a_pending_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-no-key", problem_id="1", cause="no_key_for_page", page_number=15
    )

    response_before = client.get("/keys/s-marcus/summer_bridge/evaluations")
    assert "now gradable" not in response_before.text.lower()

    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response_after = client.get("/keys/s-marcus/summer_bridge/evaluations")
    assert "now gradable" in response_after.text.lower()
    assert 'action="/keys/s-marcus/summer_bridge/regrade-pending"' in response_after.text


def test_submit_regrade_pending_grades_only_what_now_has_a_key(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-now-keyed", problem_id="1", cause="no_key_for_page", page_number=15
    )
    _seed_pending_problem(
        conn, capture_id="c-still-pending", problem_id="1", cause="no_key_for_page", page_number=71
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post("/keys/s-marcus/summer_bridge/regrade-pending", follow_redirects=False)

    assert response.status_code == 303
    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    remaining_captures = {row.capture_id for row in pending}
    assert remaining_captures == {"c-still-pending"}
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-now-keyed")
    assert graded[0].outcome == "correct"
    assert graded[0].page_number == 15


def test_enrollment_detail_shows_answer_differs_side_by_side_with_a_verdict_form(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-differs",
        problem_id="1",
        cause="answer_differs_from_key",
        page_number=15,
        prompt_text="shape?",
        student_answer_raw="rhombus",
        expected_answer="quadrilateral",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Answer differs from the key" in response.text
    assert "rhombus" in response.text
    assert "quadrilateral" in response.text
    assert 'action="/keys/s-marcus/summer_bridge/answer-verdict"' in response.text
    assert 'value="c-differs"' in response.text


def test_evaluations_screen_shows_one_ask_field_per_component_for_a_two_component_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-unresolved", problem_id="1", cause="unknown_page", page_number=None
    )
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'name="component_chapter"' in response.text
    assert 'name="component_page"' in response.text
    assert 'name="page_number"' not in response.text


def test_evaluations_screen_shows_the_real_question_number_when_known(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-known", problem_id="4", cause="needs_person", page_number=15
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Q4" in response.text


def test_evaluations_screen_offers_a_verdict_form_for_needs_person_rows_too(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The 'needs a person to judge' bucket used to only name the problem, with
    no way to act on it -- a parent reading the child's own written answer is
    exactly who can settle it, the same one-tap verdict already offered for
    answer_differs_from_key."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-needs-person",
        problem_id="4",
        cause="needs_person",
        page_number=15,
        prompt_text="explain your reasoning",
        student_answer_raw="because it has four equal sides",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/answer-verdict"' in response.text
    assert 'value="c-needs-person"' in response.text
    # No key answer exists for this cause -- the "key says" clause must not
    # render a literal "None".
    assert "key says" not in response.text


def test_evaluations_screen_offers_a_verdict_form_for_low_confidence_rows_too(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Parent feedback (2026-08-30): "Transcription could not be read" used
    to have no way to act on it at all, even though the model's own
    tentative reading was shown right there -- confusing and a dead end."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-low-conf",
        problem_id="2",
        cause="low_confidence",
        page_number=15,
        prompt_text="12 + 7",
        student_answer_raw="l9",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/answer-verdict"' in response.text
    assert 'value="c-low-conf"' in response.text
    assert 'name="student_answer_raw" value="l9"' in response.text
    assert "wasn't confident reading this one" in response.text


def test_submit_answer_verdict_corrects_a_misread_answer_before_judging_it(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-low-conf",
        problem_id="2",
        cause="low_confidence",
        page_number=15,
        prompt_text="12 + 7",
        student_answer_raw="l9",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-low-conf",
            "capture_id": "c-low-conf",
            "problem_id": "2",
            "verdict": "correct",
            "student_answer_raw": "19",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-low-conf")
    assert graded[0].outcome == "correct"
    problems = captures.list_problems_for_capture(conn, "s-marcus", "c-low-conf")
    assert problems[0].student_answer_raw == "19"


def test_submit_answer_verdict_with_a_blank_correction_leaves_the_transcription_alone(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-low-conf",
        problem_id="2",
        cause="low_confidence",
        page_number=15,
        prompt_text="12 + 7",
        student_answer_raw="19",
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-low-conf",
            "capture_id": "c-low-conf",
            "problem_id": "2",
            "verdict": "correct",
            "student_answer_raw": "   ",
        },
    )

    problems = captures.list_problems_for_capture(conn, "s-marcus", "c-low-conf")
    assert problems[0].student_answer_raw == "19"  # unchanged


def test_evaluations_screen_offers_mark_correct_and_save_as_key_for_no_key_rows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Parent feedback (2026-08-30): a question with no key at all had no way
    to act on it -- a parent reading the child's own answer, confirming it's
    right, and teaching it as the key for next time is exactly the missing
    action."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-no-key",
        problem_id="10",
        cause="no_key_for_page",
        page_number=14,
        prompt_text="bird : nest :: rabbit : ___",
        student_answer_raw="burrow",
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'name="key_answer_text" value="burrow"' in response.text
    assert 'name="page_number" value="14"' in response.text
    assert "save as key" in response.text.lower()


def test_submit_answer_verdict_saves_a_new_key_entry_and_judges_it(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-no-key",
        problem_id="10",
        cause="no_key_for_page",
        page_number=14,
        prompt_text="bird : nest :: rabbit : ___",
        student_answer_raw="burrow",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-no-key",
            "capture_id": "c-no-key",
            "problem_id": "10",
            "verdict": "correct",
            "key_answer_text": "burrow",
            "page_number": "14",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-no-key")
    assert graded[0].outcome == "correct"
    entry = answer_keys.get_entry(conn, "s-marcus", "summer_bridge", 14, "10")
    assert entry is not None
    assert entry.answer_text == "burrow"
    assert entry.source == "manual"


def test_submit_answer_verdict_saves_a_key_even_when_marking_incorrect(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent may know the real answer and mark THIS instance wrong while
    still teaching the correct key for future pages."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-no-key",
        problem_id="10",
        cause="no_key_for_page",
        page_number=14,
        prompt_text="bird : nest :: rabbit : ___",
        student_answer_raw="field",
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-no-key",
            "capture_id": "c-no-key",
            "problem_id": "10",
            "verdict": "incorrect",
            "key_answer_text": "burrow",
            "page_number": "14",
        },
    )

    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-no-key")
    assert graded[0].outcome == "incorrect"
    entry = answer_keys.get_entry(conn, "s-marcus", "summer_bridge", 14, "10")
    assert entry is not None
    assert entry.answer_text == "burrow"


def test_submit_answer_verdict_without_a_key_answer_behaves_as_before(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No key_answer_text/page_number supplied -- the plain verdict path for
    answer_differs_from_key/needs_person/low_confidence must be unaffected."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-differs",
        problem_id="1",
        cause="answer_differs_from_key",
        page_number=15,
        expected_answer="quadrilateral",
        student_answer_raw="rhombus",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-differs",
            "capture_id": "c-differs",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-differs")
    assert graded[0].outcome == "correct"
    # No key row was ever created for page 15's problem 1 by this path.
    assert answer_keys.get_entry(conn, "s-marcus", "summer_bridge", 15, "1") is None


def test_submit_answer_verdict_with_a_conflicting_key_holds_back_and_does_not_judge(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-no-key",
        problem_id="10",
        cause="no_key_for_page",
        page_number=14,
        prompt_text="bird : nest :: rabbit : ___",
        student_answer_raw="burrow",
    )
    # A key already exists for this exact page/problem by the time the parent
    # submits -- genuinely rare for NO_KEY_FOR_PAGE, but must still be held
    # back like any other conflicting write in this app, not silently lost.
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=14,
            problem_number="10",
            answer_text="den",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-no-key",
            "capture_id": "c-no-key",
            "problem_id": "10",
            "verdict": "correct",
            "key_answer_text": "burrow",
            "page_number": "14",
        },
    )

    assert response.status_code == 200
    assert "These don't match" in response.text
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-no-key")
    assert graded[0].outcome == "needs_human"  # untouched -- not judged yet


def test_evaluations_screen_offers_a_reassign_page_control(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Parent feedback (2026-08-30): a capture that resolved to the wrong
    page (a real bug found in the household's own data -- the same physical
    page photographed twice landed on two different page numbers) needs a
    direct way to say "this is actually page N.\""""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-1", problem_id="1", cause="no_key_for_page", page_number=19
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/reassign-page"' in response.text
    assert "currently page 19" in response.text
    assert 'value="c-1"' in response.text


def test_submit_reassign_page_regrades_against_the_new_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-1",
        problem_id="1",
        cause="no_key_for_page",
        page_number=19,
        student_answer_raw="19",
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=17,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/reassign-page",
        data={"capture_id": "c-1", "session_id": "sess-c-1", "page_number": "17"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-1")
    assert graded[0].page_number == 17
    assert graded[0].outcome == "correct"  # regraded from the already-stored transcription


def test_submit_reassign_page_rejects_a_non_positive_page_number(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-1", problem_id="1", cause="no_key_for_page", page_number=19
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/reassign-page",
        data={"capture_id": "c-1", "session_id": "sess-c-1", "page_number": "0"},
    )

    assert response.status_code == 400
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-1")
    assert graded[0].page_number == 19  # untouched


def test_evaluations_screen_offers_an_inline_fix_for_an_ambiguous_problem_id(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-ambiguous",
        problem_id="_ambiguous_0",
        cause="ambiguous_problem_id",
        page_number=15,
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Question number not identified" in response.text
    assert 'action="/keys/s-marcus/summer_bridge/set-problem-number"' in response.text
    assert 'value="_ambiguous_0"' in response.text

    # Counted under "needs my review" on the landing page's summary -- it
    # needs a parent to supply a value, exactly like the other causes in
    # that bucket.
    landing = client.get("/keys/s-marcus/summer_bridge")
    assert "1</strong> needs my review" in landing.text.replace("\n", "").replace("  ", " ")


def test_submit_problem_number_relabels_and_regrades_against_the_key(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-ambiguous",
        problem_id="_ambiguous_0",
        cause="ambiguous_problem_id",
        page_number=15,
        student_answer_raw="19",
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="4",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/set-problem-number",
        data={
            "capture_id": "c-ambiguous",
            "session_id": "sess-c-ambiguous",
            "old_problem_id": "_ambiguous_0",
            "problem_id": "4",
            "page_number": "15",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge/evaluations"

    resolved = sessions.list_resolved_for_source(conn, "s-marcus", "summer_bridge")
    assert [(r.problem_id, r.outcome) for r in resolved] == [("4", "correct")]
    assert sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge") == []


def test_submit_problem_number_ignores_a_collision_with_a_real_problem(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-ambiguous",
        problem_id="_ambiguous_0",
        cause="ambiguous_problem_id",
        page_number=15,
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-ambiguous",
            problem_id="4",
            prompt_text="5 + 5",
            student_answer_raw="10",
            transcription_confidence=0.9,
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/set-problem-number",
        data={
            "capture_id": "c-ambiguous",
            "session_id": "sess-c-ambiguous",
            "old_problem_id": "_ambiguous_0",
            "problem_id": "4",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    assert {row.problem_id for row in pending} == {"_ambiguous_0"}


def test_enrollment_landing_links_to_the_three_enrollment_screens_without_showing_pending_items(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-review", problem_id="1", cause="needs_person", page_number=15
    )

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert 'href="/keys/s-marcus/summer_bridge/upload"' in response.text
    assert 'href="/keys/s-marcus/summer_bridge/answers/manual-entry"' in response.text
    assert 'href="/keys/s-marcus/summer_bridge/answer-keys"' in response.text
    assert 'href="/keys/s-marcus/summer_bridge/evaluations"' in response.text
    # Not the pending item itself -- only the count and a link into evaluations.
    # ("cause-label" alone would false-pass: base.html's shared stylesheet
    # defines that CSS class on every page regardless of content.)
    assert "Needs a person to judge" not in response.text
    assert "12 + 7" not in response.text


def test_answer_keys_screen_lists_entries_grouped_by_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="4",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/answer-keys")

    assert response.status_code == 200
    assert "Page 15" in response.text
    assert "Q4" in response.text
    assert "19" in response.text


def test_answer_keys_screen_shows_the_key_photo_when_one_is_on_file(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Parent feedback (2026-08-30): this screen used to be text-only, with
    no way to check the listed answers against the actual scanned page."""
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="4",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )
    image_path = tmp_path / "key.jpg"
    image_path.write_bytes(b"a key scan")
    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            image_path=str(image_path),
            confirmed_at="2026-08-22T00:00:00+00:00",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/answer-keys")

    assert response.status_code == 200
    assert 'src="/keys/s-marcus/summer_bridge/key-image/15"' in response.text
    assert "data-lightbox" in response.text


def test_answer_keys_screen_shows_no_photo_when_none_is_on_file(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="4",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/answer-keys")

    assert "key-image" not in response.text


def test_answer_keys_screen_with_nothing_on_file_says_so_plainly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/answer-keys")

    assert response.status_code == 200
    assert "no answer keys on file" in response.text.lower()


def test_submit_answer_verdict_records_correct_and_clears_the_cause(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-differs",
        problem_id="1",
        cause="answer_differs_from_key",
        page_number=15,
        prompt_text="shape?",
        student_answer_raw="rhombus",
        expected_answer="quadrilateral",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-differs",
            "capture_id": "c-differs",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-differs")
    assert graded[0].outcome == "correct"
    assert graded[0].needs_human_cause is None
    assert graded[0].expected_answer == "quadrilateral"  # untouched, still on the row


def test_submit_answer_verdict_records_an_audit_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """docs/ROADMAP.md's M5: a correction has a name, a timestamp, and a
    before/after value on file -- not just a changed row in graded_problems."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-differs",
        problem_id="1",
        cause="answer_differs_from_key",
        page_number=15,
        prompt_text="shape?",
        student_answer_raw="rhombus",
        expected_answer="quadrilateral",
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-differs",
            "capture_id": "c-differs",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    rows = verdict_correction_audit.list_for_problem(
        conn, "s-marcus", "sess-c-differs", "c-differs", "1"
    )
    assert len(rows) == 1
    assert rows[0].previous_outcome == "needs_human"
    assert rows[0].previous_needs_human_cause == "answer_differs_from_key"
    assert rows[0].new_outcome == "correct"
    assert rows[0].previous_student_answer_raw == "rhombus"
    assert rows[0].new_student_answer_raw == "rhombus"
    assert rows[0].source == verdict_correction_audit.VerdictCorrectionSource.NEEDS_HUMAN_RESOLUTION


def test_submit_answer_verdict_records_the_corrected_transcription_in_the_audit_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-needs-person", problem_id="4", cause="needs_person", page_number=15
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-needs-person",
            "capture_id": "c-needs-person",
            "problem_id": "4",
            "verdict": "correct",
            "student_answer_raw": "19 (fixed misread)",
        },
        follow_redirects=False,
    )

    rows = verdict_correction_audit.list_for_problem(
        conn, "s-marcus", "sess-c-needs-person", "c-needs-person", "4"
    )
    assert rows[0].previous_student_answer_raw == "19"
    assert rows[0].new_student_answer_raw == "19 (fixed misread)"


def test_submit_answer_verdict_promotes_a_fixture_when_a_correct_answer_is_known(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """docs/ROADMAP.md's M5 fixture promotion, exercised end to end through
    the real endpoint -- not just the pure k12ta.evals.fixtures functions
    tests/test_fixture_promotion.py already covers in isolation."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    image_path = tmp_path / "capture.jpg"
    image_path.write_bytes(b"a real file, not the usual placeholder path")
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-differs",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path=str(image_path),
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-differs",
            problem_id="1",
            prompt_text="shape?",
            student_answer_raw="rhombus",
            transcription_confidence=0.95,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-differs",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-differs",
            capture_id="c-differs",
            problem_id="1",
            outcome="needs_human",
            grader_confidence=0.0,
            expected_answer="quadrilateral",
            page_number=15,
            needs_human_cause="answer_differs_from_key",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-differs",
            "capture_id": "c-differs",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    fixtures_dir = tmp_path / "fixtures"
    pages = load_fixture_pages(fixtures_dir)
    assert len(pages) == 1
    page = pages[0]
    assert page.provenance == FixtureProvenance.PARENT_CORRECTION
    assert page.source_id == "summer_bridge"
    assert page.subject == "math"
    assert (fixtures_dir / page.image).read_bytes() == image_path.read_bytes()
    assert page.items[0].correct_answer == "quadrilateral"


def test_submit_verdict_correction_promotes_a_fixture_using_the_students_own_answer(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    image_path = tmp_path / "capture.jpg"
    image_path.write_bytes(b"another real file")
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-incorrect",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path=str(image_path),
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-incorrect",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.95,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-incorrect",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-incorrect",
            capture_id="c-incorrect",
            problem_id="1",
            outcome="incorrect",
            grader_confidence=0.95,
            page_number=15,
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    pages = load_fixture_pages(tmp_path / "fixtures")
    assert len(pages) == 1
    assert pages[0].items[0].correct_answer == "19"


def test_submit_answer_verdict_works_on_a_needs_person_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """apply_human_verdict is cause-agnostic already -- this locks in that a
    needs_person row (no key answer to compare against, unlike
    answer_differs_from_key) is a real, working target for it, not just
    answer_differs_from_key."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-needs-person",
        problem_id="4",
        cause="needs_person",
        page_number=15,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-needs-person",
            "capture_id": "c-needs-person",
            "problem_id": "4",
            "verdict": "incorrect",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-needs-person")
    assert graded[0].outcome == "incorrect"
    assert graded[0].needs_human_cause is None


def test_submit_answer_verdict_accepts_partially_correct(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent reviewing a flagged item must be able to say "partially
    right," not just correct/incorrect -- docs/ROADMAP.md's V1 "Verdicts"."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-needs-person",
        problem_id="4",
        cause="needs_person",
        page_number=15,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-needs-person",
            "capture_id": "c-needs-person",
            "problem_id": "4",
            "verdict": "partially_correct",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-needs-person")
    assert graded[0].outcome == "partially_correct"
    assert graded[0].needs_human_cause is None


def test_submit_answer_verdict_rejects_a_value_that_is_not_a_verdict(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn,
        capture_id="c-differs",
        problem_id="1",
        cause="answer_differs_from_key",
        page_number=15,
        expected_answer="quadrilateral",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answer-verdict",
        data={
            "session_id": "sess-c-differs",
            "capture_id": "c-differs",
            "problem_id": "1",
            "verdict": "maybe",
        },
    )

    assert response.status_code == 400
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-differs")
    assert graded[0].outcome == "needs_human"  # untouched


def _seed_decisive_incorrect_problem(
    conn: sqlite3.Connection,
    *,
    capture_id: str = "c-incorrect",
    problem_id: str = "1",
    page_number: int | None = 15,
) -> None:
    """Gap B/K/L (docs/USER_WORKFLOWS.md): a row the grader already decided,
    unlike _seed_pending_problem's needs_human rows -- what a dispute
    actually contests."""
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id=capture_id,
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id=capture_id,
            problem_id=problem_id,
            prompt_text="12 + 7",
            student_answer_raw="18",
            transcription_confidence=0.95,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id=f"sess-{capture_id}",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id=f"sess-{capture_id}",
            capture_id=capture_id,
            problem_id=problem_id,
            outcome="incorrect",
            grader_confidence=0.95,
            expected_answer="19",
            page_number=page_number,
        ),
    )


def test_evaluations_screen_offers_a_correction_control_on_a_graded_incorrect_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/correct-verdict"' in response.text
    # The row is already incorrect -- its own button should not be offered again.
    incorrect_section = response.text.split('id="graded-incorrect"')[1]
    assert "Actually incorrect" not in incorrect_section.split("</form>")[0]
    assert "Actually correct" in incorrect_section


def test_evaluations_screen_shows_open_disputes_above_pending_review(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gap K (docs/USER_WORKFLOWS.md): child-escalated items are their own
    section, prioritized above the app-requested queue -- checked by
    position in the raw HTML, not just presence."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I carried the 1 correctly",
        disputed_at="2026-08-13T09:00:00+00:00",
    )
    _seed_pending_problem(
        conn, capture_id="c-review", problem_id="1", cause="needs_person", page_number=21
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Marcus disputed these" in response.text
    assert "I carried the 1 correctly" in response.text
    dispute_pos = response.text.index("Marcus disputed these")
    pending_pos = response.text.index("<h2>Pending review</h2>")
    assert dispute_pos < pending_pos


def test_evaluations_screen_has_no_dispute_section_when_nothing_is_disputed(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert "disputed these" not in response.text


def test_submit_dispute_resolution_upheld_leaves_the_grade_incorrect(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve-dispute",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "resolution": "upheld",
            "comment": "The key really does say 19 here.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-incorrect")
    assert graded[0].outcome == "incorrect"  # untouched
    row = disputes.get(conn, "s-marcus", "sess-c-incorrect", "c-incorrect", "1")
    assert row is not None
    assert row.resolution == "upheld"
    assert row.resolution_comment == "The key really does say 19 here."
    assert disputes.list_open_for_source(conn, "s-marcus", "summer_bridge") == []


def test_submit_dispute_resolution_overturned_flips_the_grade_to_correct(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve-dispute",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "resolution": "overturned",
            "comment": "You're right, good catch!",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-incorrect")
    assert graded[0].outcome == "correct"
    rows = verdict_correction_audit.list_for_problem(
        conn, "s-marcus", "sess-c-incorrect", "c-incorrect", "1"
    )
    assert len(rows) == 1
    assert rows[0].previous_outcome == "incorrect"
    assert rows[0].new_outcome == "correct"
    assert rows[0].source == verdict_correction_audit.VerdictCorrectionSource.DISPUTE_OVERTURNED


def test_submit_dispute_resolution_upheld_records_no_audit_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Upholding an incorrect verdict changes nothing about the grade -- no
    correction happened, so nothing belongs in the audit trail."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    client.post(
        "/keys/s-marcus/summer_bridge/resolve-dispute",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "resolution": "upheld",
            "comment": "The key really does say 19 here.",
        },
        follow_redirects=False,
    )

    assert (
        verdict_correction_audit.list_for_problem(
            conn, "s-marcus", "sess-c-incorrect", "c-incorrect", "1"
        )
        == []
    )


def test_submit_verdict_correction_flips_an_already_decided_verdict(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-incorrect")
    assert graded[0].outcome == "correct"
    rows = verdict_correction_audit.list_for_problem(
        conn, "s-marcus", "sess-c-incorrect", "c-incorrect", "1"
    )
    assert len(rows) == 1
    assert rows[0].previous_outcome == "incorrect"
    assert rows[0].new_outcome == "correct"
    assert (
        rows[0].source
        == verdict_correction_audit.VerdictCorrectionSource.DECIDED_VERDICT_CORRECTION
    )


def test_submit_verdict_correction_is_404_for_a_row_never_graded(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "no-such-session",
            "capture_id": "no-such-capture",
            "problem_id": "1",
            "verdict": "correct",
        },
    )

    assert response.status_code == 404


def test_submit_verdict_correction_is_refused_while_a_dispute_is_open(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "verdict": "correct",
        },
    )

    assert response.status_code == 409
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-incorrect")
    assert graded[0].outcome == "incorrect"  # untouched


def test_submit_verdict_correction_is_allowed_once_a_dispute_is_resolved(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent noticing a different mistake later, after a dispute on the
    same row already ran its course, must still be able to fix it."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )
    disputes.resolve(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        resolution="upheld",
        resolution_comment="The key really does say 19 here.",
        resolved_at="2026-08-13T10:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "verdict": "correct",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", "sess-c-incorrect")
    assert graded[0].outcome == "correct"


def test_submit_verdict_correction_rejects_a_value_that_is_not_a_verdict(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/correct-verdict",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "verdict": "maybe",
        },
    )

    assert response.status_code == 400


def test_submit_dispute_resolution_rejects_a_blank_comment(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve-dispute",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "resolution": "upheld",
            "comment": "   ",
        },
    )

    assert response.status_code == 400
    row = disputes.get(conn, "s-marcus", "sess-c-incorrect", "c-incorrect", "1")
    assert row is not None
    assert row.resolved_at is None  # untouched


def test_submit_dispute_resolution_rejects_an_invalid_resolution_value(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_decisive_incorrect_problem(conn)
    disputes.file_dispute(
        conn,
        student_id="s-marcus",
        session_id="sess-c-incorrect",
        capture_id="c-incorrect",
        problem_id="1",
        reason="I think I'm right",
        disputed_at="2026-08-13T09:00:00+00:00",
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve-dispute",
        data={
            "session_id": "sess-c-incorrect",
            "capture_id": "c-incorrect",
            "problem_id": "1",
            "resolution": "maybe",
            "comment": "a comment",
        },
    )

    assert response.status_code == 400


def test_enrollment_detail_surfaces_stale_mapping_count_after_a_schema_change(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", None)]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=v1,
            confirmed_at="2026-08-14T00:00:00+00:00",
        ),
    )
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "1 confirmed page mapping" in response.text
    assert "won't resolve" in response.text
    assert 'class="message attention"' in response.text


def test_upload_screen_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/upload").status_code == 404
    assert client.get("/keys/s-marcus/does-not-exist/upload").status_code == 404


def test_successful_upload_renders_confirm_form_with_photo_and_entries(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    assert "data:image/jpeg;base64," in html
    assert 'value="17"' in html  # page_number
    assert 'value="8 m"' in html  # answer_text
    # The ungradeable entry is pre-checked, not pre-filled with an answer.
    assert html.count('name="ungradeable_1"') == 1
    assert "checked" in html.split('name="ungradeable_1"')[1].split(">")[0]


def test_confirm_screen_hides_the_bare_page_number_field_for_a_two_component_schema(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )
    transcriber.result = _success_result()

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="page_number_0"' not in html
    assert 'name="identity_chapter_0"' in html
    assert 'name="identity_page_0"' in html


def test_confirm_screen_sorts_entries_by_page_then_problem_number(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """The model doesn't promise to emit entries in page/problem order -- multiple
    "Day N/Page NN" blocks on one photo, or a leading orphan block inferred back to
    the previous day (see prompts/transcribe_key_page.md), can easily arrive out of
    order. A parent checking answers against a printed key wants them in the same
    order the key prints them, ascending, not whatever order the model happened to
    emit."""
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=19,
                problem_number="1",
                answer_text="x",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="10",
                answer_text="y",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2",
                answer_text="z",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2a",
                answer_text="w",
                ungradeable_reason=None,
                confidence=0.9,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    # Ascending by page, then a natural (numeric-aware) ordering within a page --
    # problem "10" sorts after "2", not before it lexicographically, and "2a"
    # sorts right after "2", not off in string-order somewhere else.
    answers_in_order = [
        html.split(f'name="answer_text_{i}"')[1].split('value="')[1].split('"')[0] for i in range(4)
    ]
    assert answers_in_order == ["z", "w", "y", "x"]


def test_upload_streams_progress_updates_then_the_final_confirm_screen(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """The whole point: a parent watching a static spinner for a call that can run
    minutes has no way to tell "still working" from "stuck" (docs/ROADMAP.md's M2
    note, and the actual incident that motivated it). The response is newline-
    delimited JSON, not a single HTML blob, precisely so the browser can show
    something live instead of waiting for the whole thing."""
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()
    transcriber.progress_updates = (120, 890, 2400)

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.strip().split("\n")]

    progress_lines = [line for line in lines if line["type"] == "progress"]
    assert [line["chars"] for line in progress_lines] == [120, 890, 2400]

    assert lines[-1]["type"] == "final"
    final_html = lines[-1]["html"]
    assert "data:image/jpeg;base64," in final_html
    assert 'value="17"' in final_html


def test_confirm_persists_exactly_the_submitted_values_not_the_original_transcription(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Proves edits are honored: the parent's correction is what gets stored, not
    whatever the model originally said."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 meters",  # parent corrected the model's "8 m"
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text == "8 meters"
    assert entries[0].source == "manual"


def test_confirm_records_model_source_when_the_answer_is_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent who saves a scanned answer as-is is a model success, not a
    manual entry -- the whole reason `answer_text_original_i` exists."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "answer_text_original_0": "8 m",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].source == "model"


def test_confirm_records_manual_source_when_the_ungradeable_reason_is_corrected(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Flipping the ungradeable reason on screen is a correction just like
    editing the answer text -- the model's original guess said one thing,
    the parent said another."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "ungradeable_0": "1",
            "ungradeable_reason_0": "graph_or_table",
            "ungradeable_reason_original_0": "answers_vary",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].source == "manual"
    assert entries[0].ungradeable_reason == "graph_or_table"


def test_confirm_persists_the_scanned_image_for_every_page_it_covers(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """Persisted going forward (2026-08-22): the upload writes the photo to
    disk, confirm.html threads its path through as a hidden field, and
    submit_confirm links it to every page_number this scan actually saved
    an answer for -- so the parent scan display has a real key image to
    show, not a permanently-empty slot."""
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()

    upload_response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )
    html = _final_html(upload_response)
    image_path = html.split('name="image_path" value="')[1].split('"')[0]
    assert image_path  # a real path was threaded through, not left blank

    confirm_response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "2",
            "image_path": image_path,
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "page_number_1": "17",
            "problem_number_1": "2",
            "answer_text_1": "12 cm",
        },
    )

    assert confirm_response.status_code == 200
    assert key_page_images.get_image_path(conn, "s-marcus", "summer_bridge", 17) == image_path


def test_confirm_screen_shows_a_discovery_panel_when_the_scan_found_markers_and_no_schema_exists(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="schema_name_0"' in html
    assert 'value="day"' in html
    assert 'name="identity_0_0"' in html
    assert 'value="Day 5"' in html


def test_upload_merges_identity_markers_found_on_an_optional_example_page(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gap I (docs/USER_WORKFLOWS.md): a parent's optional second photo of a
    plain exercise page can surface a marker the isolated key page never
    showed -- exactly the real RSM gap (some answer-key editions print no
    chapter/lesson banner at all). The key page's own finding ("day") still
    comes first; the example page's finding ("chapter") fills a real gap,
    not a name already covered."""
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )
    page_transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=200,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"chapter": ("CH.4",)}, confidence=0.9),
        )
    )
    monkeypatch.setattr(keys_app, "get_page_transcriber", lambda _settings: page_transcriber)

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={
            "photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg"),
            "example_page": ("example.jpg", A_KEY_PHOTO, "image/jpeg"),
        },
    )

    html = _final_html(response)
    assert 'name="schema_name_0"' in html
    assert 'value="day"' in html
    assert 'name="schema_name_1"' in html
    assert 'value="chapter"' in html
    assert 'name="identity_1_0"' in html
    assert 'value="CH.4"' in html
    assert len(page_transcriber.calls) == 1
    # Both photos are real quota-spending calls.
    assert quota.get_count(conn, date.today()) == 2


def test_upload_does_not_call_the_page_transcriber_when_a_schema_already_exists(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a schema exists, discovery never runs at all -- an example page's
    markers have nothing left to add, and calling the page transcriber
    anyway would spend quota for no reason."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )
    page_transcriber = FakeTranscriber()
    monkeypatch.setattr(keys_app, "get_page_transcriber", lambda _settings: page_transcriber)

    client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={
            "photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg"),
            "example_page": ("example.jpg", A_KEY_PHOTO, "image/jpeg"),
        },
    )

    assert page_transcriber.calls == []
    assert quota.get_count(conn, date.today()) == 1


def test_upload_screen_offers_the_example_page_field_only_before_a_schema_exists(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    before = client.get("/keys/s-marcus/summer_bridge/upload")
    assert 'name="example_page"' in before.text

    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    after = client.get("/keys/s-marcus/summer_bridge/upload")
    assert 'name="example_page"' not in after.text


# --- Gap: "waiting on a key" / "wrong key" inline fix (parent feedback, 2026-08-30) --


def test_evaluations_screen_links_to_fix_or_add_a_key_for_an_identified_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-1", problem_id="1", cause="no_key_for_page", page_number=15
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert (
        "/keys/s-marcus/summer_bridge/answers/manual-entry?page_number=15&redirect_to="
        in response.text
    )
    assert "/keys/s-marcus/summer_bridge/upload?redirect_to=" in response.text


def test_evaluations_screen_has_no_key_fix_link_without_a_resolved_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _seed_pending_problem(
        conn, capture_id="c-1", problem_id="1", cause="unknown_page", page_number=None
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert "answers/manual-entry?page_number=" not in response.text


def test_manual_answers_screen_prefills_page_number_from_the_query_string(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get(
        "/keys/s-marcus/summer_bridge/answers/manual-entry"
        "?page_number=15&redirect_to=/keys/s-marcus/summer_bridge/evaluations"
    )

    assert response.status_code == 200
    assert 'id="page_number" value="15"' in response.text
    assert 'name="redirect_to" value="/keys/s-marcus/summer_bridge/evaluations"' in response.text


def test_submit_manual_answers_with_redirect_to_returns_there_on_a_clean_save(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Parent feedback (2026-08-30): fixing a key inline from evaluations must
    not dead-end on a bare confirmation screen."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "15",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "redirect_to": "/keys/s-marcus/summer_bridge/evaluations",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge/evaluations"
    assert answer_keys.get_entry(conn, "s-marcus", "summer_bridge", 15, "1") is not None


def test_submit_manual_answers_ignores_an_external_redirect(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "15",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "redirect_to": "//evil.example.com/",
        },
        follow_redirects=False,
    )

    # Falls back to the pre-existing dead end rather than an open redirect.
    assert response.status_code == 200
    assert "saved" in response.text.lower()


def test_submit_manual_answers_with_a_conflict_carries_redirect_to_into_resolve(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/answers/manual-entry",
        data={
            "row_count": "1",
            "page_number": "15",
            "problem_number_0": "1",
            "answer_text_0": "20",  # disagrees with what's on file
            "redirect_to": "/keys/s-marcus/summer_bridge/evaluations",
        },
    )

    assert response.status_code == 200
    assert "These don't match" in response.text
    assert 'name="redirect_to" value="/keys/s-marcus/summer_bridge/evaluations"' in response.text


def test_submit_resolve_with_redirect_to_returns_there(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve",
        data={
            "row_count": "1",
            "page_number_0": "15",
            "problem_number_0": "1",
            "new_answer_text_0": "20",
            "resolution_0": "used_new",
            "redirect_to": "/keys/s-marcus/summer_bridge/evaluations",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge/evaluations"
    entry = answer_keys.get_entry(conn, "s-marcus", "summer_bridge", 15, "1")
    assert entry is not None
    assert entry.answer_text == "20"


def test_upload_screen_carries_redirect_to_into_the_form(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get(
        "/keys/s-marcus/summer_bridge/upload?redirect_to=/keys/s-marcus/summer_bridge/evaluations"
    )

    assert response.status_code == 200
    assert 'name="redirect_to" value="/keys/s-marcus/summer_bridge/evaluations"' in response.text


def test_submit_upload_with_redirect_to_carries_it_into_the_confirm_screen(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeKeyTranscriber,
) -> None:
    """The full scan -> confirm chain: a parent adding a key by photo from
    evaluations.html must land back there once she saves, same as the
    manual-entry path."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=15,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="19",
                ungradeable_reason=None,
                confidence=0.95,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        data={"redirect_to": "/keys/s-marcus/summer_bridge/evaluations"},
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="redirect_to" value="/keys/s-marcus/summer_bridge/evaluations"' in html


def test_submit_confirm_with_redirect_to_returns_there_on_a_clean_save(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "image_path": "",
            "page_number_0": "15",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "answer_text_original_0": "19",
            "redirect_to": "/keys/s-marcus/summer_bridge/evaluations",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge/evaluations"


def test_confirm_screen_shows_a_discovery_panel_with_blank_rows_even_when_nothing_was_found(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """No identity markers extracted, no schema yet -- the panel still offers
    blank rows, so a parent can define a marker by hand (name it, then type its
    value per row below) even when the model found nothing to suggest. This is
    the manual-entry fallback generalized to "nothing at all," not just "the
    model was unsure"."""
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()  # no identity_values on either entry

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="schema_name_0"' in html
    assert 'name="identity_0_0"' in html


def test_confirm_from_a_discovery_panel_saves_the_schema_and_confirms_the_first_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Scope B rework, the whole point of "learned at first scan": one submit
    both teaches the schema and confirms this scan's page under it -- no
    separate schema-only step."""
    _seed_marcus_with_source(conn)
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "schema_count": "1",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            "schema_example_0": "Day 5",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "Day 5",
        },
    )

    assert response.status_code == 200
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5", 1) == 17
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "model"


def test_confirm_from_a_discovery_panel_types_a_marker_by_hand_when_nothing_was_discovered(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model found nothing at all -- the parent names a marker from scratch
    (a blank panel row) and types its value per row. Recorded as manual, the
    same rule as the single-component fallback this generalizes."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "schema_count": "1",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "",
        },
    )

    assert response.status_code == 200
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5", 1) == 17
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_confirm_from_a_discovery_panel_with_an_unchecked_component_omits_it_from_the_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "schema_count": "2",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            # schema_include_1 omitted -- this candidate was not kept
            "schema_name_1": "worksheet_code",
            "schema_label_1": "Worksheet code",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "Day 5",
        },
    )

    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]


def test_confirm_in_targeted_mode_records_model_source_when_every_component_is_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The parent left every component as the model extracted it -- a model
    success for eval purposes, not a manual entry."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 1",
            "identity_section_original_0": "Section 1",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "model"


def test_confirm_in_targeted_mode_records_manual_source_when_any_one_component_is_edited(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model was confidently wrong about the section; the parent corrected
    just that one field. The whole row counts as manual -- confidence does not
    make the model right, and a partly-corrected row is not a model success."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 2",  # corrected
            "identity_section_original_0": "Section 1",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 2\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "manual"


def test_confirm_with_a_two_component_schema_derives_page_number_from_the_composite(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The motivating RSM bug: a printed page-footer digit ("4") repeats across
    chapters, so it cannot be trusted as this source's page_number once a
    second component (chapter) exists to disambiguate it. Two rows sharing the
    same printed page but different chapters must land in different stored
    page_numbers, and a bare "page_number_i" field in the POST body must be
    ignored outright."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "2",
            "page_number_0": "999",  # must be ignored: not source-wide unique
            "problem_number_0": "1",
            "answer_text_0": "19",
            "identity_chapter_0": "CH.3",
            "identity_chapter_original_0": "CH.3",
            "identity_page_0": "4",
            "identity_page_original_0": "4",
            "page_number_1": "999",
            "problem_number_1": "1",
            "answer_text_1": "42",
            "identity_chapter_1": "CH.4",
            "identity_chapter_original_1": "CH.4",
            "identity_page_1": "4",
            "identity_page_original_1": "4",
        },
    )

    ch3_page4 = page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "CH.3\x1f4", 1)
    ch4_page4 = page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "CH.4\x1f4", 1)
    assert ch3_page4 is not None
    assert ch4_page4 is not None
    assert ch3_page4 != ch4_page4
    # Neither row's answer collided with the other's under a shared page_number.
    assert (
        answer_keys.get_entry(conn, "s-marcus", "summer_bridge", ch3_page4, "1").answer_text  # type: ignore[union-attr]
        == "19"
    )
    assert (
        answer_keys.get_entry(conn, "s-marcus", "summer_bridge", ch4_page4, "1").answer_text  # type: ignore[union-attr]
        == "42"
    )


def test_confirm_with_a_three_component_schema_is_not_hardcoded_to_two(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The fix isn't special-cased to "exactly two components" -- any program a
    parent describes, however many levels deep, goes through the same
    resolve_or_assign_page_number path. A hypothetical Volume+Chapter+Page
    program proves the write path, not just the pure resolution logic."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("volume", "Volume", None), ("chapter", "Chapter", None), ("page", "Page", None)],
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "2",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "identity_volume_0": "Volume 1",
            "identity_volume_original_0": "Volume 1",
            "identity_chapter_0": "CH.4",
            "identity_chapter_original_0": "CH.4",
            "identity_page_0": "4",
            "identity_page_original_0": "4",
            "problem_number_1": "1",
            "answer_text_1": "42",
            "identity_volume_1": "Volume 2",
            "identity_volume_original_1": "Volume 2",
            "identity_chapter_1": "CH.4",
            "identity_chapter_original_1": "CH.4",
            "identity_page_1": "4",
            "identity_page_original_1": "4",
        },
    )

    vol1 = page_identities.get_page_number(
        conn, "s-marcus", "summer_bridge", "Volume 1\x1fCH.4\x1f4", 1
    )
    vol2 = page_identities.get_page_number(
        conn, "s-marcus", "summer_bridge", "Volume 2\x1fCH.4\x1f4", 1
    )
    assert vol1 is not None
    assert vol2 is not None
    assert vol1 != vol2


def test_confirm_with_a_two_component_schema_reuses_the_surrogate_on_a_second_scan_of_the_same_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "1",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "identity_chapter_0": "CH.4",
            "identity_chapter_original_0": "CH.4",
            "identity_page_0": "4",
            "identity_page_original_0": "4",
        },
    )
    first = page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "CH.4\x1f4", v)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "1",
            "problem_number_0": "2",
            "answer_text_0": "20",
            "identity_chapter_0": "CH.4",
            "identity_chapter_original_0": "CH.4",
            "identity_page_0": "4",
            "identity_page_original_0": "4",
        },
    )
    second = page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "CH.4\x1f4", v)

    assert first == second


def test_confirm_with_a_two_component_schema_skips_a_row_with_an_incomplete_composite(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No page to attach the answer to without the full composite -- the row is
    silently skipped, the same honesty a 0/1-component schema already has for
    an unparseable page_number."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("chapter", "Chapter", None), ("page", "Page", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "problem_number_0": "1",
            "answer_text_0": "19",
            "identity_chapter_0": "",
            "identity_chapter_original_0": "",
            "identity_page_0": "4",
            "identity_page_original_0": "4",
        },
    )

    count = conn.execute("SELECT COUNT(*) FROM answer_key_entries").fetchone()[0]
    assert count == 0


def test_confirm_in_targeted_mode_with_a_missing_component_typed_in_is_manual(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model found nothing for "section" on this block (empty original); the
    parent filled it in by hand. Also manual -- same rule as the single-component
    fallback this generalizes."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 1",
            "identity_section_original_0": "",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "manual"


def test_confirm_screen_marks_low_confidence_rows_unconfirmed(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """A block whose identifier_confidence is below the confidence floor must show
    up on the confirm screen as editable and visibly flagged -- a parent already
    checking every answer should not have to guess which identifiers need a second
    look."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
                identifier_confidence=0.4,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="identity_day_0"' in html
    assert 'value="Day 5"' in html
    assert "unconfirmed" in html.lower()


def test_confirm_screen_does_not_flag_confidently_extracted_components(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
                identifier_confidence=0.99,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    row_html = html.split('name="identity_day_0"')[1].split("</td>")[0]
    assert "unconfirmed" not in row_html.lower()


def test_confirm_with_no_identity_and_no_schema_leaves_no_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No schema, no discovery panel submitted -- nothing enters page_identities
    this round, and nothing crashes."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert response.status_code == 200
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 0


def test_confirm_stores_an_ungradeable_row_with_no_answer_text(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "2",
            "answer_text_0": "",
            "ungradeable_0": "1",
            "ungradeable_reason_0": "answers_vary",
        },
    )

    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text is None
    assert entries[0].ungradeable_reason == "answers_vary"


def test_confirming_a_brand_new_entry_logs_a_created_audit_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert len(log) == 1
    assert log[0].action == "created"
    assert log[0].new_answer_text == "8 m"


def test_saved_screen_finish_link_returns_to_the_enrollment_not_past_it(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert 'href="/keys/s-marcus/summer_bridge"' in response.text


def test_reconfirming_an_identical_answer_is_idempotent_and_logs_matched(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    payload = {
        "row_count": "1",
        "page_number_0": "17",
        "problem_number_0": "1",
        "answer_text_0": "8 m",
    }
    client.post("/keys/s-marcus/summer_bridge/confirm", data=payload)

    response = client.post("/keys/s-marcus/summer_bridge/confirm", data=payload)

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1  # no duplicate row
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [row.action for row in log] == ["created", "matched"]


def test_reconfirming_a_different_answer_does_not_overwrite_and_shows_both_versions(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "80 m",  # a genuinely different re-scan, not a correction
        },
    )

    assert response.status_code == 200
    # Never silently overwritten -- the stored value is exactly what it was before.
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "8 m"
    # Both versions shown so the parent can actually compare them.
    assert "8 m" in response.text
    assert "80 m" in response.text
    assert "kept_old" in response.text
    assert "used_new" in response.text
    # No audit row yet -- nothing has been decided.
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [row.action for row in log] == ["created"]


def test_resolving_a_conflict_by_keeping_the_old_value_leaves_storage_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "new_answer_text_0": "80 m",
            "new_ungradeable_reason_0": "",
            "resolution_0": "kept_old",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "8 m"
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert log[-1].action == "conflict_resolved"
    assert log[-1].resolution == "kept_old"


def test_resolving_a_conflict_by_using_the_new_value_updates_storage(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "new_answer_text_0": "80 m",
            "new_ungradeable_reason_0": "",
            "resolution_0": "used_new",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "80 m"
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert log[-1].action == "conflict_resolved"
    assert log[-1].resolution == "used_new"


def test_upload_when_quota_exhausted_persists_nothing_and_never_calls_the_transcriber(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
) -> None:
    _seed_marcus_with_source(conn)
    for _ in range(settings.daily_request_limit):
        quota.record_request(conn, date.today())
    transcriber.result = _success_result()

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    assert "budget" in response.text.lower() or "quota" in response.text.lower()
    assert transcriber.calls == []
    cur = conn.execute("SELECT COUNT(*) FROM answer_key_entries")
    assert cur.fetchone()[0] == 0


def test_a_failed_page_mid_sitting_does_not_lose_earlier_confirmed_pages(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
) -> None:
    """Corner case: a multi-page scanning sitting hits quota or a network failure
    partway through. Earlier confirmed pages must survive -- each page is its own
    confirm POST, so a later page's failure can't touch what's already stored."""
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )
    assert len(answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)) == 1

    for _ in range(settings.daily_request_limit):
        quota.record_request(conn, date.today())
    transcriber.result = _success_result()
    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key2.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200  # the failure is reported plainly, not a crash
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text == "8 m"  # page 17's confirmed entry survived intact


def test_upload_screen_has_immediate_feedback_and_a_disable_on_submit_wire_up(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Same bug the student capture flow had before it was fixed (see
    tests/test_web_capture.py's equivalent test): a key-page transcribe call can run
    for the better part of a minute, and until now this screen gave no acknowledgement
    at all between tapping "Upload page" and either a result or the browser's own bare
    connection-error chrome -- indistinguishable from a hang or a crash. No test
    executes JavaScript here (TestClient never runs a real browser), so this only
    proves the server-rendered contract the fix depends on: a working-state element
    exists, hidden by default; the form/input/button carry the ids the script targets;
    the script disables the control and submits via fetch() (not a plain form POST,
    which is why nothing could be shown during the wait) before the request goes out.
    """
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/upload")
    text = response.text

    assert response.status_code == 200
    assert 'id="working-state" class="working-state" hidden' in text
    working_block = text.split('id="working-state"')[1].split('id="submit-error"')[0]
    assert "minute" in working_block.lower()

    assert 'id="upload-form"' in text
    assert 'id="photo-input"' in text
    assert 'id="upload-button"' in text

    # upload.html's own script is the one with the fetch() call -- neither
    # the first (_photo_source.html's Take Photo/Upload a Photo chooser) nor
    # the last (base.html's click-to-enlarge lightbox, appended after this
    # page's own content) is the right block to check.
    script_blocks = [b.split("</script>")[0] for b in text.split("<script>")[1:]]
    script_block = next(b for b in script_blocks if "fetch(" in b)
    disable_index = script_block.index("input.disabled = true")
    fetch_index = script_block.index("fetch(")
    assert disable_index < fetch_index


def test_transcribe_failed_message_offers_a_try_again_button(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """A transient failure (a busy model, a dropped connection mid-call) is not the
    parent's fault and not necessarily the page's fault either -- the honest response
    is a clear way to try again, not a dead-end error page they have to know to
    navigate away from themselves."""
    _seed_marcus_with_source(conn)
    transcriber.result = _failure_result(
        "TransientError: Gemini returned 503", FailureKind.TRANSIENT
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    assert "Could not read that page" in html
    assert 'href="/keys/s-marcus/summer_bridge/upload"' in html
    assert "Try again" in html


def test_uploading_an_unreadable_file_fails_gracefully_instead_of_hanging(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """The real bug this exists for: normalize_orientation used to run outside
    transcribe_key_page's only try/except, and this whole route runs the
    transcribe call inside a background thread with no exception handling of
    its own -- an escaped exception there killed the worker thread silently and
    left the main thread's queue.get() waiting forever, an actual indefinite
    hang, not just a crash. Fixed one layer down
    (k12ta.pipeline.key_ingestion.transcribe_key_page); this test proves the
    fix end to end, through the real streaming route -- safe to run now that
    transcribe_key_page is guaranteed to never raise."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", b"not an image", "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    assert "Could not read that page" in html
    assert transcriber.calls == []  # never reached: the file never decoded


def test_upload_does_not_block_other_requests_while_transcribing(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key page transcribe call routinely runs tens of seconds. `submit_upload`
    used to be `async def` calling the (synchronous) transcriber directly, which
    froze the whole single-process event loop for the entire call -- indistinguishable
    from a dropped connection, since nothing else could be served meanwhile, including
    the response we were about to send. A concurrent request must still be answered
    quickly while a slow transcribe call is in flight.

    Needs its own context-managed TestClient (`with TestClient(...) as client`): only
    that form makes the client reuse one shared anyio portal/event loop across
    requests (starlette/testclient.py's `handle_request` opens a brand new, isolated
    portal per call otherwise), which is what makes two concurrent requests actually
    contend for the same event loop the way real concurrent browser tabs would.
    """
    _seed_marcus_with_source(conn)
    release = threading.Event()

    class SlowTranscriber:
        name = "slow"
        request_count = 0

        def transcribe(self, image_bytes: bytes) -> KeyPageResult:
            release.wait(timeout=5)
            return _success_result()

    keys_app.app.dependency_overrides[keys_app.get_conn] = lambda: conn
    keys_app.app.dependency_overrides[keys_app.get_settings] = lambda: settings
    monkeypatch.setattr(keys_app, "get_transcriber", lambda _settings: SlowTranscriber())

    try:
        with TestClient(keys_app.app) as client:

            def do_upload() -> None:
                client.post(
                    "/keys/s-marcus/summer_bridge/upload",
                    files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
                )

            thread = threading.Thread(target=do_upload)
            thread.start()
            try:
                time.sleep(0.2)  # let the upload reach and start blocking in transcribe()
                started = time.monotonic()
                response = client.get("/keys/s-marcus/summer_bridge")
                elapsed = time.monotonic() - started

                assert response.status_code == 200
                assert elapsed < 1.0, (
                    f"a concurrent request took {elapsed:.2f}s to answer while a "
                    "transcribe call was in flight -- the event loop is blocked"
                )
            finally:
                release.set()
                thread.join(timeout=5)
    finally:
        keys_app.app.dependency_overrides.clear()


def test_keys_app_is_unreachable_from_the_student_web_app() -> None:
    """Structural proof of isolation, not just "nobody added a link": the student
    app genuinely has no /keys route at all."""
    student_client = TestClient(web_app.app)

    response = student_client.get("/keys/s-marcus/summer_bridge/upload")

    assert response.status_code == 404


def test_policy_override_screen_says_so_when_no_pin_is_configured(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/policy-override")

    assert response.status_code == 200
    assert "K12TA_PARENT_PIN" in response.text
    assert 'name="pin"' not in response.text


def _client_with_pin(client: TestClient, settings: Settings, pin: str = "1234") -> TestClient:
    keys_app.app.dependency_overrides[keys_app.get_settings] = lambda: replace(
        settings, parent_pin=pin
    )
    return client


def test_policy_override_screen_shows_the_form_when_a_pin_is_configured(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    _seed_marcus_with_source(conn)
    _client_with_pin(client, settings)

    response = client.get("/keys/s-marcus/summer_bridge/policy-override")

    assert response.status_code == 200
    assert 'name="pin"' in response.text
    assert 'value="full"' in response.text


def test_submit_policy_override_rejects_a_wrong_pin(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    _seed_marcus_with_source(conn)
    _client_with_pin(client, settings)

    response = client.post(
        "/keys/s-marcus/summer_bridge/policy-override",
        data={"action": "set", "mode": "full", "pin": "0000"},
    )

    assert response.status_code == 200
    assert "Wrong PIN" in response.text
    assert policy_overrides.get_override(conn, "s-marcus", "summer_bridge") is None


def test_submit_policy_override_sets_the_override_and_writes_an_audit_row(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    _seed_marcus_with_source(conn)
    _client_with_pin(client, settings)

    response = client.post(
        "/keys/s-marcus/summer_bridge/policy-override",
        data={"action": "set", "mode": "full", "pin": "1234"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    override = policy_overrides.get_override(conn, "s-marcus", "summer_bridge")
    assert override is not None
    assert override.mode == "full"
    log = policy_override_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [(r.previous_mode, r.new_mode) for r in log] == [(None, "full")]


def test_submit_policy_override_clear_removes_it_and_audits_the_change(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    _seed_marcus_with_source(conn)
    _client_with_pin(client, settings)
    client.post(
        "/keys/s-marcus/summer_bridge/policy-override",
        data={"action": "set", "mode": "full", "pin": "1234"},
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/policy-override",
        data={"action": "clear", "pin": "1234"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert policy_overrides.get_override(conn, "s-marcus", "summer_bridge") is None
    log = policy_override_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [(r.previous_mode, r.new_mode) for r in log] == [(None, "full"), ("full", None)]


def test_evaluations_screen_honours_a_parent_override(
    client: TestClient, conn: sqlite3.Connection, settings: Settings
) -> None:
    """The whole point: an override actually changes what resolve_mode
    returns for this enrollment, not just what the settings screen shows."""
    _seed_marcus_with_source(conn)  # default_mode="full"
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    _client_with_pin(client, settings)
    client.post(
        "/keys/s-marcus/summer_bridge/policy-override",
        data={"action": "set", "mode": "fluency", "pin": "1234"},
    )
    _seed_pending_problem(
        conn, capture_id="c-repeat", problem_id="1", cause="needs_person", page_number=15
    )

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "Repeated attempts" in response.text


def test_manage_source_screen_offers_delete_for_an_untouched_source(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/manage")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/delete"' in response.text


def test_manage_source_screen_refuses_delete_once_a_key_exists(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.get("/keys/s-marcus/summer_bridge/manage")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/delete"' not in response.text
    assert "can't be deleted" in response.text


def test_submit_rename_source_updates_the_label(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/rename",
        data={"label": "Summer Bridge (renamed)"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    source = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert source is not None
    assert source.label == "Summer Bridge (renamed)"


def test_submit_archive_source_sets_the_flag(client: TestClient, conn: sqlite3.Connection) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/archive",
        data={"archived": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    source = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert source is not None
    assert source.archived is True


def test_submit_archive_source_can_unarchive(client: TestClient, conn: sqlite3.Connection) -> None:
    _seed_marcus_with_source(conn)
    content.set_archived(conn, "s-marcus", "summer_bridge", True)

    response = client.post(
        "/keys/s-marcus/summer_bridge/archive",
        data={"archived": "0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    source = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert source is not None
    assert source.archived is False


def test_manage_source_screen_shows_the_archive_control(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/manage")

    assert response.status_code == 200
    assert 'action="/keys/s-marcus/summer_bridge/archive"' in response.text


def test_submit_grading_mode_switches_keyed_and_keyless(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """docs/ROADMAP.md's V1 "two program paths": a parent can switch a program
    between keyed and keyless at any time -- this is the setting a parent
    changes, k12ta.grading/k12ta.pipeline (M6) is what will read it."""
    _seed_marcus_with_source(conn)  # seeded has_answer_key=True (keyed)

    response = client.post(
        "/keys/s-marcus/summer_bridge/grading-mode",
        data={"has_answer_key": "0"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    source = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert source is not None
    assert source.has_answer_key is False


def test_evaluations_screen_stays_fully_workable_once_archived(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """docs/ROADMAP.md's V1 "Archiving": everything already evaluated stays
    fully visible, and the parent's review queue stays workable, once a
    source is archived -- archiving only blocks new child uploads."""
    _seed_marcus_with_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="does-not-matter",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-graded",
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-graded",
            problem_id="1",
            prompt_text="a correct problem",
            student_answer_raw="19",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-c-graded",
            capture_id="c-graded",
            problem_id="1",
            outcome="correct",
            grader_confidence=0.99,
            page_number=17,
        ),
    )
    content.set_archived(conn, "s-marcus", "summer_bridge", True)

    response = client.get("/keys/s-marcus/summer_bridge/evaluations")

    assert response.status_code == 200
    assert "a correct problem" in response.text


def test_submit_delete_source_removes_an_untouched_source(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post("/keys/s-marcus/summer_bridge/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert content.get_content_source(conn, "s-marcus", "summer_bridge") is None


def test_submit_delete_source_refuses_once_a_key_exists(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=1,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-13T09:00:00+00:00",
        ),
    )

    response = client.post("/keys/s-marcus/summer_bridge/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge/manage"
    assert content.get_content_source(conn, "s-marcus", "summer_bridge") is not None
