"""M3.1: the content source ("enrollment") setup flow. Drives the real form --
picker -> "+ Add an enrollment" -> fill in every field -> submit -> lands on the
real enrollment detail page for the source it just created, queried directly off
the same SQLite file the server wrote to.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from k12ta.store import content, students
from tests.browser.conftest import LiveServer

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-enrollment-setup"


def _seed_student(conn: object) -> None:
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


def test_add_enrollment_end_to_end_creates_a_real_content_source(
    page: Page, keys_server: LiveServer
) -> None:
    _seed_student(keys_server.connection())

    page.goto(f"{keys_server.base_url}/")
    page.click(f'a[href="/keys/{_STUDENT_ID}/enrollments/new"]')

    page.fill("#label", "RSM")
    page.select_option("#kind", "worksheet_packet")
    page.fill("#subject", "math")
    page.check('input[name="has_answer_key"]')
    page.check('input[name="graded_by_someone_else"]')
    page.select_option("#default_mode", "diagnostic_only")
    page.fill("#typical_session_minutes", "45")
    page.click('button:has-text("Add enrollment")')

    expect(page).to_have_url(f"{keys_server.base_url}/keys/{_STUDENT_ID}/rsm")

    row = content.get_content_source(keys_server.connection(), _STUDENT_ID, "rsm")
    assert row is not None
    assert row.label == "RSM"
    assert row.kind == "worksheet_packet"
    assert row.subject == "math"
    assert row.has_answer_key is True
    assert row.graded_by_someone_else is True
    assert row.default_mode == "diagnostic_only"
    assert row.typical_session_minutes == 45


def test_add_enrollment_with_a_blank_label_shows_an_error_and_creates_nothing(
    page: Page, keys_server: LiveServer
) -> None:
    _seed_student(keys_server.connection())

    page.goto(f"{keys_server.base_url}/keys/{_STUDENT_ID}/enrollments/new")
    page.select_option("#kind", "workbook")
    page.fill("#subject", "math")
    page.select_option("#default_mode", "full")
    page.fill("#typical_session_minutes", "30")
    page.click('button:has-text("Add enrollment")')

    expect(page.locator(".message").first).to_contain_text("Label is required")
    assert content.list_content_sources(keys_server.connection(), _STUDENT_ID) == []
