"""Orchestrating one capture through ingest -> transcribe -> grade -> persist.

Reuses the request-cap and circuit-breaker machinery already built in k12ta.llm and
k12ta.transcribe exactly as evals/run_transcription_eval.py already does: one call to
`transcriber.transcribe`, no retry loop here. The one new thing this module adds is
the daily quota gate, checked before anything is saved.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from uuid import uuid4

from k12ta.config import Settings
from k12ta.domain.models import GradeOutcome
from k12ta.grading import page_identity
from k12ta.grading.key_grader import find_key_entry
from k12ta.grading.needs_human import GradeDecision, NeedsHumanCause, decide
from k12ta.ingest import capture as ingest_capture
from k12ta.store import (
    answer_keys,
    captures,
    content,
    page_identity_resolutions,
    page_identity_schemas,
    quota,
    sessions,
)
from k12ta.transcribe.base import FailureKind, TranscribedItem, Transcriber

logger = logging.getLogger(__name__)


class PipelineStatus(Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSCRIBE_FAILED = "transcribe_failed"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
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
    def rate_limited(reason: str) -> PipelineOutcome:
        """Distinct from transcribe_failed: the provider's own rate limit was
        exhausted (FailureKind.RATE_LIMITED), not a problem with this photo."""
        return PipelineOutcome(status=PipelineStatus.RATE_LIMITED, failure_reason=reason)

    @staticmethod
    def graded(session_id: str) -> PipelineOutcome:
        return PipelineOutcome(status=PipelineStatus.GRADED, session_id=session_id)

    @staticmethod
    def internal_error(reason: str) -> PipelineOutcome:
        """An unanticipated exception escaping capture processing entirely --
        distinct from transcribe_failed (a classified, expected transcription
        problem) because, by definition, nothing here is known about the
        cause. See k12ta.web.app._stream_capture_response's worker wrapper."""
        return PipelineOutcome(status=PipelineStatus.INTERNAL_ERROR, failure_reason=reason)


# Shared with k12ta.respond.render, which strips this prefix back off for display
# -- there is no real printed label to show a student for one of these, so the
# results table shows "?" in the question-number column instead of this string.
AMBIGUOUS_PROBLEM_ID_PREFIX = "_ambiguous_"


def _resolve_storage_problem_ids(
    items: Sequence[TranscribedItem],
) -> tuple[tuple[str, bool], ...]:
    """One (storage_problem_id, is_ambiguous) pair per item in `items`, same
    order. A blank problem_id, or one that repeats across more than one item
    on this same photo, has nothing to safely key a grade to -- there is no
    honest way to tell which printed question it belongs to. Found 2026-08-20
    on real data: two blank-problem_id items on one photo crashed process_
    capture outright, a UNIQUE constraint violation on problems, not merely a
    wrong grade.

    storage_problem_id is a synthesized, per-index placeholder for exactly
    the ambiguous items (never a real printed label, never used for key
    lookup or k12ta.domain.attempts' cross-capture identity) so two ambiguous
    items on one photo can both still be stored and shown to a parent --
    losing nothing -- rather than silently dropped or crashing on the same
    UNIQUE constraint that caught this in the first place. The caller forces
    NEEDS_HUMAN/AMBIGUOUS_PROBLEM_ID for every item this marks ambiguous,
    never decide()."""
    counts = Counter(item.problem_id for item in items)
    resolved = []
    for i, item in enumerate(items):
        ambiguous = not item.problem_id or counts[item.problem_id] > 1
        resolved.append(
            (f"{AMBIGUOUS_PROBLEM_ID_PREFIX}{i}" if ambiguous else item.problem_id, ambiguous)
        )
    return tuple(resolved)


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
    path only), this function loads the source's current identity schema
    (`k12ta.store.page_identity_schemas`), passes it to the transcriber so
    extraction knows which named markers to look for, then calls
    `k12ta.grading.page_identity.resolve` using whatever identity candidates
    `result.page_identity` extracted from this same photo, and records the
    outcome via `k12ta.store.page_identity_resolutions` -- see docs/ROADMAP.md's
    page-identity discussion. A resolution that isn't RESOLVED still falls
    through to the same honest `NeedsHumanCause.UNKNOWN_PAGE` as before, never a
    guess; CONFLICTING and PARTIAL are the two exceptions, refused explicitly as
    `NeedsHumanCause.CONFLICTING_PAGE_MARKERS`/`PARTIAL_PAGE_MARKERS` before
    `decide` ever runs, because `decide` itself never produces either cause.
    Grading itself goes through `k12ta.grading.needs_human.decide`, which is the
    only place that decides an outcome and, when it's NEEDS_HUMAN, why. Do not
    add a fallback here that solves a problem independently when no key entry
    covers it. Keyless grading is M6, explicitly gated on a measured precision
    number before it ships behind a flag -- it does not exist yet, and "no key
    for this page" must never quietly become "the model's best guess instead."
    That guess is exactly the failure this system is built to avoid: a confident
    wrong grade.
    """
    today = date.today()
    if quota.get_count(conn, today) >= settings.daily_request_limit:
        logger.info("capture blocked: daily quota exhausted student_id=%s", student_id)
        return PipelineOutcome.quota_exhausted()

    capture_row = ingest_capture.save_capture(
        conn, settings, student_id, assignment_id, image_bytes
    )
    quota.record_request(conn, today)

    assignment = content.get_assignment(conn, student_id, assignment_id)
    assert assignment is not None, f"assignment {assignment_id} vanished after ingest"
    schema_version = page_identity_schemas.get_current_version(
        conn, student_id, assignment.source_id
    )
    schema = page_identity_schemas.get_current_schema(conn, student_id, assignment.source_id)
    # The union of the current schema's components and the immediately
    # preceding version's -- so one photo can carry markers for both, and
    # page_identity.resolve_with_schema_history's one-version-back fallback
    # (e.g. Summer Bridge's page-number-primary schema falling back to its
    # old Day+Section pair, docs/ROADMAP.md's M3.7) has something to read.
    # Ordinary sources with only one schema version ever saved get exactly
    # today's behaviour: fallback_schema is empty, identity_schema is schema.
    fallback_schema = (
        page_identity_schemas.get_schema_at_version(
            conn, student_id, assignment.source_id, schema_version - 1
        )
        if schema_version > 1
        else ()
    )
    identity_schema = tuple((c.component_name, c.example) for c in (*schema, *fallback_schema))

    try:
        transcriber = get_transcriber()
        result = transcriber.transcribe(capture_row.image_path, identity_schema=identity_schema)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.info(
            "transcribe capture_id=%s student_id=%s outcome=failed reason=%s",
            capture_row.capture_id,
            student_id,
            reason,
        )
        captures.record_transcribe_failure(conn, student_id, capture_row.capture_id, reason)
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
        if result.failure_kind is FailureKind.RATE_LIMITED:
            # Not a transcription problem -- the photo may be perfectly legible,
            # the provider is just out of capacity. Its own outcome, its own
            # persisted column (never transcribe_failure_reason), so a parent-
            # facing message and a diagnostic query can both tell it apart from
            # an ordinary transcribe failure instead of guessing from free text.
            captures.record_rate_limited(conn, student_id, capture_row.capture_id, result.failure)
            return PipelineOutcome.rate_limited(result.failure)
        captures.record_transcribe_failure(conn, student_id, capture_row.capture_id, result.failure)
        return PipelineOutcome.transcribe_failed(result.failure)

    storage_problem_ids = _resolve_storage_problem_ids(result.items)
    for item, (storage_problem_id, _ambiguous) in zip(
        result.items, storage_problem_ids, strict=True
    ):
        captures.insert_problem(
            conn,
            captures.ProblemRow(
                student_id=student_id,
                capture_id=capture_row.capture_id,
                problem_id=storage_problem_id,
                prompt_text=item.prompt_text,
                student_answer_raw=item.student_answer_raw,
                transcription_confidence=item.confidence,
            ),
        )

    now = datetime.now(UTC).isoformat()
    resolved_page_number = page_number
    conflicting_markers = False
    partial_detail: str | None = None
    if page_number is None:
        # Only auto-resolve when the caller didn't already supply a page number --
        # a manual override (tests, the Scope A demo path) always wins and is never
        # second-guessed by this photo's own identity extraction.
        resolution, resolved_schema_version = page_identity.resolve_with_schema_history(
            conn,
            student_id,
            assignment.source_id,
            result.page_identity.candidates,
            result.page_identity.confidence,
        )
        seen_values_json: str | None = None
        if resolution.outcome is page_identity.PageIdentityOutcome.RESOLVED:
            resolved_page_number = resolution.page_number
        elif resolution.outcome is page_identity.PageIdentityOutcome.CONFLICTING:
            # Refused, not guessed: decide() never produces this cause itself (see
            # NeedsHumanCause.CONFLICTING_PAGE_MARKERS's docstring), so every item on
            # this photo is marked needs-human here, before decide() ever runs.
            conflicting_markers = True
        elif resolution.outcome is page_identity.PageIdentityOutcome.PARTIAL:
            # Same carve-out as CONFLICTING, for the same reason -- decide() never
            # produces PARTIAL_PAGE_MARKERS either. Which components were seen and
            # missing is decided here, once, and stored as a fact for the renderer
            # to interpolate, never re-derived from the schema at render time.
            # resolved_schema_version, not "current": PARTIAL can only have come
            # from resolve_with_schema_history's fallback attempt here (a
            # single-component current schema can never itself produce PARTIAL --
            # see resolve()'s NO_MARKERS-before-missing check), so resolve_partial
            # must look at the same older schema resolve() just did.
            partial = page_identity.resolve_partial(
                conn,
                student_id,
                assignment.source_id,
                result.page_identity.candidates,
                schema_version=resolved_schema_version,
            )
            if partial.auto_resolved_page_number is not None:
                # Deduction from what a parent has already confirmed against the
                # physical book, not a guess -- see resolve_partial's docstring
                # and docs/ARCHITECTURE.md's "asking when exactly one component
                # is missing" section. Grades normally from here, same as
                # RESOLVED; the page_identity_resolutions log below still
                # records the true underlying outcome ("partial"), never
                # upgraded to "resolved" -- this was extraction plus deduction,
                # not the composite lookup RESOLVED means on its own.
                resolved_page_number = partial.auto_resolved_page_number
            else:
                partial_detail = json.dumps(
                    {
                        "seen": list(resolution.seen_labels),
                        "missing": list(resolution.missing_labels),
                    }
                )
                if partial.matches:
                    # Something to ask about: persist only what this photo
                    # read, never the candidates themselves, so the pick
                    # screen and a later pick submission both re-derive fresh
                    # matches from page_identities rather than trusting
                    # anything computed here to still be current.
                    seen_values_json = json.dumps(partial.seen_values)
        elif resolution.outcome is page_identity.PageIdentityOutcome.NO_SCHEMA:
            # Gap O (docs/USER_WORKFLOWS.md): nothing to resolve against yet,
            # but whatever this photo's own extraction found is worth keeping
            # -- it's the proposed schema k12ta.web.app offers the child to
            # confirm or correct, the one case where a genuinely brand-new
            # program is worth guessing about rather than an outright refusal
            # (see docs/ARCHITECTURE.md's confidence/escalation philosophy:
            # this doesn't relax it, since no key can exist yet for a page
            # that has no schema at all -- the worst a wrong guess produces
            # here is an honest NO_KEY_FOR_PAGE, never a confident wrong
            # grade). First non-empty value per candidate name, same
            # first-wins reduction k12ta.keys.app._discover_identity_
            # components already applies to a parent's key-scan discovery.
            guessed = {
                name: values[0]
                for name, values in result.page_identity.candidates.items()
                if values
            }
            if guessed:
                seen_values_json = json.dumps(guessed)

        page_identity_resolutions.insert_resolution(
            conn,
            page_identity_resolutions.PageIdentityResolutionRow(
                student_id=student_id,
                source_id=assignment.source_id,
                capture_id=capture_row.capture_id,
                outcome=resolution.outcome.value,
                resolved_page_number=resolution.page_number,
                created_at=now,
                seen_values_json=seen_values_json,
                schema_version=resolved_schema_version,
            ),
        )

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
    for item, (storage_problem_id, ambiguous) in zip(
        result.items, storage_problem_ids, strict=True
    ):
        detail = None
        if ambiguous:
            # Checked first, unconditionally: there is no question to key this
            # answer to at all, which is a more fundamental gap than whether
            # the page itself resolved -- see _resolve_storage_problem_ids.
            decision = GradeDecision(
                outcome=GradeOutcome.NEEDS_HUMAN,
                needs_human_cause=NeedsHumanCause.AMBIGUOUS_PROBLEM_ID,
            )
        elif conflicting_markers:
            decision = GradeDecision(
                outcome=GradeOutcome.NEEDS_HUMAN,
                needs_human_cause=NeedsHumanCause.CONFLICTING_PAGE_MARKERS,
            )
        elif partial_detail is not None:
            decision = GradeDecision(
                outcome=GradeOutcome.NEEDS_HUMAN,
                needs_human_cause=NeedsHumanCause.PARTIAL_PAGE_MARKERS,
            )
            detail = partial_detail
        else:
            key_entry = (
                find_key_entry(
                    answer_keys.get_entries_for_page(
                        conn, student_id, assignment.source_id, resolved_page_number
                    ),
                    item.problem_id,
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
                problem_id=storage_problem_id,
                outcome=decision.outcome.value,
                grader_confidence=item.confidence,
                expected_answer=decision.expected_answer,
                page_number=resolved_page_number,
                needs_human_cause=(
                    decision.needs_human_cause.value
                    if decision.needs_human_cause is not None
                    else None
                ),
                needs_human_detail=detail,
                unsimplified=decision.unsimplified,
                answered=bool(item.student_answer_raw.strip()),
            ),
        )

    return PipelineOutcome.graded(session_id)


def regrade_capture_for_resolved_identity(
    conn: sqlite3.Connection,
    student_id: str,
    session_id: str,
    capture_id: str,
    source_id: str,
    page_number: int,
) -> None:
    """The shared regrade path for a capture whose page identity is now
    known, whether that came from a student's constrained pick
    (k12ta.grading.page_identity.resolve_partial) or a parent adding a key
    for a page that was already resolved but previously had none. Re-decides
    every problem transcribed from this capture using the transcription
    already stored in the problems table -- never re-transcribes, never
    spends quota, never calls a model. Each graded_problems row is updated
    in place (k12ta.store.sessions.update_graded_problem_after_identity_
    resolution), not replaced with a new one, which is what keeps this
    problem's attempt-ordering correct: k12ta.domain.attempts counts from
    page_captures.captured_at, untouched by this function, so a problem
    regraded long after capture is still counted at its original position,
    not as a new attempt happening now.

    Resolving identity is not the same as having a key for the resolved
    page -- decide() can still land on NEEDS_HUMAN (most likely
    NO_KEY_FOR_PAGE) rather than a definite grade, and that is exactly as
    honest here as it is at capture time."""
    for problem in captures.list_problems_for_capture(conn, student_id, capture_id):
        key_entry = find_key_entry(
            answer_keys.get_entries_for_page(conn, student_id, source_id, page_number),
            problem.problem_id,
        )
        decision = decide(
            problem.student_answer_raw, problem.transcription_confidence, page_number, key_entry
        )
        sessions.update_graded_problem_after_identity_resolution(
            conn,
            student_id=student_id,
            session_id=session_id,
            capture_id=capture_id,
            problem_id=problem.problem_id,
            outcome=decision.outcome.value,
            expected_answer=decision.expected_answer,
            page_number=page_number,
            needs_human_cause=(
                decision.needs_human_cause.value if decision.needs_human_cause is not None else None
            ),
            unsimplified=decision.unsimplified,
        )


@dataclass(frozen=True)
class ReplaySummary:
    source_id: str
    captures_replayed: int


def replay_source(conn: sqlite3.Connection, student_id: str, source_id: str) -> ReplaySummary:
    """Re-decide every already-resolved capture for one source against the
    answer key and grading logic as they stand *right now* -- zero model
    calls, since it only ever calls regrade_capture_for_resolved_identity
    above, which itself never re-transcribes. Turns a one-time batch of real
    photographs (which does cost quota, at ingest) into a permanent, free
    regression corpus: re-run this after any key correction or decide()
    change to see its effect on every real capture on file in seconds,
    instead of re-photographing and spending quota again.

    Iterates k12ta.store.sessions.list_resolved_captures_for_source, which
    only returns captures whose page identity already resolved -- a capture
    still sitting on NO_SCHEMA/NO_MARKERS/UNKNOWN_PAGE etc. has no page_number
    to regrade against and is silently skipped, exactly as honest as at
    capture time. Nothing here touches page-identity resolution itself; only
    a real photo re-read by the model can change what page a capture
    resolves to."""
    resolved = sessions.list_resolved_captures_for_source(conn, student_id, source_id)
    for row in resolved:
        regrade_capture_for_resolved_identity(
            conn, student_id, row.session_id, row.capture_id, source_id, row.page_number
        )
    return ReplaySummary(source_id=source_id, captures_replayed=len(resolved))
