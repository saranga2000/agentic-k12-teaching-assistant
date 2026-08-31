"""Regression coverage for the "nothing happens on my MacBook" bug: the
capture screen used to offer only a native `capture="environment"` file
input, which desktop browsers largely ignore. `_photo_source.html` adds a
second, always-working "Upload a Photo" control -- a plain file input with no
`capture` attribute -- that funnels the chosen file into the same
`#photo-input` the rest of the capture flow already drives. This proves a
real browser actually wires that up, not just that the server renders the
right markup (see tests/test_web_capture.py for that half).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.browser.conftest import SINGLE_PAGE_IMAGE, LiveServer
from tests.browser.test_capture_flow import _seed_student_with_todays_source, _success_result
from tests.fakes import FakeTranscriber

pytestmark = pytest.mark.browser


def test_upload_a_photo_button_has_no_capture_attribute(page: Page, web_server: LiveServer) -> None:
    """The whole point: unlike #photo-input, the upload path must never carry
    `capture`, or it inherits the same desktop unreliability it exists to
    route around."""
    student_id = _seed_student_with_todays_source(web_server.connection())
    page.goto(f"{web_server.base_url}/capture/{student_id}")

    upload_input = page.locator("#photo-input-upload-proxy")
    expect(upload_input).to_have_count(1)
    assert upload_input.get_attribute("capture") is None


def test_uploading_a_photo_drives_the_same_capture_flow_as_taking_one(
    page: Page,
    web_server: LiveServer,
    stub_web_transcriber: FakeTranscriber,
) -> None:
    student_id = _seed_student_with_todays_source(web_server.connection())
    stub_web_transcriber.result = _success_result()

    page.goto(f"{web_server.base_url}/capture/{student_id}")

    with page.expect_file_chooser() as chooser_info:
        page.locator("#photo-input-upload-btn").click()
    chooser_info.value.set_files(str(SINGLE_PAGE_IMAGE))

    # Delivered onto #photo-input itself (via DataTransfer + a dispatched
    # "change"), so the pre-existing checklist/fetch wiring in
    # _capture_checklist.html runs completely unmodified.
    expect(page.locator("#checklist")).to_be_visible()
    expect(page).to_have_url(re.compile(rf"/session/{student_id}/"), timeout=5000)
