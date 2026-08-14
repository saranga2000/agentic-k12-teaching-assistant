"""Key-page orchestration: quota gate -> normalize -> transcribe. No persistence here
(that's the parent's confirm step) and no test hits the network.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image

from k12ta.config import Settings
from k12ta.llm.base import DataRetention
from k12ta.pipeline.key_ingestion import KeyIngestionStatus, transcribe_key_page
from k12ta.store import db, migrate, quota
from k12ta.transcribe.base import FailureKind
from k12ta.transcribe.key_page import KeyPageEntry, KeyPageResult
from tests.fakes import FakeKeyTranscriber

TODAY = date.today()


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _settings(tmp_path: Path, daily_request_limit: int = 20) -> Settings:
    return Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=daily_request_limit,
        log_level="INFO",
    )


def _sideways_portrait_jpeg() -> bytes:
    """A photo shaped exactly like a real camera stores one -- EXIF-rotated -- to
    prove transcribe_key_page normalizes orientation before transcribing."""
    image = Image.new("RGB", (1600, 1200), color=(210, 210, 210))
    exif = image.getexif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _success_result(*page_numbers: int) -> KeyPageResult:
    entries = tuple(
        KeyPageEntry(
            page_number=page_number,
            problem_number="1",
            answer_text="8 m",
            ungradeable_reason=None,
            confidence=0.95,
        )
        for page_number in page_numbers
    )
    return KeyPageResult(
        entries=entries,
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(kind: FailureKind) -> KeyPageResult:
    return KeyPageResult(
        entries=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=200,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=f"simulated {kind.value}",
        failure_kind=kind,
    )


def test_successful_transcribe_returns_entries_and_writes_no_answer_key_rows(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_success_result(17, 18))

    outcome = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())

    assert outcome.status is KeyIngestionStatus.TRANSCRIBED
    assert [e.page_number for e in outcome.entries] == [17, 18]
    assert transcriber.request_count == 1
    assert quota.get_count(conn, TODAY) == 1

    cur = conn.execute("SELECT COUNT(*) FROM answer_key_entries")
    assert cur.fetchone()[0] == 0  # nothing enters the store until the parent confirms


def test_photo_is_normalized_before_being_sent_to_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_success_result(17))

    transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())

    assert len(transcriber.calls) == 1
    sent = Image.open(io.BytesIO(transcriber.calls[0]))
    assert sent.size == (1200, 1600)  # corrected, not the raw 1600x1200 sideways buffer


def test_normalized_bytes_are_returned_for_the_confirm_screens_photo_preview(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_success_result(17))

    outcome = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())

    assert outcome.normalized_image_bytes is not None
    assert Image.open(io.BytesIO(outcome.normalized_image_bytes)).size == (1200, 1600)


def test_transcribe_passes_on_progress_through_to_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_success_result(17), progress_updates=(50, 400))
    seen: list[int] = []

    transcribe_key_page(
        conn, settings, lambda: transcriber, _sideways_portrait_jpeg(), on_progress=seen.append
    )

    assert seen == [50, 400]


def test_transcribe_passes_identity_schema_through_to_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_success_result(17))

    transcribe_key_page(
        conn,
        settings,
        lambda: transcriber,
        _sideways_portrait_jpeg(),
        identity_schema=[("day", "Day 5"), ("section", "Section 1")],
    )

    assert transcriber.identity_schemas_seen == [[("day", "Day 5"), ("section", "Section 1")]]


def test_quota_already_exhausted_never_calls_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path, daily_request_limit=1)
    quota.record_request(conn, TODAY)  # already at the limit
    transcriber = FakeKeyTranscriber(result=_success_result(17))

    outcome = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())

    assert outcome.status is KeyIngestionStatus.QUOTA_EXHAUSTED
    assert outcome.entries == ()
    assert transcriber.calls == []
    assert quota.get_count(conn, TODAY) == 1  # unchanged, not incremented further


def test_transcribe_failure_degrades_gracefully(tmp_path: Path) -> None:
    conn = _migrated_connection()
    settings = _settings(tmp_path)
    transcriber = FakeKeyTranscriber(result=_failure_result(FailureKind.UNREADABLE))

    outcome = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())

    assert outcome.status is KeyIngestionStatus.TRANSCRIBE_FAILED
    assert outcome.entries == ()
    assert quota.get_count(conn, TODAY) == 1  # the attempt still counted


def test_shares_the_capture_pipelines_daily_quota_table(tmp_path: Path) -> None:
    """The actual proof of "reuse the same quota counter as the capture pipeline":
    exhaust it via a plain quota.record_request call (standing in for a student
    capture) and confirm key ingestion sees the same, combined count."""
    conn = _migrated_connection()
    settings = _settings(tmp_path, daily_request_limit=2)
    quota.record_request(conn, TODAY)  # simulates one student capture already today
    transcriber = FakeKeyTranscriber(result=_success_result(17))

    first = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())
    assert first.status is KeyIngestionStatus.TRANSCRIBED
    assert quota.get_count(conn, TODAY) == 2

    second = transcribe_key_page(conn, settings, lambda: transcriber, _sideways_portrait_jpeg())
    assert second.status is KeyIngestionStatus.QUOTA_EXHAUSTED
