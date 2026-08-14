"""The parent-only answer-key ingestion app: picker, upload+transcribe+confirm,
persist. No test hits the network -- the transcriber is monkeypatched, matching
tests/test_web_capture.py's pattern (get_transcriber is deliberately not a FastAPI
dependency, for the same reason k12ta.web's isn't: the quota gate must run before a
live adapter is ever built).
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import k12ta.keys.app as keys_app
import k12ta.web.app as web_app
from k12ta.config import Settings
from k12ta.llm.base import DataRetention
from k12ta.store import (
    answer_key_audit,
    answer_keys,
    content,
    db,
    migrate,
    page_identities,
    page_identity_resolutions,
    quota,
    students,
)
from k12ta.transcribe.base import FailureKind
from k12ta.transcribe.key_page import KeyPageEntry, KeyPageResult
from tests.fakes import FakeKeyTranscriber


def _jpeg_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


A_KEY_PHOTO = _jpeg_bytes((1200, 1600), (210, 210, 210))


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = db.connect(":memory:")
    migrate.apply_migrations(c)
    return c


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=20,
        log_level="INFO",
    )


@pytest.fixture
def transcriber() -> FakeKeyTranscriber:
    return FakeKeyTranscriber()


@pytest.fixture
def client(
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    keys_app.app.dependency_overrides[keys_app.get_conn] = lambda: conn
    keys_app.app.dependency_overrides[keys_app.get_settings] = lambda: settings
    monkeypatch.setattr(keys_app, "get_transcriber", lambda _settings: transcriber)
    test_client = TestClient(keys_app.app)
    yield test_client
    keys_app.app.dependency_overrides.clear()


def _final_html(response: httpx.Response) -> str:
    """/upload's response is newline-delimited JSON (progress lines, then one
    final line) rather than a single HTML blob -- see test_upload_streams_
    progress_updates_then_the_final_confirm_screen. Most tests only care about
    the eventual rendered page, not the progress lines along the way."""
    lines = [json.loads(line) for line in response.text.strip().split("\n")]
    assert lines[-1]["type"] == "final"
    html: str = lines[-1]["html"]
    return html


def _seed_marcus_with_source(conn: sqlite3.Connection) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-marcus",
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            label="Summer bridge workbook",
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
                page_number=17,
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2",
                answer_text=None,
                ungradeable_reason="answers_vary",
                confidence=0.9,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(reason: str, kind: FailureKind) -> KeyPageResult:
    return KeyPageResult(
        entries=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=800,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=reason,
        failure_kind=kind,
    )


def test_picker_lists_students_and_their_enrollments(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/")

    assert response.status_code == 200
    assert "Marcus" in response.text
    assert "Summer bridge workbook" in response.text
    # Scanning a key is not a top-level action any more -- the picker links to the
    # enrollment, not straight to /upload.
    assert 'href="/keys/s-marcus/summer_bridge"' in response.text
    assert 'href="/keys/s-marcus/summer_bridge/upload"' not in response.text


def test_picker_with_no_students_shows_an_intelligible_message(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "No students" in response.text


def test_enrollment_detail_shows_scan_link_and_says_plainly_what_is_not_built_yet(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "Summer bridge workbook" in response.text
    assert 'href="/keys/s-marcus/summer_bridge/upload"' in response.text
    # No dashboard, no invented metrics -- a plain line for each thing that has no
    # data behind it yet, not an empty panel.
    assert "not shown here yet" in response.text.lower()
    assert "not tracked yet" in response.text.lower()


def test_enrollment_detail_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge").status_code == 404
    assert client.get("/keys/s-marcus/does-not-exist").status_code == 404


def test_enrollment_detail_shows_page_identity_kind_picker_with_plain_language_options(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A parent sees plain language, never the internal enum names -- see
    k12ta.keys.app.PAGE_IDENTITY_KIND_LABELS."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "Day or unit number shown on the page" in response.text
    assert "Worksheet code in the corner" in response.text
    assert "Chapter and problem numbers" in response.text
    assert "Printed page number" in response.text
    assert "Not sure yet" in response.text


def test_enrollment_detail_preselects_the_configured_kind(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.set_page_identity_kind(conn, "s-marcus", "summer_bridge", "day_or_unit_banner")

    response = client.get("/keys/s-marcus/summer_bridge")

    option_html = response.text.split('value="day_or_unit_banner"')[1].split("</option>")[0]
    assert "selected" in option_html


def test_submit_identity_kind_updates_the_content_source_and_redirects(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    assert content.get_content_source(conn, "s-marcus", "summer_bridge").page_identity_kind is None

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity-kind",
        data={"page_identity_kind": "day_or_unit_banner"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge"
    row = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert row.page_identity_kind == "day_or_unit_banner"


def test_submit_identity_kind_of_not_sure_yet_clears_it_to_none(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    content.set_page_identity_kind(conn, "s-marcus", "summer_bridge", "day_or_unit_banner")

    client.post(
        "/keys/s-marcus/summer_bridge/identity-kind",
        data={"page_identity_kind": ""},
    )

    row = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert row.page_identity_kind is None


def test_submit_identity_kind_rejects_an_unrecognised_value(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity-kind",
        data={"page_identity_kind": "made_up_kind"},
    )

    assert response.status_code == 400
    row = content.get_content_source(conn, "s-marcus", "summer_bridge")
    assert row.page_identity_kind is None


def test_submit_identity_kind_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    response = client.post(
        "/keys/does-not-exist/summer_bridge/identity-kind",
        data={"page_identity_kind": "day_or_unit_banner"},
    )
    assert response.status_code == 404


def test_enrollment_detail_with_no_resolutions_says_so_plainly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No invented zeroes dressed up as a dashboard -- a plain line when nothing
    has happened yet, same rule as the other two "not tracked"/"not shown" panels
    on this screen."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "no captures" in response.text.lower()


def test_enrollment_detail_surfaces_page_identity_resolution_counts(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Scope B instrumentation: a parent needs to know which resolution outcome
    dominates in real use, because the fix differs per outcome -- see
    k12ta.store.page_identity_resolutions.count_outcomes_for_source."""
    _seed_marcus_with_source(conn)
    now = "2026-08-13T08:00:00+00:00"
    for i, outcome in enumerate(
        [
            "resolved",
            "resolved",
            "below_floor",
            "not_found",
            "not_found",
            "not_found",
            "conflicting",
        ]
    ):
        page_identity_resolutions.insert_resolution(
            conn,
            page_identity_resolutions.PageIdentityResolutionRow(
                student_id="s-marcus",
                source_id="summer_bridge",
                capture_id=f"c-{i}",
                outcome=outcome,
                resolved_page_number=17 if outcome == "resolved" else None,
                created_at=now,
            ),
        )

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "Resolved: 2" in response.text
    assert "Below confidence floor: 1" in response.text
    assert "Identifier not found: 3" in response.text
    assert "Conflicting markers: 1" in response.text


def test_upload_screen_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/upload").status_code == 404
    assert client.get("/keys/s-marcus/does-not-exist/upload").status_code == 404


def test_successful_upload_renders_confirm_form_with_photo_and_entries(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    assert "data:image/jpeg;base64," in html
    assert 'value="17"' in html  # page_number
    assert 'value="8 m"' in html  # answer_text
    # The ungradeable entry is pre-checked, not pre-filled with an answer.
    assert html.count('name="ungradeable_1"') == 1
    assert "checked" in html.split('name="ungradeable_1"')[1].split(">")[0]


def test_confirm_screen_sorts_entries_by_page_then_problem_number(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """The model doesn't promise to emit entries in page/problem order -- multiple
    "Day N/Page NN" blocks on one photo, or a leading orphan block inferred back to
    the previous day (see prompts/transcribe_key_page.md), can easily arrive out of
    order. A parent checking answers against a printed key wants them in the same
    order the key prints them, ascending, not whatever order the model happened to
    emit."""
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=19,
                problem_number="1",
                answer_text="x",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="10",
                answer_text="y",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2",
                answer_text="z",
                ungradeable_reason=None,
                confidence=0.9,
            ),
            KeyPageEntry(
                page_number=17,
                problem_number="2a",
                answer_text="w",
                ungradeable_reason=None,
                confidence=0.9,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    # Ascending by page, then a natural (numeric-aware) ordering within a page --
    # problem "10" sorts after "2", not before it lexicographically, and "2a"
    # sorts right after "2", not off in string-order somewhere else.
    answers_in_order = [
        html.split(f'name="answer_text_{i}"')[1].split('value="')[1].split('"')[0] for i in range(4)
    ]
    assert answers_in_order == ["z", "w", "y", "x"]


def test_upload_streams_progress_updates_then_the_final_confirm_screen(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """The whole point: a parent watching a static spinner for a call that can run
    minutes has no way to tell "still working" from "stuck" (docs/ROADMAP.md's M2
    note, and the actual incident that motivated it). The response is newline-
    delimited JSON, not a single HTML blob, precisely so the browser can show
    something live instead of waiting for the whole thing."""
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()
    transcriber.progress_updates = (120, 890, 2400)

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.strip().split("\n")]

    progress_lines = [line for line in lines if line["type"] == "progress"]
    assert [line["chars"] for line in progress_lines] == [120, 890, 2400]

    assert lines[-1]["type"] == "final"
    final_html = lines[-1]["html"]
    assert "data:image/jpeg;base64," in final_html
    assert 'value="17"' in final_html


def test_confirm_persists_exactly_the_submitted_values_not_the_original_transcription(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Proves edits are honored: the parent's correction is what gets stored, not
    whatever the model originally said."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 meters",  # parent corrected the model's "8 m"
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text == "8 meters"


def test_confirm_persists_identifier_value_as_a_page_identity_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Scope B: confirming a key page is what populates the day/marker ->
    page_number mapping a student capture later resolves against -- see
    k12ta.store.page_identities. Nothing enters it before a parent confirms,
    same rule as answer_key_entries."""
    _seed_marcus_with_source(conn)
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5") is None

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identifier_value_0": "Day 5",
        },
    )

    assert response.status_code == 200
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5") == 17


def test_confirm_records_unchanged_identifier_as_model_sourced(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The parent left the model's extraction as-is -- that's a model success for
    eval purposes, not a manual entry."""
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identifier_value_0": "Day 5",
            "identifier_value_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE identifier_value = 'Day 5'"
    ).fetchone()
    assert row[0] == "model"


def test_confirm_records_edited_identifier_as_manually_sourced(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model extracted "Day 3"; the parent corrected it to "Day 5" because the
    model was confidently wrong. That correction must be counted as manual, never
    as a model success -- confidence does not make the model right."""
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identifier_value_0": "Day 5",
            "identifier_value_original_0": "Day 3",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE identifier_value = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_confirm_records_identifier_typed_from_scratch_as_manually_sourced(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model reported nothing at all (empty original); the parent supplied the
    identifier the model couldn't read. Also manual."""
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identifier_value_0": "Day 5",
            "identifier_value_original_0": "",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE identifier_value = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_confirm_screen_marks_low_confidence_identifier_blocks_as_unconfirmed(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """A block whose identifier_confidence is below the confidence floor must show
    up on the confirm screen as editable and visibly flagged -- a parent already
    checking every answer should not have to guess which identifiers need a second
    look."""
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identifier_value="Day 5",
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
                identifier_confidence=0.4,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="identifier_value_0"' in html
    assert 'value="Day 5"' in html
    assert "unconfirmed" in html.lower()


def test_confirm_screen_does_not_flag_confidently_extracted_identifiers(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identifier_value="Day 5",
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
                identifier_confidence=0.99,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    row_html = html.split('name="identifier_value_0"')[1].split("</td>")[0]
    assert "unconfirmed" not in row_html.lower()


def test_confirm_with_no_identifier_value_leaves_no_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A blank identifier_value (the model didn't report one) must not become a
    stored mapping from an empty string -- that would silently "resolve" every
    future capture with no legible marker to this one page."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert response.status_code == 200
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "") is None


def test_confirm_stores_an_ungradeable_row_with_no_answer_text(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "2",
            "answer_text_0": "",
            "ungradeable_0": "1",
            "ungradeable_reason_0": "answers_vary",
        },
    )

    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text is None
    assert entries[0].ungradeable_reason == "answers_vary"


def test_confirming_a_brand_new_entry_logs_a_created_audit_row(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert len(log) == 1
    assert log[0].action == "created"
    assert log[0].new_answer_text == "8 m"


def test_saved_screen_finish_link_returns_to_the_enrollment_not_past_it(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    assert 'href="/keys/s-marcus/summer_bridge"' in response.text


def test_reconfirming_an_identical_answer_is_idempotent_and_logs_matched(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    payload = {
        "row_count": "1",
        "page_number_0": "17",
        "problem_number_0": "1",
        "answer_text_0": "8 m",
    }
    client.post("/keys/s-marcus/summer_bridge/confirm", data=payload)

    response = client.post("/keys/s-marcus/summer_bridge/confirm", data=payload)

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1  # no duplicate row
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [row.action for row in log] == ["created", "matched"]


def test_reconfirming_a_different_answer_does_not_overwrite_and_shows_both_versions(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "80 m",  # a genuinely different re-scan, not a correction
        },
    )

    assert response.status_code == 200
    # Never silently overwritten -- the stored value is exactly what it was before.
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "8 m"
    # Both versions shown so the parent can actually compare them.
    assert "8 m" in response.text
    assert "80 m" in response.text
    assert "kept_old" in response.text
    assert "used_new" in response.text
    # No audit row yet -- nothing has been decided.
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert [row.action for row in log] == ["created"]


def test_resolving_a_conflict_by_keeping_the_old_value_leaves_storage_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "new_answer_text_0": "80 m",
            "new_ungradeable_reason_0": "",
            "resolution_0": "kept_old",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "8 m"
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert log[-1].action == "conflict_resolved"
    assert log[-1].resolution == "kept_old"


def test_resolving_a_conflict_by_using_the_new_value_updates_storage(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/resolve",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "new_answer_text_0": "80 m",
            "new_ungradeable_reason_0": "",
            "resolution_0": "used_new",
        },
    )

    assert response.status_code == 200
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert entries[0].answer_text == "80 m"
    log = answer_key_audit.list_audit_log_for_source(conn, "s-marcus", "summer_bridge")
    assert log[-1].action == "conflict_resolved"
    assert log[-1].resolution == "used_new"


def test_upload_when_quota_exhausted_persists_nothing_and_never_calls_the_transcriber(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
) -> None:
    _seed_marcus_with_source(conn)
    for _ in range(settings.daily_request_limit):
        quota.record_request(conn, date.today())
    transcriber.result = _success_result()

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    assert "budget" in response.text.lower() or "quota" in response.text.lower()
    assert transcriber.calls == []
    cur = conn.execute("SELECT COUNT(*) FROM answer_key_entries")
    assert cur.fetchone()[0] == 0


def test_a_failed_page_mid_sitting_does_not_lose_earlier_confirmed_pages(
    client: TestClient,
    conn: sqlite3.Connection,
    settings: Settings,
    transcriber: FakeKeyTranscriber,
) -> None:
    """Corner case: a multi-page scanning sitting hits quota or a network failure
    partway through. Earlier confirmed pages must survive -- each page is its own
    confirm POST, so a later page's failure can't touch what's already stored."""
    _seed_marcus_with_source(conn)
    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
        },
    )
    assert len(answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)) == 1

    for _ in range(settings.daily_request_limit):
        quota.record_request(conn, date.today())
    transcriber.result = _success_result()
    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key2.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200  # the failure is reported plainly, not a crash
    entries = answer_keys.get_entries_for_page(conn, "s-marcus", "summer_bridge", 17)
    assert len(entries) == 1
    assert entries[0].answer_text == "8 m"  # page 17's confirmed entry survived intact


def test_upload_screen_has_immediate_feedback_and_a_disable_on_submit_wire_up(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Same bug the student capture flow had before it was fixed (see
    tests/test_web_capture.py's equivalent test): a key-page transcribe call can run
    for the better part of a minute, and until now this screen gave no acknowledgement
    at all between tapping "Upload page" and either a result or the browser's own bare
    connection-error chrome -- indistinguishable from a hang or a crash. No test
    executes JavaScript here (TestClient never runs a real browser), so this only
    proves the server-rendered contract the fix depends on: a working-state element
    exists, hidden by default; the form/input/button carry the ids the script targets;
    the script disables the control and submits via fetch() (not a plain form POST,
    which is why nothing could be shown during the wait) before the request goes out.
    """
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/upload")
    text = response.text

    assert response.status_code == 200
    assert 'id="working-state" class="working-state" hidden' in text
    working_block = text.split('id="working-state"')[1].split('id="submit-error"')[0]
    assert "minute" in working_block.lower()

    assert 'id="upload-form"' in text
    assert 'id="photo-input"' in text
    assert 'id="upload-button"' in text

    script_block = text.split("<script>")[1].split("</script>")[0]
    assert "fetch(" in script_block
    disable_index = script_block.index("input.disabled = true")
    fetch_index = script_block.index("fetch(")
    assert disable_index < fetch_index


def test_transcribe_failed_message_offers_a_try_again_button(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """A transient failure (a busy model, a dropped connection mid-call) is not the
    parent's fault and not necessarily the page's fault either -- the honest response
    is a clear way to try again, not a dead-end error page they have to know to
    navigate away from themselves."""
    _seed_marcus_with_source(conn)
    transcriber.result = _failure_result(
        "TransientError: Gemini returned 503", FailureKind.TRANSIENT
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    assert response.status_code == 200
    html = _final_html(response)
    assert "Could not read that page" in html
    assert 'href="/keys/s-marcus/summer_bridge/upload"' in html
    assert "Try again" in html


def test_upload_does_not_block_other_requests_while_transcribing(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key page transcribe call routinely runs tens of seconds. `submit_upload`
    used to be `async def` calling the (synchronous) transcriber directly, which
    froze the whole single-process event loop for the entire call -- indistinguishable
    from a dropped connection, since nothing else could be served meanwhile, including
    the response we were about to send. A concurrent request must still be answered
    quickly while a slow transcribe call is in flight.

    Needs its own context-managed TestClient (`with TestClient(...) as client`): only
    that form makes the client reuse one shared anyio portal/event loop across
    requests (starlette/testclient.py's `handle_request` opens a brand new, isolated
    portal per call otherwise), which is what makes two concurrent requests actually
    contend for the same event loop the way real concurrent browser tabs would.
    """
    _seed_marcus_with_source(conn)
    release = threading.Event()

    class SlowTranscriber:
        name = "slow"
        request_count = 0

        def transcribe(self, image_bytes: bytes) -> KeyPageResult:
            release.wait(timeout=5)
            return _success_result()

    keys_app.app.dependency_overrides[keys_app.get_conn] = lambda: conn
    keys_app.app.dependency_overrides[keys_app.get_settings] = lambda: settings
    monkeypatch.setattr(keys_app, "get_transcriber", lambda _settings: SlowTranscriber())

    try:
        with TestClient(keys_app.app) as client:

            def do_upload() -> None:
                client.post(
                    "/keys/s-marcus/summer_bridge/upload",
                    files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
                )

            thread = threading.Thread(target=do_upload)
            thread.start()
            try:
                time.sleep(0.2)  # let the upload reach and start blocking in transcribe()
                started = time.monotonic()
                response = client.get("/keys/s-marcus/summer_bridge")
                elapsed = time.monotonic() - started

                assert response.status_code == 200
                assert elapsed < 1.0, (
                    f"a concurrent request took {elapsed:.2f}s to answer while a "
                    "transcribe call was in flight -- the event loop is blocked"
                )
            finally:
                release.set()
                thread.join(timeout=5)
    finally:
        keys_app.app.dependency_overrides.clear()


def test_keys_app_is_unreachable_from_the_student_web_app() -> None:
    """Structural proof of isolation, not just "nobody added a link": the student
    app genuinely has no /keys route at all."""
    student_client = TestClient(web_app.app)

    response = student_client.get("/keys/s-marcus/summer_bridge/upload")

    assert response.status_code == 404
