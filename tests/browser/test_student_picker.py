"""Regression coverage for the bug named in AGENTS.md rule 11: an unseeded student
picker once returned 200 OK with an empty body and nothing in the log to explain
it. A status-code-only test would have passed against that; this asserts real
rendered content in a real browser.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.browser.conftest import LiveServer

pytestmark = pytest.mark.browser


def test_no_students_shows_an_intelligible_message_not_a_blank_page(
    page: Page, web_server: LiveServer
) -> None:
    page.goto(f"{web_server.base_url}/")

    expect(page.locator(".message")).to_contain_text("No students yet")
    # Just as important as the message being present: no student button rendered
    # at all. A blank body with no message and no button is exactly what the
    # original bug looked like -- 200 OK, nothing a person could act on.
    assert page.locator(".big-button").count() == 0
