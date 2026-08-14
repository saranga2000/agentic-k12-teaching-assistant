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
from k12ta.domain.models import GradeOutcome
from k12ta.grading import page_identity
from k12ta.grading.needs_human import GradeDecision, NeedsHumanCause, decide
from k12ta.ingest import capture as ingest_capture
from k12ta.store import (
    answer_keys,
    captures,
    content,
    page_identity_resolutions,
    quota,
    sessions,
)
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
    page_number: int | None = None,
) -> PipelineOutcome:
    """Walk one accepted photo through transcription, key grading, and persistence.

    Quota-gated before anything is saved: if today's request count is already at
    `settings.daily_request_limit`, `get_transcriber` is never called and nothing is
    written. It's a factory rather than an already-built `Transcriber` for exactly
    that reason: building one (a live vision-model adapter) has to happen *after* the
    quota gate passes, not before, or a quota-exhausted request would pay the
    construction cost anyway -- and a broken provider config would 500 even requests
    that were never going to call the model at all.

    `page_number` is the only thing that lets an item be looked up against a
    confirmed answer key at all. It is optional and, when omitted (the normal
    capture path -- callers with a manual override are tests and the Scope A demo
    path only), this function calls `k12ta.grading.page_identity.resolve` itself
    using whatever identity markers `result.page_identity` extracted from this same
    photo, and records the outcome via `k12ta.store.page_identity_resolutions` --
    see docs/ROADMAP.md's page-identity discussion. A resolution that isn't
    RESOLVED still falls through to the same honest `NeedsHumanCause.UNKNOWN_PAGE`
    as before, never a guess; CONFLICTING is the one exception, refused explicitly
    as `NeedsHumanCause.CONFLICTING_PAGE_MARKERS` before `decide` ever runs, because
    `decide` itself never produces that cause. Grading itself goes through
    `k12ta.grading.needs_human.decide`, which is the only place that decides an
    outcome and, when it's NEEDS_HUMAN, why. Do not add a fallback here that solves
    a problem independently when no key entry covers it. Keyless grading is M6,
    explicitly gated on a measured precision number before it ships behind a flag --
    it does not exist yet, and "no key for this page" must never quietly become
    "the model's best guess instead." That guess is exactly the failure this system
    is built to avoid: a confident wrong grade.
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

    assignment = content.get_assignment(conn, student_id, assignment_id)
    assert assignment is not None, f"assignment {assignment_id} vanished after ingest"

    now = datetime.now(UTC).isoformat()
    resolved_page_number = page_number
    conflicting_markers = False
    if page_number is None:
        # Only auto-resolve when the caller didn't already supply a page number --
        # a manual override (tests, the Scope A demo path) always wins and is never
        # second-guessed by this photo's own identity extraction.
        source = content.get_content_source(conn, student_id, assignment.source_id)
        resolution = page_identity.resolve(
            conn,
            student_id,
            assignment.source_id,
            source.page_identity_kind if source is not None else None,
            result.page_identity.candidates,
            result.page_identity.confidence,
        )
        page_identity_resolutions.insert_resolution(
            conn,
            page_identity_resolutions.PageIdentityResolutionRow(
                student_id=student_id,
                source_id=assignment.source_id,
                capture_id=capture_row.capture_id,
                outcome=resolution.outcome.value,
                resolved_page_number=resolution.page_number,
                created_at=now,
            ),
        )
        if resolution.outcome is page_identity.PageIdentityOutcome.RESOLVED:
            resolved_page_number = resolution.page_number
        elif resolution.outcome is page_identity.PageIdentityOutcome.CONFLICTING:
            # Refused, not guessed: decide() never produces this cause itself (see
            # NeedsHumanCause.CONFLICTING_PAGE_MARKERS's docstring), so every item on
            # this photo is marked needs-human here, before decide() ever runs.
            conflicting_markers = True

    session_id = str(uuid4())
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
        if conflicting_markers:
            decision = GradeDecision(
                outcome=GradeOutcome.NEEDS_HUMAN,
                needs_human_cause=NeedsHumanCause.CONFLICTING_PAGE_MARKERS,
            )
        else:
            key_entry = (
                answer_keys.get_entry(
                    conn, student_id, assignment.source_id, resolved_page_number, item.problem_id
                )
                if resolved_page_number is not None
                else None
            )
            decision = decide(
                item.student_answer_raw, item.confidence, resolved_page_number, key_entry
            )
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id=student_id,
                session_id=session_id,
                capture_id=capture_row.capture_id,
                problem_id=item.problem_id,
                outcome=decision.outcome.value,
                grader_confidence=item.confidence,
                expected_answer=decision.expected_answer,
                needs_human_cause=(
                    decision.needs_human_cause.value
                    if decision.needs_human_cause is not None
                    else None
                ),
            ),
        )

    return PipelineOutcome.graded(session_id)
