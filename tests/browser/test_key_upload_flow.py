"""Regression coverage for the key-upload flow: the actual path a parent hit a
"connection interrupted" browser error on, then a dead-end "Gemini returned 503"
message on refresh (see docs/ROADMAP.md's M2 note and the fixes in
src/k12ta/keys/app.py, src/k12ta/llm/gemini.py, src/k12ta/transcribe/key_page.py).
Drives the whole path: file selection -> working state -> confirm screen -> confirm
submit -> a real row in answer_key_entries, queried directly off the same SQLite
file the server wrote to.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from playwright.sync_api import Page, expect

from k12ta.llm.base import DataRetention
from k12ta.store import answer_keys, content, page_identities, students
from k12ta.transcribe.key_page import KeyPageEntry, KeyPageResult
from tests.browser.conftest import KEY_PAGE_DENSE_IMAGE, DelayedTranscriber, LiveServer
from tests.fakes import FakeKeyTranscriber

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-keys"
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


def _success_result() -> KeyPageResult:
    return KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=27,
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
        ),
        provider="stub",
        model="stub-model",
        cost_usd=0.0,
        latency_ms=10,
        data_retention=DataRetention.NO_RETENTION,
    )


@dataclass
class _SlowProgressTranscriber:
    """Reports progress with a real pause between updates, standing in for real
    streamed chunks arriving one at a time -- so a test can observe the browser's
    live character count mid-flight, not just the eventual result."""

    inner: FakeKeyTranscriber
    updates: tuple[int, ...]
    delay_seconds: float = 0.5

    def transcribe(
        self,
        image_bytes: bytes,
        on_progress: Callable[[int], None],
        identity_schema: object = (),
    ) -> object:
        for chars in self.updates:
            time.sleep(self.delay_seconds)
            on_progress(chars)
        # A pause after the last update too, not just between updates: otherwise
        # the final result can replace the DOM before a test even gets to observe
        # the last progress checkpoint having rendered at all.
        time.sleep(self.delay_seconds)
        return self.inner.transcribe(image_bytes)


def test_upload_shows_a_live_character_count_while_reading(
    page: Page,
    keys_server: LiveServer,
    stub_key_transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of this whole change: a parent watching a static "Reading the
    page…" spinner has no way to tell "still working" from "stuck" (see
    docs/ROADMAP.md's M2 note, and the actual incident that motivated it). This
    proves the browser genuinely updates the count live, from real streamed
    server-sent progress -- not that the feature merely exists in the markup."""
    _seed_student_with_source(keys_server.connection())
    stub_key_transcriber.result = _success_result()

    import k12ta.keys.app as keys_app_module

    # Two checkpoints, not three: the point is proving sequential, live updates
    # (not one static message), and a third check here would race the DOM being
    # replaced by the final result immediately after the last on_progress call --
    # a test-timing artifact, not a product bug (both checkpoints below were
    # independently confirmed live before that race was ever an issue).
    slow = _SlowProgressTranscriber(inner=stub_key_transcriber, updates=(120, 890))
    monkeypatch.setattr(keys_app_module, "get_transcriber", lambda settings: slow)

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/upload")
    page.locator("#photo-input").set_input_files(str(KEY_PAGE_DENSE_IMAGE))
    page.click("#upload-button")

    detail = page.locator("#working-detail")
    expect(detail).to_contain_text("120 characters", timeout=2000)
    expect(detail).to_contain_text("890 characters", timeout=2000)

    answer_field = page.locator('input[name="answer_text_0"]')
    expect(answer_field).to_have_value("8 m", timeout=5000)


def test_upload_through_working_state_confirm_to_a_real_answer_key_row(
    page: Page,
    keys_server: LiveServer,
    stub_key_transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_student_with_source(keys_server.connection())
    stub_key_transcriber.result = _success_result()

    import k12ta.keys.app as keys_app_module

    delayed = DelayedTranscriber(inner=stub_key_transcriber, delay_seconds=0.6)
    monkeypatch.setattr(keys_app_module, "get_transcriber", lambda settings: delayed)

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/upload")
    page.locator("#photo-input").set_input_files(str(KEY_PAGE_DENSE_IMAGE))
    # Unlike capture.html's auto-submit-on-file-select, upload.html requires an
    # explicit tap on "Upload page" -- it listens for the form's "submit" event,
    # not the input's "change" event.
    page.click("#upload-button")

    expect(page.locator("#working-state")).to_be_visible()
    expect(page.locator("#upload-button")).to_be_disabled()

    # fetch() resolves and document.write()s the confirm screen in place.
    answer_field = page.locator('input[name="answer_text_0"]')
    expect(answer_field).to_have_value("8 m", timeout=5000)

    page.click('button:has-text("Save")')
    expect(page.locator(".message")).to_contain_text("Saved 1 entry")

    entries = answer_keys.get_entries_for_page(
        keys_server.connection(), _STUDENT_ID, _SOURCE_ID, 27
    )
    assert len(entries) == 1
    assert entries[0].answer_text == "8 m"


def _result_with_unresolved_identifier() -> KeyPageResult:
    """The model read the answer fine but couldn't read the page heading at all --
    a real, not contrived, shape: a thumb over the corner, a faded banner. No
    identity markers at all, exactly what a parent must be able to name and fill
    in by hand rather than the system silently refusing forever."""
    return KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=27,
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
                identity_values={},
                identifier_confidence=0.0,
            ),
        ),
        provider="stub",
        model="stub-model",
        cost_usd=0.0,
        latency_ms=10,
        data_retention=DataRetention.NO_RETENTION,
    )


def test_manually_entered_identifier_lands_in_page_identities_as_manual(
    page: Page,
    keys_server: LiveServer,
    stub_key_transcriber: FakeKeyTranscriber,
) -> None:
    """The manual-identifier fallback end to end, generalized to "nothing at
    all": no schema exists and the model found no markers, so the confirm
    screen's discovery panel offers only blank rows; a parent naming one by hand
    and filling in its value must produce a real page_identities row recorded
    as "manual", not "model" -- see
    k12ta.store.page_identities.PageIdentityRow.source's docstring."""
    _seed_student_with_source(keys_server.connection())
    stub_key_transcriber.result = _result_with_unresolved_identifier()

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/{_SOURCE_ID}/upload")
    page.locator("#photo-input").set_input_files(str(KEY_PAGE_DENSE_IMAGE))
    page.click("#upload-button")

    name_field = page.locator('input[name="schema_name_0"]')
    expect(name_field).to_be_visible(timeout=5000)
    name_field.fill("day")
    page.locator('input[name="schema_label_0"]').fill("Day")

    identity_field = page.locator('input[name="identity_0_0"]')
    identity_field.fill("Day 5")
    page.click('button:has-text("Save")')
    expect(page.locator(".message")).to_contain_text("Saved 1 entry")

    conn = keys_server.connection()
    assert page_identities.get_page_number(conn, _STUDENT_ID, _SOURCE_ID, "Day 5", 1) == 27
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"
