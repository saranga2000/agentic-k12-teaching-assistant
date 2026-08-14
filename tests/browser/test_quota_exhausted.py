"""Student capture when the daily request quota is already exhausted. This path
exists in k12ta.pipeline.process (checked and unit-tested there) but had never been
exercised end to end through a real browser before this test -- the exact path a
busy evening hits once both children have photographed a few pages. It must render
the honest, already-defined QUOTA_EXHAUSTED_MESSAGE, not a blank page, a crash, or
an attempt to call the model anyway.
"""

from __future__ import annotations

from datetime import date

import pytest
from playwright.sync_api import Page, expect

from k12ta.store import content, quota, students
from k12ta.store import schedule as store_schedule
from tests.browser.conftest import SINGLE_PAGE_IMAGE, LiveServer
from tests.fakes import FakeTranscriber

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-quota"
_SOURCE_ID = "summer_bridge"


def _seed_student_with_todays_source_and_exhausted_quota(conn: object) -> None:
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
    store_schedule.set_default_source(
        conn,
        store_schedule.WeeklyDefaultSourceRow(
            student_id=_STUDENT_ID, weekday=date.today().weekday(), source_id=_SOURCE_ID
        ),
    )
    # web_server fixes K12TA_DAILY_REQUEST_LIMIT at 20 -- exhaust it precisely,
    # not by guessing a small configured limit, matching the pattern already used
    # for the keys-app quota test in tests/test_keys_app.py.
    for _ in range(20):
        quota.record_request(conn, date.today())


def test_quota_exhausted_shows_the_honest_message_and_never_calls_the_model(
    page: Page, web_server: LiveServer, stub_web_transcriber: FakeTranscriber
) -> None:
    conn = web_server.connection()
    _seed_student_with_todays_source_and_exhausted_quota(conn)

    page.goto(f"{web_server.base_url}/capture/{_STUDENT_ID}")
    page.locator("#photo-input").set_input_files(str(SINGLE_PAGE_IMAGE))

    # :visible, not just .message: the working-state paragraph and the hidden
    # #submit-error paragraph both carry class="message" too, and briefly coexist
    # with this one in the DOM around the document.write() transition.
    expect(page.locator(".message:visible")).to_contain_text(
        "done all my reading for today", timeout=5000
    )
    # The honest message, not a crash: no server error page, no half-rendered
    # screen. And the quota gate must short-circuit before ever building or
    # calling a transcriber -- a quota-exhausted request must not still pay the
    # cost of a model call it was never going to use.
    assert stub_web_transcriber.calls == []
