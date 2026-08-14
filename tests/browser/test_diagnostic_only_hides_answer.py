"""M3.2: the concrete September case. RSM and Kumon are graded by someone else
and have no answer key on file, so resolve_mode() returns DIAGNOSTIC_ONLY
regardless of the source's own default_mode. This proves the real, rendered page
-- not just the render function in isolation -- never contains the expected
answer for a DIAGNOSTIC_ONLY assignment.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from k12ta.store import captures, content, sessions, students
from tests.browser.conftest import LiveServer

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-diagnostic-only"
_SESSION_ID = "sess-diagnostic-only"
_CAPTURE_ID = "c-diagnostic-only"
_SECRET_ANSWER = "19_SECRET_ANSWER"


def _seed_diagnostic_only_session(conn: object) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=_STUDENT_ID,
            display_name="Jahnvi",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=_STUDENT_ID,
            source_id="rsm",
            label="RSM",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=True,
            default_mode="full",  # ignored: graded_by_someone_else forces DIAGNOSTIC_ONLY
            typical_session_minutes=45,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id=_STUDENT_ID,
            assignment_id="a-rsm-1",
            source_id="rsm",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id=_STUDENT_ID,
            capture_id=_CAPTURE_ID,
            assignment_id="a-rsm-1",
            captured_at="2026-08-13T08:05:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id=_STUDENT_ID,
            capture_id=_CAPTURE_ID,
            problem_id="1",
            prompt_text="Solve for x: 2x + 5 = 43",
            student_answer_raw="18",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id=_STUDENT_ID,
            session_id=_SESSION_ID,
            assignment_id="a-rsm-1",
            started_at="2026-08-13T08:05:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id=_STUDENT_ID,
            session_id=_SESSION_ID,
            capture_id=_CAPTURE_ID,
            problem_id="1",
            outcome="incorrect",
            grader_confidence=0.99,
            expected_answer=_SECRET_ANSWER,
        ),
    )


def test_diagnostic_only_assignment_never_renders_the_expected_answer(
    page: Page, web_server: LiveServer
) -> None:
    _seed_diagnostic_only_session(web_server.connection())

    page.goto(f"{web_server.base_url}/session/{_STUDENT_ID}/{_SESSION_ID}")

    expect(page.locator(".outcome-label")).to_have_count(1)
    expect(page.locator("body")).not_to_contain_text(_SECRET_ANSWER)
