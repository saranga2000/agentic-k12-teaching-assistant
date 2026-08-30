"""The two-tap capture surface: student picker, capture screen, submit, reject/retake."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import k12ta.web.app as web_app
from k12ta.config import Settings
from k12ta.grading.page_identity import build_composite_key
from k12ta.ingest.schedule import get_or_create_todays_assignment
from k12ta.llm.base import DataRetention
from k12ta.respond import render as respond_render
from k12ta.store import (
    answer_keys,
    content,
    db,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    quota,
    sessions,
    students,
)
from k12ta.store import captures as store_captures
from k12ta.store import schedule as store_schedule
from k12ta.transcribe.base import FailureKind, TranscribedItem, TranscriptionResult
from tests.fakes import FakeTranscriber


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _stream_events(response: httpx.Response) -> list[dict[str, Any]]:
    """/capture's response is newline-delimited JSON -- zero or more {"type":
    "step", ...} lines as the checklist advances, then one {"type": "final",
    ...} line carrying either "html" (reject/quota/transcribe-failed, rendered
    in place) or "redirect" (a real grade, since that case has a real URL --
    /session/{student_id}/{session_id} -- worth keeping in the address bar for
    refresh/back-button/bookmark, unlike the "stay and try again" cases)."""
    return [json.loads(line) for line in response.text.strip().split("\n")]


def _final_event(response: httpx.Response) -> dict[str, Any]:
    events = _stream_events(response)
    assert events[-1]["type"] == "final"
    return events[-1]


def _step_statuses(response: httpx.Response, step: str) -> list[str]:
    return [e["status"] for e in _stream_events(response) if e.get("step") == step]


_EXIF_ORIENTATION_TAG = 0x0112


def _jpeg_bytes_with_exif_orientation(
    size: tuple[int, int], color: tuple[int, int, int], orientation: int
) -> bytes:
    image = Image.new("RGB", size, color=color)
    exif = image.getexif()
    exif[_EXIF_ORIENTATION_TAG] = orientation
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


TOO_SMALL = _jpeg_bytes((10, 10), (255, 255, 255))
TOO_DARK = _jpeg_bytes((1200, 1600), (5, 5, 5))
LOOKS_LIKE_TWO_PAGES = _jpeg_bytes((1600, 1200), (200, 200, 200))
ACCEPTED = _jpeg_bytes((1200, 1600), (200, 200, 200))
NOT_AN_IMAGE = b"whatever this is, it is not a photo"
# What an iPad camera actually produces for a single page held in portrait: raw
# buffer 1600x1200 (landscape), EXIF orientation 6 says "rotate 90 CW to display
# upright." This is the exact photo that was rejected as a two-page spread every
# time on a real device.
PORTRAIT_STORED_SIDEWAYS = _jpeg_bytes_with_exif_orientation((1600, 1200), (210, 210, 210), 6)


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
        daily_request_limit=20,
        log_level="INFO",
    )


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def client(
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    web_app.app.dependency_overrides[web_app.get_conn] = lambda: conn
    web_app.app.dependency_overrides[web_app.get_settings] = lambda: settings
    # get_transcriber is deliberately not a FastAPI dependency (see its docstring):
    # k12ta.pipeline calls it directly, only after the quota gate passes.
    monkeypatch.setattr(web_app, "get_transcriber", lambda _settings: transcriber)
    test_client = TestClient(web_app.app)
    yield test_client
    web_app.app.dependency_overrides.clear()


def _success_result(*confidences: float) -> TranscriptionResult:
    items = tuple(
        TranscribedItem(
            problem_id=str(i + 1),
            prompt_text=f"12 + {i + 1}",
            student_answer_raw="19",
            confidence=confidence,
        )
        for i, confidence in enumerate(confidences)
    )
    return TranscriptionResult(
        items=items,
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(kind: FailureKind) -> TranscriptionResult:
    return TranscriptionResult(
        items=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=200,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=f"simulated {kind.value}",
        failure_kind=kind,
    )


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


def test_root_with_no_students_shows_a_message_instead_of_a_blank_screen(
    client: TestClient,
) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "seed_dev_data" in response.text
    assert 'class="big-button"' not in response.text


def test_student_picker_links_into_the_program_picker_not_straight_to_capture(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Child-app nav restructure (docs/ROADMAP.md): choose a child, then a
    program, then what to do -- not straight from student picker to the
    camera."""
    _seed_two_students(conn)

    response = client.get("/")

    assert 'href="/student/s-marcus"' in response.text
    assert 'href="/capture/s-marcus"' not in response.text


def _seed_one_source(conn: sqlite3.Connection, student_id: str = "s-marcus") -> None:
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


def test_program_picker_skips_straight_through_with_only_one_program(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)

    response = client.get("/student/s-marcus", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/student/s-marcus/summer_bridge"


def test_program_picker_offers_a_choice_with_more_than_one_program(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="rsm",
            label="Russian School of Math",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=True,
            default_mode="diagnostic_only",
            typical_session_minutes=45,
        ),
    )

    response = client.get("/student/s-marcus")

    assert response.status_code == 200
    assert 'href="/student/s-marcus/summer_bridge"' in response.text
    assert 'href="/student/s-marcus/rsm"' in response.text


def test_program_picker_for_unknown_student_is_404(client: TestClient) -> None:
    assert client.get("/student/does-not-exist").status_code == 404


def test_source_home_offers_add_a_page_and_my_pages(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)

    response = client.get("/student/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert 'href="/capture/s-marcus?source_id=summer_bridge"' in response.text
    assert 'href="/student/s-marcus/summer_bridge/pages"' in response.text


def test_source_home_shows_how_many_are_waiting_on_a_grownup(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.9,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-13T08:00:00+00:00",
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
            grader_confidence=0.0,
            needs_human_cause="no_key_for_page",
            page_number=15,
        ),
    )

    response = client.get("/student/s-marcus/summer_bridge")

    assert "1 waiting on a grown-up" in response.text


def test_my_pages_splits_waiting_to_look_at_and_graded(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    for capture_id, problem_id, prompt, answer, outcome, cause, page in [
        ("c-waiting", "1", "12 + 7", "19", "needs_human", "no_key_for_page", 15),
        ("c-correct", "1", "3 + 4", "7", "correct", None, 16),
    ]:
        store_captures.insert_page_capture(
            conn,
            store_captures.PageCaptureRow(
                student_id="s-marcus",
                capture_id=capture_id,
                assignment_id="a-1",
                captured_at="2026-08-13T08:00:00+00:00",
                image_path="/tmp/does-not-matter.jpg",
            ),
        )
        store_captures.insert_problem(
            conn,
            store_captures.ProblemRow(
                student_id="s-marcus",
                capture_id=capture_id,
                problem_id=problem_id,
                prompt_text=prompt,
                student_answer_raw=answer,
                transcription_confidence=0.9,
            ),
        )
        sessions.insert_session(
            conn,
            sessions.SessionRow(
                student_id="s-marcus",
                session_id=f"sess-{capture_id}",
                assignment_id="a-1",
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
                outcome=outcome,
                grader_confidence=0.9,
                needs_human_cause=cause,
                page_number=page,
            ),
        )

    response = client.get("/student/s-marcus/summer_bridge/pages")

    assert response.status_code == 200
    assert "12 + 7" in response.text
    assert "3 + 4" in response.text
    assert 'action="/student/s-marcus/summer_bridge/remind"' in response.text
    assert "Nothing to look at right now." in response.text


def test_submit_reminder_sets_the_flag_the_parent_app_shows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_source(conn)
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-1",
            assignment_id="a-1",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-1",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="19",
            transcription_confidence=0.9,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-1",
            assignment_id="a-1",
            started_at="2026-08-13T08:00:00+00:00",
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
            grader_confidence=0.0,
            needs_human_cause="no_key_for_page",
            page_number=15,
        ),
    )

    response = client.post(
        "/student/s-marcus/summer_bridge/remind",
        data={"session_id": "sess-1", "capture_id": "c-1", "problem_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    pending = sessions.list_pending_for_source(conn, "s-marcus", "summer_bridge")
    assert pending[0].reminder_requested_at is not None


def test_capture_screen_shows_todays_default_assignment(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")

    response = client.get("/capture/s-marcus")

    assert response.status_code == 200
    assert "Summer bridge workbook" in response.text


def test_capture_screen_framing_guide_distinguishes_good_from_bad_without_relying_on_colour(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A 7th grader must be able to tell which example to copy at a glance -- not a
    subtle border, not colour alone. This checks for redundant, non-colour signals:
    distinct glyphs and distinct, unambiguous text labels, not just distinct CSS
    classes that happen to differ only in a border-color value."""
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")

    response = client.get("/capture/s-marcus")
    text = response.text

    assert response.status_code == 200
    # Distinct glyphs -- legible even with no colour at all.
    assert "✓" in text
    assert "✗" in text
    # Distinct, explicit text labels -- not just "one page" / "two pages" (which
    # read as parallel, equally-weighted options with no cue which to copy).
    assert "Like this" in text
    assert "Not this" in text
    # The two labels must land inside their respective good/bad markup, not just
    # appear somewhere on the page -- proves the labels are actually attached to
    # the right example, not coincidentally present.
    assert 'class="frame-good"' in text
    assert 'class="frame-bad"' in text
    good_block = text.split('class="frame-good"')[1].split('class="frame-bad"')[0]
    bad_block = text.split('class="frame-bad"')[1]
    assert "Like this" in good_block
    assert "Not this" in bad_block


def test_capture_screen_hides_the_page_framing_guide_for_an_online_exercise_source(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The framing guide illustrates "one physical page vs. two" -- a screenshot
    has no page edges to frame, and showing it would tell a parent testing an
    online programme from their laptop to do something that doesn't apply."""
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
            source_id="online_math",
            label="Online math programme",
            kind="online_exercise",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=20,
        ),
    )

    response = client.get("/capture/s-marcus?source_id=online_math")

    assert response.status_code == 200
    assert 'class="framing-guide"' not in response.text
    assert "Add Screenshot" in response.text


def test_capture_screen_has_immediate_feedback_and_a_disable_on_submit_wire_up(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A real session on a real device: after tapping the shutter there was no
    confirmation a photo had been accepted, then ~18s of silence a student read as
    "broken" and tried to retake -- which would have fired a second API call for the
    same page had the control not been disabled. No test executes JavaScript here
    (TestClient never runs a real browser), so this can only prove the server-
    rendered contract the fix depends on: a checklist element exists, hidden by
    default, with a row for every step; the button and input carry the ids the
    script targets; the script both disables the control and switches away from
    a plain full-page POST (which is *why* nothing could be shown during the
    wait -- the browser owns an untouched page until navigation completes) to a
    fetch() call the page stays in control of throughout. Whether the browser
    actually runs it correctly is a device check, not something this suite can
    certify.
    """
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")

    response = client.get("/capture/s-marcus")
    text = response.text

    assert response.status_code == 200
    assert 'id="checklist" class="checklist" hidden' in text
    checklist_block = text.split('id="checklist"')[1].split('id="submit-error"')[0]
    for label in ("Photo received", "Photo checked", "Page identified", "Answers read", "Graded"):
        assert label in checklist_block

    assert 'id="take-photo-button"' in text
    assert 'id="photo-input"' in text

    # The last <script> block is _capture_checklist.html's -- capture.html now
    # also includes _photo_source.html's own script earlier on the page (the
    # Take Photo/Upload a Photo chooser), so this can no longer assume the
    # checklist's script is the first one.
    script_block = text.split("<script>")[-1].split("</script>")[0]
    assert "fetch(" in script_block
    assert ".requestSubmit(" not in script_block
    disable_index = script_block.index("input.disabled = true")
    fetch_index = script_block.index("fetch(")
    assert disable_index < fetch_index


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


def test_a_landscape_screenshot_is_not_rejected_as_a_spread_for_an_online_exercise_source(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    """The gap the plan was written to close: SourceKind.ONLINE_EXERCISE turns
    off the photography-only spread heuristic, by configuration, not by
    guessing from the image. LOOKS_LIKE_TWO_PAGES is a plain landscape image --
    the same bytes that get rejected for a workbook source below are accepted
    here."""
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
            source_id="online_math",
            label="Online math programme",
            kind="online_exercise",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=20,
        ),
    )
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "online_math", date.today())
    transcriber.result = _success_result(0.99)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("screenshot.jpg", LOOKS_LIKE_TWO_PAGES, "image/jpeg")},
    )

    final = _final_event(response)
    assert final["redirect"].startswith("/session/s-marcus/")


def test_post_capture_with_a_good_photo_redirects_to_the_results_page(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeTranscriber,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _success_result(0.99)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    final = _final_event(response)
    assert final["redirect"].startswith("/session/s-marcus/")
    assert transcriber.request_count == 1

    cur = conn.execute(
        "SELECT capture_id, image_path FROM page_captures WHERE student_id = ?", ("s-marcus",)
    )
    row = cur.fetchone()
    assert row is not None
    assert Path(row["image_path"]).exists()

    results = client.get(final["redirect"])
    assert results.status_code == 200
    assert "12 + 1" in results.text
    # High confidence, but student capture has no page-number field yet (see
    # docs/ROADMAP.md's page-identity discussion), so the honest cause is "not sure
    # which page this is" -- not "no answer key," which would claim a page was
    # identified and specifically lacks a key, a more specific claim than this
    # system can actually make today. Distinct from "couldn't read it" either way.
    assert "not sure which page" in results.text.lower()
    assert "could not read this one clearly" not in results.text.lower()


def test_post_capture_streams_the_full_checklist_in_order_on_a_success(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    """The whole point of this: she can always see what happened and where it
    stopped. On a clean run, every step resolves ok, in order, before the
    terminal redirect -- nothing left ambiguous or mid-flight."""
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _success_result(0.99)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    events = _stream_events(response)
    steps = [(e["step"], e["status"]) for e in events if e["type"] == "step"]
    assert steps == [
        ("checked", "ok"),
        ("read", "started"),
        ("read", "ok"),
        ("graded", "ok"),
    ]
    assert events[-1]["type"] == "final"
    assert events[-1]["redirect"].startswith("/session/s-marcus/")


def test_post_capture_with_a_sideways_stored_portrait_photo_is_accepted(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    """The bug reported live: a single page held in portrait, photographed on a real
    iPad, was rejected as a two-page spread on every attempt -- because the quality
    gate read the camera's raw (unrotated) buffer dimensions instead of the
    EXIF-corrected ones. This uploads a photo built exactly the way a real camera
    stores one and asserts it is accepted, not rejected."""
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _success_result(0.99)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", PORTRAIT_STORED_SIDEWAYS, "image/jpeg")},
    )

    final = _final_event(response)
    assert final["redirect"].startswith("/session/s-marcus/")


def test_low_confidence_item_shows_the_could_not_read_message(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _success_result(0.4)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    results = client.get(_final_event(response)["redirect"])
    assert results.status_code == 200
    assert "i could not read this one clearly" in results.text.lower()


def test_needs_human_copy_for_partial_page_markers_names_seen_and_missing() -> None:
    """The whole point of storing structured detail: the message tells a child
    something actionable ("I can see the Day but not the Section"), not the
    generic "I'm not sure which page this is." """
    import json

    detail = json.dumps({"seen": ["Day"], "missing": ["Section"]})

    glyph, message = respond_render._needs_human_copy("partial_page_markers", detail)

    assert "Day" in message
    assert "Section" in message
    assert message != respond_render.UNKNOWN_PAGE_MESSAGE


def test_needs_human_copy_for_partial_page_markers_with_no_detail_falls_back_honestly() -> None:
    """A row with the cause but no detail (malformed, or predates this column)
    must still render something intelligible, never crash."""
    glyph, message = respond_render._needs_human_copy("partial_page_markers", None)

    assert message
    assert glyph


def test_needs_human_copy_joins_more_than_two_missing_labels_readably() -> None:
    import json

    detail = json.dumps({"seen": ["Day"], "missing": ["Section", "Chapter", "Unit"]})

    _, message = respond_render._needs_human_copy("partial_page_markers", detail)

    assert "Section, Chapter, and Unit" in message


def test_post_capture_when_quota_is_exhausted_calls_the_transcriber_never(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeTranscriber,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    for _ in range(settings.daily_request_limit):
        quota.record_request(conn, date.today())
    transcriber.result = _success_result(0.99)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    assert response.status_code == 200
    final = _final_event(response)
    assert "I have done all my reading for today, ask a grown-up." in final["html"]
    assert "Retake" not in final["html"]
    assert transcriber.calls == []
    # The checklist resolves honestly rather than leaving "Reading your page..."
    # stuck forever -- quota is checked before any model call, so it's the
    # "read" step that reports why, not a step left dangling mid-progress.
    assert _step_statuses(response, "read") == ["started", "failed"]

    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", ("s-marcus",))
    assert cur.fetchone()[0] == 0


def test_post_capture_when_transcription_fails_offers_retake_and_keeps_the_photo(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _failure_result(FailureKind.UNREADABLE)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    assert response.status_code == 200
    final = _final_event(response)
    assert "Retake" in final["html"]
    # Same duplicate-request risk as the initial capture: a slow retake with no
    # feedback invites a second tap. Same fix required here.
    assert 'id="checklist" class="checklist" hidden' in final["html"]
    script_block = final["html"].split("<script>")[-1].split("</script>")[0]
    assert "fetch(" in script_block
    assert ".requestSubmit(" not in script_block
    assert _step_statuses(response, "read") == ["started", "failed"]

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", ("s-marcus",))
    row = cur.fetchone()
    assert row is not None  # the photo was preserved even though transcription failed
    cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE student_id = ?", ("s-marcus",))
    assert cur.fetchone()[0] == 0


def test_post_capture_when_provider_is_rate_limited_shows_a_distinct_honest_message(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    """Not "I couldn't read this one" -- that message reads as a photo problem,
    and a real provider rate limit is not one. The two must be visibly
    different messages, not the same generic copy for two different causes."""
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _failure_result(FailureKind.RATE_LIMITED)

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    assert response.status_code == 200
    final = _final_event(response)
    assert web_app.REJECT_MESSAGES["rate_limited"] in final["html"]
    assert web_app.REJECT_MESSAGES["could_not_transcribe"] not in final["html"]
    assert _step_statuses(response, "read") == ["started", "failed"]

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", ("s-marcus",))
    row = cur.fetchone()
    assert row is not None  # the photo was preserved, same as an ordinary transcribe failure
    capture_row = store_captures.get_page_capture(conn, "s-marcus", row[0])
    assert capture_row is not None
    assert capture_row.rate_limited_reason is not None
    assert capture_row.transcribe_failure_reason is None


def test_results_page_for_an_unknown_session_is_a_clear_not_found(client: TestClient) -> None:
    response = client.get("/session/s-marcus/does-not-exist")
    assert response.status_code == 404


def test_results_page_with_zero_graded_problems_shows_an_intelligible_empty_state(
    client: TestClient,
    conn: sqlite3.Connection,
    transcriber: FakeTranscriber,
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    transcriber.result = _success_result()  # zero items detected on the page

    response = client.post(
        "/capture/s-marcus",
        data={"assignment_id": assignment.assignment_id},
        files={"photo": ("page.jpg", ACCEPTED, "image/jpeg")},
    )

    results = client.get(_final_event(response)["redirect"])
    assert results.status_code == 200
    assert "did not find any problems" in results.text.lower()


def test_results_page_renders_correct_incorrect_and_needs_human_distinctly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-synthetic",
            assignment_id=assignment.assignment_id,
            captured_at="2026-08-12T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    for problem_id, answer, confidence in (("1", "19", 0.99), ("2", "12", 0.99), ("3", "7", 0.99)):
        store_captures.insert_problem(
            conn,
            store_captures.ProblemRow(
                student_id="s-marcus",
                capture_id="c-synthetic",
                problem_id=problem_id,
                prompt_text=f"problem {problem_id}",
                student_answer_raw=answer,
                transcription_confidence=confidence,
            ),
        )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-synthetic",
            assignment_id=assignment.assignment_id,
            started_at="2026-08-12T08:00:00+00:00",
            ended_at="2026-08-12T08:00:00+00:00",
        ),
    )
    for problem_id, outcome in (("1", "correct"), ("2", "incorrect"), ("3", "needs_human")):
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id="s-marcus",
                session_id="sess-synthetic",
                capture_id="c-synthetic",
                problem_id=problem_id,
                outcome=outcome,
                grader_confidence=0.99,
                expected_answer="19",
            ),
        )

    response = client.get("/session/s-marcus/sess-synthetic")

    assert response.status_code == 200
    assert "outcome-correct" in response.text
    assert "outcome-incorrect" in response.text
    assert "outcome-needs-human" in response.text


def test_results_table_orders_by_real_question_number_not_string_order(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """ "10" must sort after "2", not before it -- plain string ordering would
    put it right after "1", which doesn't match the physical page a parent or
    student is holding while reading this table."""
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")
    assignment = get_or_create_todays_assignment(conn, "s-marcus", "summer_bridge", date.today())
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-ordering",
            assignment_id=assignment.assignment_id,
            captured_at="2026-08-12T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    for problem_id in ("10", "2", "1"):
        store_captures.insert_problem(
            conn,
            store_captures.ProblemRow(
                student_id="s-marcus",
                capture_id="c-ordering",
                problem_id=problem_id,
                # Trailing marker, not a bare number: "problem-1-marker" is not a
                # substring of "problem-10-marker", so the position check below
                # can't accidentally match the wrong row.
                prompt_text=f"problem-{problem_id}-marker",
                student_answer_raw="1",
                transcription_confidence=0.99,
            ),
        )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-ordering",
            assignment_id=assignment.assignment_id,
            started_at="2026-08-12T08:00:00+00:00",
            ended_at="2026-08-12T08:00:00+00:00",
        ),
    )
    for problem_id in ("10", "2", "1"):
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id="s-marcus",
                session_id="sess-ordering",
                capture_id="c-ordering",
                problem_id=problem_id,
                outcome="correct",
                grader_confidence=0.99,
            ),
        )

    response = client.get("/session/s-marcus/sess-ordering")

    assert response.status_code == 200
    text = response.text
    positions = {pid: text.index(f"problem-{pid}-marker") for pid in ("1", "2", "10")}
    assert positions["1"] < positions["2"] < positions["10"]


def _seed_partial_identity_session(
    conn: sqlite3.Connection, *, seen_values_json: str = '{"day": "Day 5"}'
) -> str:
    """A capture whose page identity resolved its Day but not its Section --
    PARTIAL_PAGE_MARKERS, with the seen values stored for the ask-flow to
    re-derive candidates from. Returns the session_id."""
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
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-partial",
            assignment_id="a-1",
            captured_at="2026-08-12T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-partial",
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
            session_id="sess-partial",
            assignment_id="a-1",
            started_at="2026-08-12T08:00:00+00:00",
            ended_at="2026-08-12T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-partial",
            capture_id="c-partial",
            problem_id="1",
            outcome="needs_human",
            grader_confidence=0.97,
            needs_human_cause="partial_page_markers",
            needs_human_detail='{"seen": ["Day"], "missing": ["Section"]}',
        ),
    )
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            capture_id="c-partial",
            outcome="partial",
            resolved_page_number=None,
            created_at="2026-08-12T08:00:00+00:00",
            seen_values_json=seen_values_json,
        ),
    )
    return "sess-partial"


def test_session_results_offers_a_pick_among_real_confirmed_candidates(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    session_id = _seed_partial_identity_session(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=63,
            composite_key=build_composite_key(["Section 2", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.get(f"/session/s-marcus/{session_id}")

    assert response.status_code == 200
    assert "Section" in response.text  # the missing component's parent-facing label
    assert 'name="page_number" value="15"' in response.text
    assert 'name="page_number" value="63"' in response.text
    assert "Not sure" in response.text
    assert f'action="/session/s-marcus/{session_id}/resolve-identity"' in response.text


def test_session_results_offers_a_free_text_ask_when_no_candidates_exist(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Nothing confirmed for Day 5 at all -- no constrained pick to offer, but
    per the ask-and-proceed principle (2026-08-22) that no longer means a
    bare refusal: a free-text "what page is this" ask, with her own photo,
    takes its place."""
    session_id = _seed_partial_identity_session(conn)

    response = client.get(f"/session/s-marcus/{session_id}")

    assert response.status_code == 200
    assert "resolve-identity" not in response.text
    assert "preview-page-entry" in response.text
    assert "What page is this?" in response.text
    assert "/captures/s-marcus/c-partial/image" in response.text


def test_session_results_offers_a_free_text_ask_for_unknown_page(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """UNKNOWN_PAGE -- nothing legible at all, not even a partial read -- gets
    the same free-text ask as a PARTIAL with no candidates. No candidates
    concept applies here; there was never anything to constrain a pick from."""
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
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-unknown",
            assignment_id="a-1",
            captured_at="2026-08-12T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-unknown",
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
            session_id="sess-unknown",
            assignment_id="a-1",
            started_at="2026-08-12T08:00:00+00:00",
            ended_at="2026-08-12T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-unknown",
            capture_id="c-unknown",
            problem_id="1",
            outcome="needs_human",
            grader_confidence=0.97,
            needs_human_cause="unknown_page",
        ),
    )

    response = client.get("/session/s-marcus/sess-unknown")

    assert response.status_code == 200
    assert "preview-page-entry" in response.text


def test_capture_image_serves_the_real_file(
    client: TestClient, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(ACCEPTED)
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
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-photo",
            assignment_id="a-1",
            captured_at="2026-08-12T08:00:00+00:00",
            image_path=str(image_path),
        ),
    )

    response = client.get("/captures/s-marcus/c-photo/image")

    assert response.status_code == 200
    assert response.content == ACCEPTED


def test_capture_image_for_an_unknown_capture_is_404(client: TestClient) -> None:
    response = client.get("/captures/s-marcus/no-such-capture/image")

    assert response.status_code == 404


def test_preview_page_entry_shows_the_photo_and_key_preview(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    session_id = _seed_partial_identity_session(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.post(
        f"/session/s-marcus/{session_id}/preview-page-entry",
        data={"capture_id": "c-partial", "page_number": "15"},
    )

    assert response.status_code == 200
    assert "Is this page 15?" in response.text
    assert "/captures/s-marcus/c-partial/image" in response.text
    assert "Problem 1: 19" in response.text


def test_preview_page_entry_with_no_key_yet_says_so_honestly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    session_id = _seed_partial_identity_session(conn)

    response = client.post(
        f"/session/s-marcus/{session_id}/preview-page-entry",
        data={"capture_id": "c-partial", "page_number": "15"},
    )

    assert response.status_code == 200
    assert "I don't have answers for page 15 yet" in response.text


def test_preview_page_entry_rejects_a_non_numeric_page_silently(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    session_id = _seed_partial_identity_session(conn)

    response = client.post(
        f"/session/s-marcus/{session_id}/preview-page-entry",
        data={"capture_id": "c-partial", "page_number": "not-a-number"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_commit_page_entry_grades_and_records_distinct_provenance(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Logged as RESOLVED_BY_STUDENT_ENTRY, never RESOLVED_BY_STUDENT_PICK --
    a typed, self-confirmed number is a different, weaker claim than a pick
    among real candidates, and an accuracy count must never conflate them."""
    session_id = _seed_partial_identity_session(conn)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.post(
        f"/session/s-marcus/{session_id}/commit-page-entry",
        data={"capture_id": "c-partial", "page_number": "15"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].page_number == 15
    counts = page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge")
    assert counts.get("resolved_by_student_entry") == 1
    assert counts.get("resolved_by_student_pick") is None


def test_session_results_auto_resolves_when_only_one_candidate_ever_confirmed(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The situation became unambiguous since capture time (only one section
    has ever been confirmed for this source) -- applied immediately on view,
    no stale ask shown for a question that's already answered."""
    session_id = _seed_partial_identity_session(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
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
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.get(f"/session/s-marcus/{session_id}")

    assert response.status_code == 200
    assert "resolve-identity" not in response.text
    assert "outcome-correct" in response.text
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].page_number == 15


def test_submit_identity_pick_grades_the_capture_and_records_provenance(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    session_id = _seed_partial_identity_session(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=63,
            composite_key=build_composite_key(["Section 2", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
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
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.post(
        f"/session/s-marcus/{session_id}/resolve-identity",
        data={"capture_id": "c-partial", "page_number": "15"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].page_number == 15
    counts = page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge")
    assert counts.get("resolved_by_student_pick") == 1
    # Evals must never count this as the model resolving it -- the original
    # honest "partial" log entry is untouched, not upgraded to "resolved".
    assert counts.get("partial") == 1
    assert counts.get("resolved") is None


def test_submit_identity_pick_with_a_page_number_that_is_not_a_real_candidate_does_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The requirement stated plainly: a wrong pick must fail to resolve. Here
    that means a submission naming a page_number that isn't among the fresh,
    server-recomputed candidates -- a stale or tampered request -- changes
    nothing."""
    session_id = _seed_partial_identity_session(conn)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=15,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-12T07:00:00+00:00",
        ),
    )

    response = client.post(
        f"/session/s-marcus/{session_id}/resolve-identity",
        data={"capture_id": "c-partial", "page_number": "999"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    graded = sessions.list_graded_problems_for_session(conn, "s-marcus", session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == "partial_page_markers"
    counts = page_identity_resolutions.count_outcomes_for_source(conn, "s-marcus", "summer_bridge")
    assert counts.get("resolved_by_student_pick") is None


def _seed_one_incorrect_session(
    conn: sqlite3.Connection, *, source_id: str, graded_by_someone_else: bool, default_mode: str
) -> None:
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
            source_id=source_id,
            label=source_id,
            kind="worksheet_packet",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=graded_by_someone_else,
            default_mode=default_mode,
            typical_session_minutes=30,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-synthetic",
            source_id=source_id,
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )
    store_captures.insert_page_capture(
        conn,
        store_captures.PageCaptureRow(
            student_id="s-marcus",
            capture_id="c-synthetic",
            assignment_id="a-synthetic",
            captured_at="2026-08-12T08:00:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    store_captures.insert_problem(
        conn,
        store_captures.ProblemRow(
            student_id="s-marcus",
            capture_id="c-synthetic",
            problem_id="1",
            prompt_text="12 + 7",
            student_answer_raw="18",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id="s-marcus",
            session_id="sess-synthetic",
            assignment_id="a-synthetic",
            started_at="2026-08-12T08:00:00+00:00",
            ended_at="2026-08-12T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id="s-marcus",
            session_id="sess-synthetic",
            capture_id="c-synthetic",
            problem_id="1",
            outcome="incorrect",
            grader_confidence=0.99,
            expected_answer="19_SECRET",
        ),
    )


def test_results_page_hides_the_answer_when_graded_by_someone_else(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """RSM/Kumon: graded_by_someone_else forces DIAGNOSTIC_ONLY regardless of the
    source's own default_mode -- resolve_mode()'s precedence, exercised end to end."""
    _seed_one_incorrect_session(
        conn, source_id="rsm", graded_by_someone_else=True, default_mode="full"
    )

    response = client.get("/session/s-marcus/sess-synthetic")

    assert response.status_code == 200
    assert "19_SECRET" not in response.text


def test_results_page_shows_the_answer_in_full_mode(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_one_incorrect_session(
        conn, source_id="summer_bridge", graded_by_someone_else=False, default_mode="full"
    )

    response = client.get("/session/s-marcus/sess-synthetic")

    assert response.status_code == 200
    assert "19_SECRET" in response.text


def _seed_whole_page_recapture(conn: sqlite3.Connection, *, second_guess: str) -> None:
    """The point-3 scenario: two problems photographed together, only one
    revised, the whole page re-photographed as a second capture. Both problems'
    graded rows share page_number=5, matching a real page_identity resolution.
    second_guess is problem 1's answer on the recapture -- "19" is correct
    (expected="19"), anything else is incorrect."""
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
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id="s-marcus",
            assignment_id="a-rsm",
            source_id="rsm",
            created_at="2026-08-12T08:00:00+00:00",
        ),
    )

    for capture_id, session_id, captured_at, answers in (
        ("c-first", "sess-first", "2026-08-12T08:00:00+00:00", {"1": "18", "2": "6"}),
        ("c-second", "sess-second", "2026-08-12T08:10:00+00:00", {"1": second_guess, "2": "6"}),
    ):
        store_captures.insert_page_capture(
            conn,
            store_captures.PageCaptureRow(
                student_id="s-marcus",
                capture_id=capture_id,
                assignment_id="a-rsm",
                captured_at=captured_at,
                image_path="/tmp/does-not-matter.jpg",
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
        for problem_id, answer, expected, outcome in (
            ("1", answers["1"], "19", "correct" if answers["1"] == "19" else "incorrect"),
            ("2", answers["2"], "7", "incorrect"),
        ):
            store_captures.insert_problem(
                conn,
                store_captures.ProblemRow(
                    student_id="s-marcus",
                    capture_id=capture_id,
                    problem_id=problem_id,
                    prompt_text=f"problem {problem_id}",
                    student_answer_raw=answer,
                    transcription_confidence=0.99,
                ),
            )
            sessions.insert_graded_problem(
                conn,
                sessions.GradedProblemRow(
                    student_id="s-marcus",
                    session_id=session_id,
                    capture_id=capture_id,
                    problem_id=problem_id,
                    outcome=outcome,
                    grader_confidence=0.99,
                    expected_answer=expected,
                    page_number=5,
                ),
            )


def test_a_changed_answer_on_recapture_is_suppressed_but_an_unchanged_one_is_not(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The whole-page-recapture case, end to end: she revises only problem 1 and
    re-photographs the whole page. Problem 1's new, genuinely different guess
    (now correct) must be suppressed; problem 2's unchanged resubmission must
    render exactly as a normal first attempt, not go silent."""
    _seed_whole_page_recapture(conn, second_guess="19")

    response = client.get("/session/s-marcus/sess-second")

    assert response.status_code == 200
    assert "Correct!" not in response.text
    assert "already told you what I can" in response.text
    assert "This one needs another look." in response.text


def test_a_second_new_wrong_guess_is_also_suppressed(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The mirror of the case above: the revised guess happens to be wrong this
    time, not right. Message-symmetry itself (identical regardless of
    correctness) is proven directly at the render layer in
    tests/test_respond_render.py; this only confirms the wrong-guess side is
    also suppressed end to end, not accidentally exempted."""
    _seed_whole_page_recapture(conn, second_guess="20")  # a new, still-wrong guess

    response = client.get("/session/s-marcus/sess-second")

    assert response.status_code == 200
    assert "already told you what I can" in response.text
    # Not the ordinary first-attempt incorrect message either -- suppression, not
    # a second helping of the same generic wording.
    assert response.text.count("This one needs another look.") == 1


@pytest.mark.parametrize(
    ("image_bytes", "expected_phrase"),
    [
        (TOO_SMALL, "small"),
        (TOO_DARK, "dark"),
        (LOOKS_LIKE_TWO_PAGES, "two pages"),
        (NOT_AN_IMAGE, "open that photo"),
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
    final = _final_event(response)
    assert expected_phrase in final["html"].lower()
    assert "Retake" in final["html"]
    assert _step_statuses(response, "checked") == ["failed"]

    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", ("s-marcus",))
    assert cur.fetchone()[0] == 0


def test_group_by_problem_preserves_order_within_each_group() -> None:
    rows = [
        sessions.GradedAttemptRow(
            page_number=3,
            problem_id="1",
            outcome="incorrect",
            student_answer_raw="18",
            captured_at="2026-08-12T08:00:00+00:00",
            capture_id="c-1",
        ),
        sessions.GradedAttemptRow(
            page_number=3,
            problem_id="2",
            outcome="incorrect",
            student_answer_raw="6",
            captured_at="2026-08-12T08:00:00+00:00",
            capture_id="c-1",
        ),
        sessions.GradedAttemptRow(
            page_number=3,
            problem_id="1",
            outcome="correct",
            student_answer_raw="19",
            captured_at="2026-08-12T08:10:00+00:00",
            capture_id="c-2",
        ),
    ]

    grouped = web_app._group_by_problem(rows)

    assert set(grouped.keys()) == {(3, "1"), (3, "2")}
    assert [r.student_answer_raw for r in grouped[(3, "1")]] == ["18", "19"]
    assert [r.student_answer_raw for r in grouped[(3, "2")]] == ["6"]
