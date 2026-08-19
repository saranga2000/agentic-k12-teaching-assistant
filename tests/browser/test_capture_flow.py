"""Regression coverage for the capture flow's silent-wait bug: a real session found
~18s of silence between tapping the shutter and any acknowledgement, read as
"broken," inviting a retake that would have fired a second API call for the same
page (see docs/PROGRESS.md, M2, and the fix's own test in tests/test_web_capture.py
-- that test proves the server-rendered contract; this proves a real browser
actually runs the script and shows the working state while the request is still in
flight).
"""

from __future__ import annotations

import io
import re
from datetime import date

import pytest
from PIL import Image
from playwright.sync_api import FilePayload, Page, expect

from k12ta.llm.base import DataRetention
from k12ta.store import content, students
from k12ta.store import schedule as store_schedule
from k12ta.transcribe.base import TranscribedItem, TranscriptionResult
from tests.browser.conftest import SINGLE_PAGE_IMAGE, DelayedTranscriber, LiveServer
from tests.fakes import FakeTranscriber

pytestmark = pytest.mark.browser


def _landscape_jpeg_payload() -> FilePayload:
    """In-memory, not a checked-in fixture: the only thing that matters is the
    aspect ratio (>= capture.SPREAD_ASPECT_RATIO_THRESHOLD), so a real file on
    disk would add nothing a synthetic one doesn't already give."""
    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), color=(200, 200, 200)).save(buf, format="JPEG")
    return FilePayload(name="screenshot.jpg", mimeType="image/jpeg", buffer=buf.getvalue())


def _seed_student_with_todays_source(conn: object) -> str:
    student_id = "s-browser-capture"
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Jahnvi",
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
            label="Summer Bridge",
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
    return student_id


def _success_result() -> TranscriptionResult:
    return TranscriptionResult(
        items=(
            TranscribedItem(
                problem_id="1", prompt_text="14 + 7", student_answer_raw="21", confidence=0.98
            ),
        ),
        provider="stub",
        model="stub-model",
        cost_usd=0.0,
        latency_ms=10,
        data_retention=DataRetention.NO_RETENTION,
    )


def test_capture_shows_checklist_progress_then_redirects_to_the_result(
    page: Page,
    web_server: LiveServer,
    stub_web_transcriber: FakeTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student_id = _seed_student_with_todays_source(web_server.connection())
    stub_web_transcriber.result = _success_result()

    # A real, observable delay before the transcriber responds -- long enough for
    # this test to catch the checklist mid-flight, the same window a real ~18s
    # Gemini call left empty before the original silent-wait fix.
    import k12ta.web.app as web_app_module

    delayed = DelayedTranscriber(inner=stub_web_transcriber, delay_seconds=0.6)
    monkeypatch.setattr(web_app_module, "get_transcriber", lambda settings: delayed)

    page.goto(f"{web_server.base_url}/capture/{student_id}")
    page.locator("#photo-input").set_input_files(str(SINGLE_PAGE_IMAGE))

    expect(page.locator("#checklist")).to_be_visible()
    expect(page.locator('[data-step="checked"]')).to_have_class("checklist-item done")
    expect(page.locator('[data-step="read"]')).to_have_class("checklist-item active")
    expect(page.locator("#photo-input")).to_be_disabled()
    expect(page.locator("#take-photo-button")).to_be_hidden()

    # A real grade has its own URL (see _stream_capture_response's docstring),
    # so this is a real navigation now, not a document.write() in place --
    # assert on both the address bar and the rendered content. Not "Correct!":
    # k12ta.web's capture route has no page-number field yet (see docs/ROADMAP.md's
    # page-identity discussion), so *any* confidence still lands on the honest
    # "not sure which page this is" cause, never a graded verdict, until that's
    # built. That is what this flow actually does today; see
    # tests/browser/test_results_needs_human.py for the other three causes.
    expect(page).to_have_url(re.compile(rf"/session/{student_id}/"), timeout=5000)
    expect(page.locator(".outcome-label")).to_contain_text("not sure which page", timeout=5000)


def test_checklist_is_hidden_on_a_fresh_capture_screen(page: Page, web_server: LiveServer) -> None:
    """The real bug: `.working-state { display: flex; ... }` (now `.checklist`)
    in base.html had no `[hidden]` override, and an author-stylesheet rule
    always beats the browser's default `[hidden] { display: none }` regardless
    of specificity -- so the `hidden` attribute on this div was never actually
    hiding it. Before the fix, a student opened the capture screen and saw a
    "Checking your page..." spinner under the Take Photo button before taking
    any photo at all."""
    student_id = _seed_student_with_todays_source(web_server.connection())

    page.goto(f"{web_server.base_url}/capture/{student_id}")

    expect(page.locator("#checklist")).to_be_hidden()


def test_checklist_stays_hidden_on_a_rejected_photo(page: Page, web_server: LiveServer) -> None:
    """The exact incident reported live: a landscape photo gets rejected as a
    two-page spread, and result.html's own (also unconditionally-present,
    hidden-by-default) checklist was visible at the same time -- contradictory,
    and since nothing in this flow's JS ever intended to show it here, nothing
    ever hid it either."""
    student_id = _seed_student_with_todays_source(web_server.connection())

    page.goto(f"{web_server.base_url}/capture/{student_id}")
    page.locator("#photo-input").set_input_files(_landscape_jpeg_payload())

    # A stable signal specific to the post-document.write() reject page (the
    # pre-fetch capture.html has no "Retake" text anywhere), so this waits out
    # the transition before asserting on content -- .message matches more than
    # one element while the old and new documents briefly overlap.
    expect(page.locator("#take-photo-button")).to_contain_text("Retake", timeout=5000)
    expect(page.locator(".message").first).to_contain_text("two pages")
    expect(page.locator("#checklist")).to_be_hidden()
