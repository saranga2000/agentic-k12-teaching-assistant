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
    page_identity_schemas,
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


def test_enrollment_detail_links_to_the_identity_schema_editor(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert 'href="/keys/s-marcus/summer_bridge/identity-schema"' in response.text


def test_identity_schema_screen_for_a_source_with_no_schema_shows_blank_rows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert response.status_code == 200
    assert 'name="component_name_0"' in response.text


def test_identity_schema_screen_prefills_the_current_schema(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    response = client.get("/keys/s-marcus/summer_bridge/identity-schema")

    assert response.status_code == 200
    assert 'value="section"' in response.text
    assert 'value="Section"' in response.text
    assert 'value="day"' in response.text


def test_submit_identity_schema_saves_components_and_redirects(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "2",
            "component_name_0": "section",
            "component_label_0": "Section",
            "component_example_0": "Section 1",
            "component_name_1": "day",
            "component_label_1": "Day",
            "component_example_1": "Day 5",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/keys/s-marcus/summer_bridge"
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["section", "day"]


def test_submit_identity_schema_with_no_components_leaves_the_schema_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "3",
            "component_name_0": "",
            "component_name_1": "",
            "component_name_2": "",
        },
    )

    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]


def test_editing_the_schema_bumps_the_version_and_does_not_touch_existing_mappings(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The explicit requirement: a schema change must flag an existing mapping
    for review, never drop it or silently reinterpret it -- see
    k12ta.store.page_identities' staleness rule."""
    _seed_marcus_with_source(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", None)]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=v1,
            confirmed_at="2026-08-14T00:00:00+00:00",
        ),
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity-schema",
        data={
            "component_count": "2",
            "component_name_0": "section",
            "component_label_0": "Section",
            "component_name_1": "day",
            "component_label_1": "Day",
        },
    )

    new_version = page_identity_schemas.get_current_version(conn, "s-marcus", "summer_bridge")
    assert new_version == 2
    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 1", 1) == 13
    )  # untouched, still there under the old version
    assert (
        page_identities.count_stale_for_source(conn, "s-marcus", "summer_bridge", new_version) == 1
    )


def test_identity_schema_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/identity-schema").status_code == 404
    assert (
        client.post("/keys/does-not-exist/summer_bridge/identity-schema", data={}).status_code
        == 404
    )


def test_manual_mapping_screen_shows_one_field_per_current_schema_component(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )

    response = client.get("/keys/s-marcus/summer_bridge/identity/manual-entry")

    assert response.status_code == 200
    assert 'name="component_section"' in response.text
    assert 'name="component_day"' in response.text


def test_manual_mapping_screen_for_a_source_with_no_schema_says_so_plainly(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Nothing to map against yet -- an honest message, not a blank or broken
    form. Set up the schema first, at /identity-schema."""
    _seed_marcus_with_source(conn)

    response = client.get("/keys/s-marcus/summer_bridge/identity/manual-entry")

    assert response.status_code == 200
    assert "no identity schema" in response.text.lower()
    assert 'name="component_' not in response.text


def test_submit_manual_mapping_persists_a_composite_marked_manual(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The backfill mechanism: entering a day-to-page mapping you've verified
    against the physical book yourself, no re-scan, no photo -- recorded as
    manual so the eval never mistakes it for a model success."""
    _seed_marcus_with_source(conn)
    v = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"page_number": "17", "component_section": "Section 1", "component_day": "Day 5"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (
        page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Section 1\x1fDay 5", v)
        == 17
    )
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_submit_manual_mapping_with_a_blank_component_persists_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A half-filled composite can never match a future capture's fully-populated
    one -- same rule as the confirm screen's per-row identity fields."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"page_number": "17", "component_section": "", "component_day": "Day 5"},
    )

    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 0


def test_submit_manual_mapping_for_a_source_with_no_schema_is_a_400(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/identity/manual-entry",
        data={"page_number": "17"},
    )

    assert response.status_code == 400


def test_manual_mapping_for_unknown_student_or_source_is_404(client: TestClient) -> None:
    assert client.get("/keys/does-not-exist/summer_bridge/identity/manual-entry").status_code == 404
    assert (
        client.post("/keys/does-not-exist/summer_bridge/identity/manual-entry", data={}).status_code
        == 404
    )


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
            "no_mapping",
            "no_mapping",
            "no_mapping",
            "conflicting",
            "partial",
            "no_schema",
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
    assert "No mapping yet: 3" in response.text
    assert "Conflicting markers: 1" in response.text
    assert "Partially identified: 1" in response.text
    assert "No identity schema yet: 1" in response.text


def test_enrollment_detail_surfaces_stale_mapping_count_after_a_schema_change(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    _seed_marcus_with_source(conn)
    v1 = page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", None)]
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=13,
            composite_key="Day 1",
            schema_version=v1,
            confirmed_at="2026-08-14T00:00:00+00:00",
        ),
    )
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    response = client.get("/keys/s-marcus/summer_bridge")

    assert response.status_code == 200
    assert "1 mapping" in response.text
    assert "needs review" in response.text


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


def test_confirm_screen_shows_a_discovery_panel_when_the_scan_found_markers_and_no_schema_exists(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
                problem_number="1",
                answer_text="8 m",
                ungradeable_reason=None,
                confidence=0.95,
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
    assert 'name="schema_name_0"' in html
    assert 'value="day"' in html
    assert 'name="identity_0_0"' in html
    assert 'value="Day 5"' in html


def test_confirm_screen_shows_a_discovery_panel_with_blank_rows_even_when_nothing_was_found(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """No identity markers extracted, no schema yet -- the panel still offers
    blank rows, so a parent can define a marker by hand (name it, then type its
    value per row below) even when the model found nothing to suggest. This is
    the manual-entry fallback generalized to "nothing at all," not just "the
    model was unsure"."""
    _seed_marcus_with_source(conn)
    transcriber.result = _success_result()  # no identity_values on either entry

    response = client.post(
        "/keys/s-marcus/summer_bridge/upload",
        files={"photo": ("key.jpg", A_KEY_PHOTO, "image/jpeg")},
    )

    html = _final_html(response)
    assert 'name="schema_name_0"' in html
    assert 'name="identity_0_0"' in html


def test_confirm_from_a_discovery_panel_saves_the_schema_and_confirms_the_first_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Scope B rework, the whole point of "learned at first scan": one submit
    both teaches the schema and confirms this scan's page under it -- no
    separate schema-only step."""
    _seed_marcus_with_source(conn)
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "schema_count": "1",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            "schema_example_0": "Day 5",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "Day 5",
        },
    )

    assert response.status_code == 200
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5", 1) == 17
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "model"


def test_confirm_from_a_discovery_panel_types_a_marker_by_hand_when_nothing_was_discovered(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model found nothing at all -- the parent names a marker from scratch
    (a blank panel row) and types its value per row. Recorded as manual, the
    same rule as the single-component fallback this generalizes."""
    _seed_marcus_with_source(conn)

    response = client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "schema_count": "1",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "",
        },
    )

    assert response.status_code == 200
    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]
    assert page_identities.get_page_number(conn, "s-marcus", "summer_bridge", "Day 5", 1) == 17
    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Day 5'"
    ).fetchone()
    assert row[0] == "manual"


def test_confirm_from_a_discovery_panel_with_an_unchecked_component_omits_it_from_the_schema(
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
            "schema_count": "2",
            "schema_include_0": "1",
            "schema_name_0": "day",
            "schema_label_0": "Day",
            # schema_include_1 omitted -- this candidate was not kept
            "schema_name_1": "worksheet_code",
            "schema_label_1": "Worksheet code",
            "identity_0_0": "Day 5",
            "identity_0_original_0": "Day 5",
        },
    )

    schema = page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge")
    assert [c.component_name for c in schema] == ["day"]


def test_confirm_in_targeted_mode_records_model_source_when_every_component_is_unchanged(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The parent left every component as the model extracted it -- a model
    success for eval purposes, not a manual entry."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 1",
            "identity_section_original_0": "Section 1",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "model"


def test_confirm_in_targeted_mode_records_manual_source_when_any_one_component_is_edited(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model was confidently wrong about the section; the parent corrected
    just that one field. The whole row counts as manual -- confidence does not
    make the model right, and a partly-corrected row is not a model success."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 2",  # corrected
            "identity_section_original_0": "Section 1",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 2\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "manual"


def test_confirm_in_targeted_mode_with_a_missing_component_typed_in_is_manual(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The model found nothing for "section" on this block (empty original); the
    parent filled it in by hand. Also manual -- same rule as the single-component
    fallback this generalizes."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("section", "Section", None), ("day", "Day", None)]
    )

    client.post(
        "/keys/s-marcus/summer_bridge/confirm",
        data={
            "row_count": "1",
            "page_number_0": "17",
            "problem_number_0": "1",
            "answer_text_0": "8 m",
            "identity_section_0": "Section 1",
            "identity_section_original_0": "",
            "identity_day_0": "Day 5",
            "identity_day_original_0": "Day 5",
        },
    )

    row = conn.execute(
        "SELECT source FROM page_identities WHERE composite_key = 'Section 1\x1fDay 5'"
    ).fetchone()
    assert row is not None
    assert row[0] == "manual"


def test_confirm_screen_marks_low_confidence_rows_unconfirmed(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    """A block whose identifier_confidence is below the confidence floor must show
    up on the confirm screen as editable and visibly flagged -- a parent already
    checking every answer should not have to guess which identifiers need a second
    look."""
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
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
    assert 'name="identity_day_0"' in html
    assert 'value="Day 5"' in html
    assert "unconfirmed" in html.lower()


def test_confirm_screen_does_not_flag_confidently_extracted_components(
    client: TestClient, conn: sqlite3.Connection, transcriber: FakeKeyTranscriber
) -> None:
    _seed_marcus_with_source(conn)
    page_identity_schemas.save_new_schema(conn, "s-marcus", "summer_bridge", [("day", "Day", None)])
    transcriber.result = KeyPageResult(
        entries=(
            KeyPageEntry(
                page_number=17,
                identity_values={"day": "Day 5"},
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
    row_html = html.split('name="identity_day_0"')[1].split("</td>")[0]
    assert "unconfirmed" not in row_html.lower()


def test_confirm_with_no_identity_and_no_schema_leaves_no_mapping(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """No schema, no discovery panel submitted -- nothing enters page_identities
    this round, and nothing crashes."""
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
    assert page_identity_schemas.get_current_schema(conn, "s-marcus", "summer_bridge") == ()
    count = conn.execute("SELECT COUNT(*) FROM page_identities").fetchone()[0]
    assert count == 0


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
