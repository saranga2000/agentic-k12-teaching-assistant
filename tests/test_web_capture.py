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
from k12ta.llm.base import DataRetention
from k12ta.respond import render as respond_render
from k12ta.store import captures as store_captures
from k12ta.store import content, db, migrate, quota, sessions, students
from k12ta.store import schedule as store_schedule
from k12ta.transcribe.base import FailureKind, TranscribedItem, TranscriptionResult
from tests.fakes import FakeTranscriber


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


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


def test_capture_screen_has_immediate_feedback_and_a_disable_on_submit_wire_up(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A real session on a real device: after tapping the shutter there was no
    confirmation a photo had been accepted, then ~18s of silence a student read as
    "broken" and tried to retake -- which would have fired a second API call for the
    same page had the control not been disabled. No test executes JavaScript here
    (TestClient never runs a real browser), so this can only prove the server-
    rendered contract the fix depends on: a working-state element with the right
    copy exists, hidden by default; the button and input carry the ids the script
    targets; the script both disables the control and switches away from a plain
    full-page POST (which is *why* nothing could be shown during the wait -- the
    browser owns an untouched page until navigation completes) to a fetch() call
    the page stays in control of throughout. Whether the browser actually runs it
    correctly is a device check, not something this suite can certify.
    """
    _seed_two_students(conn)
    _seed_todays_schedule(conn, "s-marcus")

    response = client.get("/capture/s-marcus")
    text = response.text

    assert response.status_code == 200
    assert 'id="working-state" class="working-state" hidden' in text
    working_block = text.split('id="working-state"')[1].split('id="submit-error"')[0]
    assert "Got it" in working_block
    assert "a minute" in working_block.lower()

    assert 'id="take-photo-button"' in text
    assert 'id="photo-input"' in text

    script_block = text.split("<script>")[1].split("</script>")[0]
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
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/session/s-marcus/")
    assert transcriber.request_count == 1

    cur = conn.execute(
        "SELECT capture_id, image_path FROM page_captures WHERE student_id = ?", ("s-marcus",)
    )
    row = cur.fetchone()
    assert row is not None
    assert Path(row["image_path"]).exists()

    results = client.get(response.headers["location"])
    assert results.status_code == 200
    assert "12 + 1" in results.text
    # High confidence, but student capture has no page-number field yet (see
    # docs/ROADMAP.md's page-identity discussion), so the honest cause is "not sure
    # which page this is" -- not "no answer key," which would claim a page was
    # identified and specifically lacks a key, a more specific claim than this
    # system can actually make today. Distinct from "couldn't read it" either way.
    assert "not sure which page" in results.text.lower()
    assert "could not read this one clearly" not in results.text.lower()


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
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/session/s-marcus/")


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
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "i could not read this one clearly" in response.text.lower()


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
    assert "I have done all my reading for today, ask a grown-up." in response.text
    assert "Retake" not in response.text
    assert transcriber.calls == []

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
    assert "Retake" in response.text
    # Same duplicate-request risk as the initial capture: a slow retake with no
    # feedback invites a second tap. Same fix required here.
    assert 'id="working-state" class="working-state" hidden' in response.text
    script_block = response.text.split("<script>")[1].split("</script>")[0]
    assert "fetch(" in script_block
    assert ".requestSubmit(" not in script_block

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", ("s-marcus",))
    row = cur.fetchone()
    assert row is not None  # the photo was preserved even though transcription failed
    cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE student_id = ?", ("s-marcus",))
    assert cur.fetchone()[0] == 0


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
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "did not find any problems" in response.text.lower()


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
