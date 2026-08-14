"""Regression coverage for the key-upload dead end: a transient model failure (a
busy model, a 503 after retries exhausted) used to render a plain "Back to upload"
link indistinguishable from every other failure message. It should render an
honest, specific message and a clear "Try again" affordance -- not a page a parent
has to know to navigate away from themselves.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from k12ta.llm.base import DataRetention
from k12ta.store import content, students
from k12ta.transcribe.key_page import KeyPageResult
from tests.browser.conftest import KEY_PAGE_DENSE_IMAGE, LiveServer
from tests.fakes import FakeKeyTranscriber

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-model-failure"
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


def _transient_failure_result() -> KeyPageResult:
    from k12ta.transcribe.base import FailureKind

    return KeyPageResult(
        entries=(),
        provider="stub",
        model="stub-model",
        cost_usd=0.0,
        latency_ms=800,
        data_retention=DataRetention.NO_RETENTION,
        failure="TransientError: Gemini returned 503 after 4 retries",
        failure_kind=FailureKind.TRANSIENT,
    )


def test_transient_model_failure_offers_try_again_not_a_dead_end(
    page: Page, keys_server: LiveServer, stub_key_transcriber: FakeKeyTranscriber
) -> None:
    _seed_student_with_source(keys_server.connection())
    stub_key_transcriber.result = _transient_failure_result()

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/upload")
    page.locator("#photo-input").set_input_files(str(KEY_PAGE_DENSE_IMAGE))
    page.click("#upload-button")

    # :visible, not just .message: the working-state paragraph and the hidden
    # #submit-error paragraph both carry class="message" too, and briefly coexist
    # with this one in the DOM around the document.write() transition.
    expect(page.locator(".message:visible")).to_contain_text(
        "Could not read that page", timeout=5000
    )
    try_again = page.locator('a:has-text("Try again")')
    expect(try_again).to_be_visible()
    assert try_again.get_attribute("href") == f"/keys/{_STUDENT_ID}/{_SOURCE_ID}/upload"
