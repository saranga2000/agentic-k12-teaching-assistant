"""The parent-only answer-key ingestion app: picker, upload+transcribe+confirm,
persist. No test hits the network -- the transcriber is monkeypatched, matching
tests/test_web_capture.py's pattern (get_transcriber is deliberately not a FastAPI
dependency, for the same reason k12ta.web's isn't: the quota gate must run before a
live adapter is ever built).
"""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import k12ta.keys.app as keys_app
import k12ta.web.app as web_app
from k12ta.config import Settings
from k12ta.llm.base import DataRetention
from k12ta.store import answer_key_audit, answer_keys, content, db, migrate, quota, students
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
    assert "data:image/jpeg;base64," in response.text
    assert 'value="17"' in response.text  # page_number
    assert 'value="8 m"' in response.text  # answer_text
    # The ungradeable entry is pre-checked, not pre-filled with an answer.
    assert response.text.count('name="ungradeable_1"') == 1
    assert "checked" in response.text.split('name="ungradeable_1"')[1].split(">")[0]


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


def test_keys_app_is_unreachable_from_the_student_web_app() -> None:
    """Structural proof of isolation, not just "nobody added a link": the student
    app genuinely has no /keys route at all."""
    student_client = TestClient(web_app.app)

    response = student_client.get("/keys/s-marcus/summer_bridge/upload")

    assert response.status_code == 404
