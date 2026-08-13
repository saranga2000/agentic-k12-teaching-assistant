"""Orchestrating one capture through ingest -> transcribe -> grade -> persist.

Reuses the request-cap and circuit-breaker machinery already built in k12ta.llm and
k12ta.transcribe exactly as evals/run_transcription_eval.py already does: one call to
`transcriber.transcribe`, no retry loop here. The one new thing this module adds is
the daily quota gate, checked before anything is saved.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from uuid import uuid4

from k12ta.config import Settings
from k12ta.ingest import capture as ingest_capture
from k12ta.store import captures, quota, sessions
from k12ta.transcribe.base import Transcriber

logger = logging.getLogger(__name__)


class PipelineStatus(Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSCRIBE_FAILED = "transcribe_failed"
    GRADED = "graded"


@dataclass(frozen=True)
class PipelineOutcome:
    status: PipelineStatus
    session_id: str | None = None
    failure_reason: str | None = None

    @staticmethod
    def quota_exhausted() -> PipelineOutcome:
        return PipelineOutcome(status=PipelineStatus.QUOTA_EXHAUSTED)

    @staticmethod
    def transcribe_failed(reason: str) -> PipelineOutcome:
        return PipelineOutcome(status=PipelineStatus.TRANSCRIBE_FAILED, failure_reason=reason)

    @staticmethod
    def graded(session_id: str) -> PipelineOutcome:
        return PipelineOutcome(status=PipelineStatus.GRADED, session_id=session_id)


def process_capture(
    conn: sqlite3.Connection,
    settings: Settings,
    get_transcriber: Callable[[], Transcriber],
    student_id: str,
    assignment_id: str,
    image_bytes: bytes,
) -> PipelineOutcome:
    """Walk one accepted photo through transcription, (no-key) grading, and persistence.

    Quota-gated before anything is saved: if today's request count is already at
    `settings.daily_request_limit`, `get_transcriber` is never called and nothing is
    written. It's a factory rather than an already-built `Transcriber` for exactly
    that reason: building one (a live vision-model adapter) has to happen *after* the
    quota gate passes, not before, or a quota-exhausted request would pay the
    construction cost anyway -- and a broken provider config would 500 even requests
    that were never going to call the model at all.

    M2.3 has no answer-key storage anywhere yet (M2.4 builds that), so every
    transcribed item is graded NEEDS_HUMAN unconditionally -- this never calls
    k12ta.grading.key_grader, because there is nothing to grade against.
    """
    today = date.today()
    if quota.get_count(conn, today) >= settings.daily_request_limit:
        logger.info("capture blocked: daily quota exhausted student_id=%s", student_id)
        return PipelineOutcome.quota_exhausted()

    capture_row = ingest_capture.save_capture(
        conn, settings, student_id, assignment_id, image_bytes
    )
    quota.record_request(conn, today)

    try:
        transcriber = get_transcriber()
        result = transcriber.transcribe(capture_row.image_path)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.info(
            "transcribe capture_id=%s student_id=%s outcome=failed reason=%s",
            capture_row.capture_id,
            student_id,
            reason,
        )
        return PipelineOutcome.transcribe_failed(reason)

    logger.info(
        "transcribe capture_id=%s student_id=%s outcome=%s cost_usd=%s latency_ms=%s",
        capture_row.capture_id,
        student_id,
        "failed" if result.failure is not None else "ok",
        result.cost_usd,
        result.latency_ms,
    )

    if result.failure is not None:
        return PipelineOutcome.transcribe_failed(result.failure)

    for item in result.items:
        captures.insert_problem(
            conn,
            captures.ProblemRow(
                student_id=student_id,
                capture_id=capture_row.capture_id,
                problem_id=item.problem_id,
                prompt_text=item.prompt_text,
                student_answer_raw=item.student_answer_raw,
                transcription_confidence=item.confidence,
            ),
        )

    session_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id=student_id,
            session_id=session_id,
            assignment_id=assignment_id,
            started_at=now,
            ended_at=now,
        ),
    )
    for item in result.items:
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id=student_id,
                session_id=session_id,
                capture_id=capture_row.capture_id,
                problem_id=item.problem_id,
                outcome="needs_human",
                grader_confidence=item.confidence,
                expected_answer=None,
            ),
        )

    return PipelineOutcome.graded(session_id)
