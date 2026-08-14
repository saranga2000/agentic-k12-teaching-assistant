"""Regression coverage for the capture flow's silent-wait bug: a real session found
~18s of silence between tapping the shutter and any acknowledgement, read as
"broken," inviting a retake that would have fired a second API call for the same
page (see docs/PROGRESS.md, M2, and the fix's own test in tests/test_web_capture.py
-- that test proves the server-rendered contract; this proves a real browser
actually runs the script and shows the working state while the request is still in
flight).
"""

from __future__ import annotations

from datetime import date

import pytest
from playwright.sync_api import Page, expect

from k12ta.llm.base import DataRetention
from k12ta.store import content, students
from k12ta.store import schedule as store_schedule
from k12ta.transcribe.base import TranscribedItem, TranscriptionResult
from tests.browser.conftest import SINGLE_PAGE_IMAGE, DelayedTranscriber, LiveServer
from tests.fakes import FakeTranscriber

pytestmark = pytest.mark.browser


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


def test_capture_shows_working_state_disables_input_then_renders_result(
    page: Page,
    web_server: LiveServer,
    stub_web_transcriber: FakeTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student_id = _seed_student_with_todays_source(web_server.connection())
    stub_web_transcriber.result = _success_result()

    # A real, observable delay before the transcriber responds -- long enough for
    # this test to catch the working state mid-flight, the same window a real ~18s
    # Gemini call left empty before the fix.
    import k12ta.web.app as web_app_module

    delayed = DelayedTranscriber(inner=stub_web_transcriber, delay_seconds=0.6)
    monkeypatch.setattr(web_app_module, "get_transcriber", lambda settings: delayed)

    page.goto(f"{web_server.base_url}/capture/{student_id}")
    page.locator("#photo-input").set_input_files(str(SINGLE_PAGE_IMAGE))

    expect(page.locator("#working-state")).to_be_visible()
    expect(page.locator("#photo-input")).to_be_disabled()
    expect(page.locator("#take-photo-button")).to_be_hidden()

    # The fetch() resolves and document.write()s the real result in place -- no
    # navigation, so this asserts on content, not on page.url. Not "Correct!":
    # k12ta.web's capture route has no page-number field yet (see docs/ROADMAP.md's
    # page-identity discussion), so *any* confidence still lands on the honest
    # "not sure which page this is" cause, never a graded verdict, until that's
    # built. That is what this flow actually does today; see
    # tests/browser/test_results_needs_human.py for the other three causes.
    expect(page.locator(".outcome-label")).to_contain_text("not sure which page", timeout=5000)
