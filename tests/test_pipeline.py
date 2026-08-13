"""Pipeline orchestration: capture -> ingest -> transcribe -> grade -> persist.

No test here hits the network — every transcribe call goes through FakeTranscriber.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from k12ta.config import Settings
from k12ta.llm.base import DataRetention
from k12ta.pipeline.process import PipelineStatus, process_capture
from k12ta.store import captures, content, db, migrate, quota, sessions, students
from k12ta.transcribe.base import FailureKind, TranscribedItem, TranscriptionResult
from tests.fakes import FakeTranscriber

TODAY = date(2026, 8, 12)


def _migrated_connection(path: str = ":memory:") -> sqlite3.Connection:
    conn = db.connect(path)
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


def _seed_student_with_source(conn: sqlite3.Connection, student_id: str) -> str:
    """Seed a student, a content source (deliberately without any key content, since
    none exists anywhere yet), and today's assignment. Returns the assignment_id."""
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Jahnvi",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
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
    assignment_id = "summer_bridge:2026-08-12"
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id=student_id,
            assignment_id=assignment_id,
            source_id="summer_bridge",
            created_at=TODAY.isoformat(),
        ),
    )
    return assignment_id


def _success_result(*confidences: float) -> TranscriptionResult:
    items = tuple(
        TranscribedItem(
            problem_id=str(i + 1),
            prompt_text=f"problem {i + 1}",
            student_answer_raw="42",
            confidence=confidence,
        )
        for i, confidence in enumerate(confidences)
    )
    return TranscriptionResult(
        items=items,
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(kind: FailureKind) -> TranscriptionResult:
    return TranscriptionResult(
        items=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=200,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=f"simulated {kind.value}",
        failure_kind=kind,
    )


def test_successful_transcribe_persists_problems_and_needs_human_graded_rows(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_success_result(0.99, 0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    assert outcome.session_id is not None
    assert transcriber.request_count == 1

    session = sessions.get_session(conn, student_id, outcome.session_id)
    assert session is not None
    assert session.assignment_id == assignment_id

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert len(graded) == 2
    assert all(g.outcome == "needs_human" for g in graded)
    assert all(g.expected_answer is None for g in graded)

    quota_count = quota.get_count(conn, TODAY)
    assert quota_count == 1


def test_quota_already_exhausted_never_calls_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path, daily_request_limit=1)
    quota.record_request(conn, TODAY)  # already at the limit
    transcriber = FakeTranscriber(result=_success_result(0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.QUOTA_EXHAUSTED
    assert transcriber.calls == []
    assert quota.get_count(conn, TODAY) == 1  # unchanged, not incremented further

    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", (student_id,))
    assert cur.fetchone()[0] == 0


def test_transcriber_construction_failure_degrades_gracefully(tmp_path: Path) -> None:
    """get_transcriber is a factory precisely so building a live adapter can wait
    until after the quota gate passes -- and a broken provider config (a bad
    K12TA_LLM_PROVIDER, a missing key) must never surface as an unhandled 500 to a
    student who was just trying to submit a photo."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)

    def broken_factory() -> FakeTranscriber:
        raise ValueError("unsupported LLM provider: 'anthropic'")

    outcome = process_capture(
        conn, settings, broken_factory, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.TRANSCRIBE_FAILED
    assert outcome.failure_reason is not None
    assert "unsupported LLM provider" in outcome.failure_reason
    # The photo was still preserved -- construction failing is a transcribe failure,
    # not a quota-exhausted one, and follows the same "preserve the photo" rule.
    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", (student_id,))
    assert cur.fetchone()[0] == 1


def test_transcribe_failure_preserves_the_photo_but_persists_nothing_else(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_failure_result(FailureKind.UNREADABLE))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.TRANSCRIBE_FAILED
    assert outcome.session_id is None
    assert quota.get_count(conn, TODAY) == 1  # the attempt still counted

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", (student_id,))
    row = cur.fetchone()
    assert row is not None  # the photo itself was preserved
    capture_row = captures.get_page_capture(conn, student_id, row[0])
    assert capture_row is not None
    assert Path(capture_row.image_path).exists()

    assert captures.list_problems_for_capture(conn, student_id, row[0]) == []
    cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE student_id = ?", (student_id,))
    assert cur.fetchone()[0] == 0


def test_daily_counter_survives_a_simulated_server_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "pipeline-test.db")
    settings = _settings(tmp_path, daily_request_limit=1)

    first_conn = _migrated_connection(db_path)
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(first_conn, student_id)
    first_transcriber = FakeTranscriber(result=_success_result(0.99))
    first_outcome = process_capture(
        first_conn, settings, lambda: first_transcriber, student_id, assignment_id, b"photo-one"
    )
    assert first_outcome.status is PipelineStatus.GRADED
    first_conn.close()

    # A fresh connection to the same file, simulating a server restart.
    second_conn = _migrated_connection(db_path)
    second_transcriber = FakeTranscriber(result=_success_result(0.99))
    second_outcome = process_capture(
        second_conn, settings, lambda: second_transcriber, student_id, assignment_id, b"photo-two"
    )

    assert second_outcome.status is PipelineStatus.QUOTA_EXHAUSTED
    assert second_transcriber.calls == []
