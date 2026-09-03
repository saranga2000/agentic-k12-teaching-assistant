"""Real-browser coverage for the Ledger repaint's new interactive pieces
(docs/ROADMAP.md's M9), 2026-09-01/02. Everything up to now proved these
behaviors as server-rendered HTML strings via TestClient
(tests/test_static_assets.py, tests/test_keys_app.py) -- real, but it never
actually clicks anything or asks a browser to run the JavaScript in between.
This file drives the same three pieces through a real Chromium, matching
this suite's own established shape (see conftest.py's own module docstring
for what a green run here does and doesn't prove).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from k12ta.store import captures, content, sessions, students
from tests.browser.conftest import SINGLE_PAGE_IMAGE, LiveServer

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-ledger"
_SOURCE_ID = "summer_bridge"


def _seed_student_with_source(conn: object) -> None:
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
            source_id=_SOURCE_ID,
            label="Summer Bridge",
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
            student_id=_STUDENT_ID,
            assignment_id="does-not-matter",
            source_id=_SOURCE_ID,
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )


def _seed_pending_capture(conn: object, *, capture_id: str) -> None:
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id=_STUDENT_ID,
            capture_id=capture_id,
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path=str(SINGLE_PAGE_IMAGE),
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id=_STUDENT_ID,
            session_id=f"sess-{capture_id}",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )


def _seed_pending_problem(
    conn: object, *, capture_id: str, problem_id: str, page_number: int
) -> None:
    """Adds one more pending problem to a capture already seeded by
    _seed_pending_capture -- callers wanting several questions in the same
    page group (as a single real photo would produce) call this more than
    once with the same capture_id."""
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id=_STUDENT_ID,
            capture_id=capture_id,
            problem_id=problem_id,
            prompt_text=f"problem {problem_id}",
            student_answer_raw="some answer",
            transcription_confidence=0.9,
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id=_STUDENT_ID,
            session_id=f"sess-{capture_id}",
            capture_id=capture_id,
            problem_id=problem_id,
            outcome="needs_human",
            grader_confidence=0.9,
            needs_human_cause="needs_person",
            page_number=page_number,
        ),
    )


def _seed_correct_graded_problem(conn: object, *, capture_id: str, page_number: int) -> None:
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id=_STUDENT_ID,
            capture_id=capture_id,
            assignment_id="does-not-matter",
            captured_at="2026-08-13T08:00:00+00:00",
            image_path=str(SINGLE_PAGE_IMAGE),
        ),
    )
    captures.insert_problem(
        conn,
        captures.ProblemRow(
            student_id=_STUDENT_ID,
            capture_id=capture_id,
            problem_id="1",
            prompt_text="14 + 7",
            student_answer_raw="21",
            transcription_confidence=0.99,
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id=_STUDENT_ID,
            session_id=f"sess-{capture_id}",
            assignment_id="does-not-matter",
            started_at="2026-08-13T08:00:00+00:00",
        ),
    )
    sessions.insert_graded_problem(
        conn,
        sessions.GradedProblemRow(
            student_id=_STUDENT_ID,
            session_id=f"sess-{capture_id}",
            capture_id=capture_id,
            problem_id="1",
            outcome="correct",
            grader_confidence=0.99,
            page_number=page_number,
        ),
    )


def test_theme_toggle_overrides_system_preference_and_persists(
    page: Page, web_server: LiveServer
) -> None:
    """A household member's tablet may be set to either system theme; the
    toggle (top-right, both apps, shared via _theme_toggle.html) must win
    over it regardless, and remember the choice on the next visit."""
    page.emulate_media(color_scheme="light")
    page.goto(f"{web_server.base_url}/")

    html = page.locator("html")
    assert html.get_attribute("data-theme") is None  # system-driven so far
    expect(page.locator(".theme-toggle .icon-sun")).to_be_visible()
    expect(page.locator(".theme-toggle .icon-moon")).to_be_hidden()

    page.locator("#theme-toggle").click()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(page.locator(".theme-toggle .icon-moon")).to_be_visible()
    expect(page.locator(".theme-toggle .icon-sun")).to_be_hidden()

    # Persists across a fresh navigation -- theme-init.js re-reads
    # localStorage before first paint on every load, not just this one.
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_mark_all_correct_checks_every_radio_without_submitting(
    page: Page, keys_server: LiveServer
) -> None:
    """k12ta.keys.app.submit_bulk_answer_verdict's own docstring: nothing is
    ever pre-checked by the server, since no retained model confidence
    exists for these causes to honestly pre-suggest from. "Mark all as
    correct" is a parent-initiated convenience that fills in a choice they
    still have to review and submit themselves -- proven here by checking
    every radio lands checked, then confirming nothing left the browser
    (still needs_human in the database, not yet graded)."""
    conn = keys_server.connection()
    _seed_student_with_source(conn)
    _seed_pending_capture(conn, capture_id="c-1")
    _seed_pending_problem(conn, capture_id="c-1", problem_id="1", page_number=15)
    _seed_pending_problem(conn, capture_id="c-1", problem_id="2", page_number=15)

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/evaluations")
    radios = page.locator('input[type="radio"][value="correct"]')
    expect(radios).to_have_count(2)
    for i in range(2):
        expect(radios.nth(i)).not_to_be_checked()

    page.locator(".mark-all-correct").first.click()
    for i in range(2):
        expect(radios.nth(i)).to_be_checked()

    # A button, not a submit -- still the same page, nothing graded yet.
    expect(page).to_have_url(re.compile(r"/evaluations$"))
    graded = sessions.list_graded_problems_for_session(
        keys_server.connection(), _STUDENT_ID, "sess-c-1"
    )
    assert graded[0].outcome == "needs_human"


def test_correct_tab_unfolds_the_graded_section(page: Page, keys_server: LiveServer) -> None:
    """Parent feedback, 2026-09-01: the jump-nav tabs must both scroll to and
    unfold their section -- proven here by checking the folded table is
    genuinely not rendered visible until the real click happens, in a real
    browser, not just that the HTML for both states exists somewhere in the
    response body."""
    conn = keys_server.connection()
    _seed_student_with_source(conn)
    _seed_correct_graded_problem(conn, capture_id="c-correct", page_number=17)

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/evaluations")
    graded_table = page.locator("#graded-correct table.graded-table")
    expect(graded_table).to_be_hidden()

    page.get_by_role("link", name=re.compile(r"^Correct\b")).click()
    expect(graded_table).to_be_visible()
    expect(graded_table).to_contain_text("14 + 7")
