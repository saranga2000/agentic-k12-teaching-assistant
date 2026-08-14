"""M3.2b: the actual multi-attempt oracle attack, run against the real pipeline --
not the renderer in isolation. RSM (graded_by_someone_else) has no answer key
attached to the student's homework, so DIAGNOSTIC_ONLY applies. She photographs
the same problem wrong, then again with a different, correct answer. The second
capture must never say "Correct!" and must never contain the real answer.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from k12ta.grading.page_identity import build_composite_key
from k12ta.llm.base import DataRetention
from k12ta.store import answer_keys, content, page_identities, page_identity_schemas, students
from k12ta.transcribe.base import (
    PageIdentityExtraction,
    TranscribedItem,
    TranscriptionResult,
)
from tests.browser.conftest import SINGLE_PAGE_IMAGE, LiveServer
from tests.fakes import FakeTranscriber

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-oracle"
_SOURCE_ID = "rsm"
_PAGE_NUMBER = 5
_PAGE_MARKER = "Page 5"
_REAL_ANSWER = "19"


def _seed_gradeable_diagnostic_only_source(conn: object) -> None:
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
            label="RSM",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=True,
            default_mode="full",  # ignored: graded_by_someone_else forces DIAGNOSTIC_ONLY
            typical_session_minutes=45,
        ),
    )
    page_identity_schemas.save_new_schema(
        conn, _STUDENT_ID, _SOURCE_ID, [("page_marker", "Page marker", "Page 5")]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=_STUDENT_ID,
            source_id=_SOURCE_ID,
            page_number=_PAGE_NUMBER,
            composite_key=build_composite_key([_PAGE_MARKER]),
            schema_version=1,
            confirmed_at="2026-08-13T08:00:00+00:00",
        ),
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=_STUDENT_ID,
            source_id=_SOURCE_ID,
            page_number=_PAGE_NUMBER,
            problem_number="1",
            answer_text=_REAL_ANSWER,
            ungradeable_reason=None,
            confirmed_at="2026-08-13T08:00:00+00:00",
        ),
    )


def _capture_result(student_answer: str) -> TranscriptionResult:
    return TranscriptionResult(
        items=(
            TranscribedItem(
                problem_id="1",
                prompt_text="Solve for x: 2x + 5 = 43",
                student_answer_raw=student_answer,
                confidence=0.98,
            ),
        ),
        provider="stub",
        model="stub-model",
        cost_usd=0.0,
        latency_ms=10,
        data_retention=DataRetention.NO_RETENTION,
        page_identity=PageIdentityExtraction(
            candidates={"page_marker": (_PAGE_MARKER,)}, confidence=0.98
        ),
    )


def test_a_second_capture_with_a_changed_correct_answer_never_confirms_it(
    page: Page, web_server: LiveServer, stub_web_transcriber: FakeTranscriber
) -> None:
    _seed_gradeable_diagnostic_only_source(web_server.connection())

    stub_web_transcriber.result = _capture_result("18")  # wrong
    page.goto(f"{web_server.base_url}/capture/{_STUDENT_ID}?source_id={_SOURCE_ID}")
    page.locator("#photo-input").set_input_files(str(SINGLE_PAGE_IMAGE))
    expect(page.locator(".outcome-label")).to_be_visible(timeout=5000)

    stub_web_transcriber.result = _capture_result(_REAL_ANSWER)  # right, but a new guess
    page.goto(f"{web_server.base_url}/capture/{_STUDENT_ID}?source_id={_SOURCE_ID}")
    page.locator("#photo-input").set_input_files(str(SINGLE_PAGE_IMAGE))
    expect(page.locator(".outcome-label")).to_be_visible(timeout=5000)

    # Scoped to the coach's own message, not the whole page: "You wrote: 19" in
    # the (separate) answer div is her own input echoed back, not a disclosure --
    # the property under test is that the *coach's verdict* never confirms it.
    outcome_label = page.locator(".outcome-label")
    expect(outcome_label).not_to_contain_text("Correct!")
    expect(outcome_label).not_to_contain_text(_REAL_ANSWER)
    expect(outcome_label).to_contain_text("already told you what I can")
