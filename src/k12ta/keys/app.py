"""Routes for the parent-only answer-key ingestion app.

Upload -> transcribe -> confirm is one stateless request/response cycle: a key photo
is never written to disk, and nothing enters `answer_key_entries` before the parent's
confirm POST. HTTP and templates only, per docs/ARCHITECTURE.md -- the quota gate,
orientation fix, and transcription live in `k12ta.pipeline.key_ingestion`.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from k12ta.config import COACH_NAME_PLACEHOLDER, Settings, load_dotenv
from k12ta.domain.attempts import PastAttempt, attempt_number
from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for
from k12ta.domain.text import humanize_math_text
from k12ta.grading import page_identity
from k12ta.grading.key_grader import CONFIDENCE_FLOOR, find_key_entry
from k12ta.grading.page_identity import build_composite_key
from k12ta.llm import build_vision_model
from k12ta.pipeline.key_ingestion import (
    KeyIngestionOutcome,
    KeyIngestionStatus,
    discover_identity_from_example_page,
    save_key_page_image,
    transcribe_key_page,
)
from k12ta.pipeline.process import regrade_capture_for_resolved_identity, replay_source
from k12ta.store import (
    answer_key_audit,
    answer_keys,
    capture_duplicates,
    captures,
    content,
    db,
    disputes,
    identity_corrections,
    key_page_images,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    policy_override_audit,
    policy_overrides,
    program_requests,
    sessions,
    students,
    verdict_correction_audit,
)
from k12ta.transcribe.base import Transcriber
from k12ta.transcribe.key_page import KeyPageEntry, KeyTranscriber, VisionLLMKeyTranscriber
from k12ta.transcribe.vision_llm import VisionLLMTranscriber

QUOTA_EXHAUSTED_MESSAGE = (
    "Today's reading budget is used up. Try again tomorrow, or raise K12TA_DAILY_REQUEST_LIMIT."
)
NO_STUDENTS_MESSAGE = (
    "No students yet. Run `python scripts/seed_dev_data.py` against this server's K12TA_DATA_DIR."
)
UNGRADEABLE_REASONS = ("answers_vary", "graph_or_table")

# Plain-language options for the enrollment setup screen (M3.1), not the internal
# enum values (k12ta.content.source.SourceKind) a parent has no reason to know.
# "generated" is deliberately absent: per its own docstring it's "produced by the
# coach itself," never something a parent sets up by hand.
SOURCE_KIND_LABELS: dict[str, str] = {
    "workbook": "Workbook",
    "worksheet_packet": "Worksheet packet",
    "textbook": "Textbook",
    "fluency_drill": "Fluency drill",
    "online_exercise": "Online exercise (a screenshot, not a printed page)",
}
# Same reasoning, for k12ta.domain.policy.FeedbackMode.
FEEDBACK_MODE_LABELS: dict[str, str] = {
    "full": "Full teaching (self-directed practice, worked solutions allowed)",
    "diagnostic_only": "Diagnostic only (someone else grades this)",
    "fluency": "Timed fluency drill",
}

load_dotenv()  # must run before any Settings.from_env() call in this module
logging.basicConfig(
    level=Settings.from_env().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

app = FastAPI()
# M9a (docs/ROADMAP.md): same shared design-system directory k12ta.web mounts --
# see that app's own comment on this line for why (one physical file, no new
# dependency).
app.mount(
    "/static", StaticFiles(directory=str(Path(__file__).parent.parent / "design")), name="static"
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["humanize_math"] = humanize_math_text

_transcriber: KeyTranscriber | None = None
_page_transcriber: Transcriber | None = None


def get_settings() -> Settings:
    return Settings.from_env()


def get_conn(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(settings.data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def get_transcriber(settings: Settings) -> KeyTranscriber:
    """One transcriber instance, reused across every request for the life of the
    process -- same shape as k12ta.web.app's, same reason: a fresh instance per
    request would reset request_count and defeat the per-run cap in
    k12ta.llm.gemini. Deliberately not a FastAPI dependency: k12ta.pipeline calls
    this only after its quota gate passes."""
    global _transcriber
    if _transcriber is None:
        vision_model = build_vision_model(settings)
        _transcriber = VisionLLMKeyTranscriber(
            vision_model, provider=settings.llm_provider, model=settings.llm_model
        )
    return _transcriber


def get_page_transcriber(settings: Settings) -> Transcriber:
    """Gap I (docs/USER_WORKFLOWS.md): the same student-side `Transcriber`
    k12ta.web.app uses, for the optional "also upload an example exercise
    page" discovery bonus in submit_upload -- a different provider adapter
    from get_transcriber above (that one reads answers off a key page; this
    one reads a plain exercise page the way a student capture would). Same
    reuse-one-instance-per-process reasoning as get_transcriber."""
    global _page_transcriber
    if _page_transcriber is None:
        vision_model = build_vision_model(settings)
        _page_transcriber = VisionLLMTranscriber(
            vision_model, provider=settings.llm_provider, model=settings.llm_model
        )
    return _page_transcriber


def _get(data: dict[str, list[str]], key: str, default: str = "") -> str:
    return data.get(key, [default])[0]


_LEADING_DIGITS = re.compile(r"\d+")


def _problem_number_sort_key(problem_number: str) -> tuple[int, str]:
    """Numeric-aware: "10" sorts after "2", not before it lexicographically, and
    "2a" sorts right after "2" (same leading number, suffix breaks the tie)."""
    match = _LEADING_DIGITS.match(problem_number)
    if match is None:
        return (0, problem_number)
    return (int(match.group()), problem_number[match.end() :])


def _sorted_for_confirm(entries: tuple[KeyPageEntry, ...]) -> tuple[KeyPageEntry, ...]:
    """Ascending by page, then problem number, before a parent ever sees them. The
    model has no obligation to emit entries in page/problem order -- multiple "Day
    N/Page NN" blocks on one photo, or a leading block inferred back to the
    previous day (prompts/transcribe_key_page.md), routinely arrive out of order.
    A parent checking against a printed key wants the same order the key prints."""
    return tuple(
        sorted(
            entries,
            key=lambda e: (e.page_number, _problem_number_sort_key(e.problem_number)),
        )
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Children first, then their enrollments -- not "scan an answer key" as the
    top-level action. See docs/ROADMAP.md, "Parent surface: information
    architecture": this is what a parent actually opens the app for, once daily
    progress (M5) exists to put here; until then it's the enrollment list."""
    rows = [
        _HomeRow(
            student=student,
            sources=(sources := content.list_content_sources(conn, student.student_id)),
            program_requested_at=(
                program_requests.get_requested_at(conn, student.student_id) if not sources else None
            ),
        )
        for student in students.list_students(conn)
    ]
    # Gap G (docs/USER_WORKFLOWS.md): a cross-child, cross-program review
    # queue, before drilling into any one enrollment -- pure aggregation of
    # sessions.list_pending_for_source, which already exists per enrollment
    # and was never rolled up. No mastery model needed, unlike Gap F.
    review_queue = sorted(
        (
            _ReviewQueueItem(student=row.student, source=source, pending_count=pending_count)
            for row in rows
            for source in row.sources
            if (
                pending_count := len(
                    sessions.list_pending_for_source(conn, row.student.student_id, source.source_id)
                )
            )
            > 0
        ),
        key=lambda item: item.pending_count,
        reverse=True,
    )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "rows": rows,
            "no_students_message": NO_STUDENTS_MESSAGE,
            "review_queue": review_queue,
        },
    )


@dataclass(frozen=True)
class _HomeRow:
    student: students.StudentRow
    sources: list[content.ContentSourceRow]
    program_requested_at: str | None


@dataclass(frozen=True)
class _ReviewQueueItem:
    """Gap G: one enrollment with at least one pending item, for the
    landing page's cross-child rollup. Deliberately just a count, not the
    full CaptureGroup breakdown evaluations_screen builds -- a landing page
    only needs "go look here," not the detail that screen already owns."""

    student: students.StudentRow
    source: content.ContentSourceRow
    pending_count: int


@dataclass
class _StudentFormInput:
    """Gap E (docs/USER_WORKFLOWS.md): registering a child only ever happened
    by hand-editing the database via scripts/seed_dev_data.py -- same shape
    as _EnrollmentFormInput above, both the blank starting state and, on a
    validation error, exactly what the parent typed. state_code and
    coach_name are not asked here: neither drives any decision anywhere in
    this codebase today (k12ta.config's COACH_NAME_PLACEHOLDER is what the
    student actually sees until she names her own coach), so asking a parent
    to fill in two fields nothing reads yet would be friction with no
    payoff."""

    display_name: str = ""
    grade_level_raw: str = ""
    grade_level: int | None = None


def _parse_student_setup_form(
    data: dict[str, list[str]],
) -> tuple[_StudentFormInput, list[str]]:
    grade_level_raw = _get(data, "grade_level").strip()
    grade_level = int(grade_level_raw) if grade_level_raw.isdigit() else None
    values = _StudentFormInput(
        display_name=_get(data, "display_name").strip(),
        grade_level_raw=grade_level_raw,
        grade_level=grade_level,
    )
    errors = []
    if not values.display_name:
        errors.append("Name is required.")
    if grade_level is None or not 0 <= grade_level <= 12:
        errors.append("Grade must be a number from 0 (kindergarten) to 12.")
    return values, errors


def _normalize_student_id(raw: str) -> str:
    """Same shape as _normalize_source_id -- a parent never types or sees a
    student_id at all; it's derived from the name they did type."""
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def _unique_student_id(conn: sqlite3.Connection, base: str) -> str:
    """`base` with a numeric suffix appended only if it collides with an
    existing student -- two children sharing a first name must never surface
    a database error. Same reasoning as _unique_source_id."""
    candidate = base or "student"
    suffix = 1
    while students.get_student(conn, candidate) is not None:
        suffix += 1
        candidate = f"{base or 'student'}_{suffix}"
    return candidate


@app.get("/students/new", response_class=HTMLResponse)
def student_setup_screen(request: Request) -> HTMLResponse:
    """Gap E: the only intended way a student is created going forward --
    scripts/seed_dev_data.py stays for dev fixtures only, same relationship
    enrollment_setup_screen already has with hand-editing content sources."""
    return templates.TemplateResponse(
        request,
        "student_setup.html",
        {"values": _StudentFormInput(), "errors": []},
    )


@app.post("/students/new", response_model=None)
async def submit_student_setup(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    data = parse_qs((await request.body()).decode())
    values, errors = _parse_student_setup_form(data)
    if errors:
        return templates.TemplateResponse(
            request,
            "student_setup.html",
            {"values": values, "errors": errors},
        )
    assert values.grade_level is not None  # guaranteed by the empty-errors check above

    student_id = _unique_student_id(conn, _normalize_student_id(values.display_name))
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name=values.display_name,
            grade_level=values.grade_level,
            state_code="",
            coach_name=COACH_NAME_PLACEHOLDER,
        ),
    )
    return RedirectResponse("/", status_code=303)


def _require_student(conn: sqlite3.Connection, student_id: str) -> students.StudentRow:
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    return student


def _require_student_and_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> tuple[students.StudentRow, content.ContentSourceRow]:
    student = _require_student(conn, student_id)
    source = content.get_content_source(conn, student_id, source_id)
    if source is None:
        raise HTTPException(404, "no such content source")
    return student, source


@dataclass
class _EnrollmentFormInput:
    """One round trip through the enrollment setup form -- both the blank
    starting state and, on a validation error, exactly what the parent typed,
    handed back so nothing has to be retyped."""

    label: str = ""
    kind: str = ""
    subject: str = ""
    default_mode: str = ""
    minutes_raw: str = ""
    minutes: int | None = None
    has_answer_key: bool = False
    graded_by_someone_else: bool = False


def _parse_enrollment_setup_form(
    data: dict[str, list[str]],
) -> tuple[_EnrollmentFormInput, list[str]]:
    """Ordinary "a parent might mistype this" validation -- every failure here
    re-renders the form with what was typed preserved, never a bare 400 (that
    stays reserved for a request only a bug or a tampered client could send,
    e.g. against a student that doesn't exist)."""
    minutes_raw = _get(data, "typical_session_minutes").strip()
    minutes = int(minutes_raw) if minutes_raw.isdigit() else None
    values = _EnrollmentFormInput(
        label=_get(data, "label").strip(),
        kind=_get(data, "kind").strip(),
        subject=_get(data, "subject").strip(),
        default_mode=_get(data, "default_mode").strip(),
        minutes_raw=minutes_raw,
        minutes=minutes,
        has_answer_key=_get(data, "has_answer_key") == "1",
        graded_by_someone_else=_get(data, "graded_by_someone_else") == "1",
    )
    errors = []
    if not values.label:
        errors.append("Label is required.")
    if values.kind not in SOURCE_KIND_LABELS:
        errors.append("Choose what kind of material this is.")
    if not values.subject:
        errors.append("Subject is required.")
    if values.default_mode not in FEEDBACK_MODE_LABELS:
        errors.append("Choose a feedback mode.")
    if minutes is None or minutes <= 0:
        errors.append("Typical session length must be a positive number of minutes.")
    return values, errors


def _normalize_source_id(raw: str) -> str:
    """Same shape as `_normalize_component_name` below -- a parent never types or
    sees a source_id at all; it's derived from the label they did type."""
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def _unique_source_id(conn: sqlite3.Connection, student_id: str, base: str) -> str:
    """`base` with a numeric suffix appended only if it collides with a source
    this student already has -- a parent picking the same label twice (a
    correction, a duplicate attempt) must never surface a database error."""
    candidate = base or "source"
    suffix = 1
    while content.get_content_source(conn, student_id, candidate) is not None:
        suffix += 1
        candidate = f"{base or 'source'}_{suffix}"
    return candidate


@app.get("/keys/{student_id}/enrollments/new", response_class=HTMLResponse)
def enrollment_setup_screen(
    request: Request,
    student_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """M3.1: the only intended way a content source is created -- never by
    hand-editing the database (see `scripts/seed_dev_data.py`'s own docstring,
    which names this exact screen as its replacement)."""
    student = _require_student(conn, student_id)
    return templates.TemplateResponse(
        request,
        "enrollment_setup.html",
        {
            "student": student,
            "kind_labels": SOURCE_KIND_LABELS,
            "mode_labels": FEEDBACK_MODE_LABELS,
            "values": _EnrollmentFormInput(),
            "errors": [],
        },
    )


@app.post("/keys/{student_id}/enrollments/new", response_model=None)
async def submit_enrollment_setup(
    request: Request,
    student_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    student = _require_student(conn, student_id)
    data = parse_qs((await request.body()).decode())
    values, errors = _parse_enrollment_setup_form(data)
    if errors:
        return templates.TemplateResponse(
            request,
            "enrollment_setup.html",
            {
                "student": student,
                "kind_labels": SOURCE_KIND_LABELS,
                "mode_labels": FEEDBACK_MODE_LABELS,
                "values": values,
                "errors": errors,
            },
        )
    assert values.minutes is not None  # guaranteed by the empty-errors check above

    source_id = _unique_source_id(conn, student_id, _normalize_source_id(values.label))
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
            source_id=source_id,
            label=values.label,
            kind=values.kind,
            subject=values.subject,
            has_answer_key=values.has_answer_key,
            graded_by_someone_else=values.graded_by_someone_else,
            default_mode=values.default_mode,
            typical_session_minutes=values.minutes,
        ),
    )
    # Gap H (docs/USER_WORKFLOWS.md): straight into describing this source's
    # page structure, not the enrollment landing page -- one continuous flow
    # instead of a separately-linked, easily-skipped-indefinitely step.
    # identity_schema_screen's own back-link is the skip: a parent who wants
    # to describe structure later just navigates away without submitting.
    return RedirectResponse(f"/keys/{student_id}/{source_id}/identity-schema", status_code=303)


def _group_by_problem(
    rows: list[sessions.GradedAttemptRow],
) -> dict[tuple[int, str], list[sessions.GradedAttemptRow]]:
    """Groups without reordering -- rows must already be chronological per
    identity, as list_graded_attempts_for_source returns them. Plain data
    plumbing, not a repository concern, so it lives at the call site rather
    than in k12ta.store (see tests/test_store_scoping.py)."""
    grouped: dict[tuple[int, str], list[sessions.GradedAttemptRow]] = {}
    for row in rows:
        grouped.setdefault((row.page_number, row.problem_id), []).append(row)
    return grouped


_WAITING_ON_KEY_CAUSE = "no_key_for_page"
_WAITING_ON_IDENTITY_CAUSES = frozenset(
    {"unknown_page", "partial_page_markers", "conflicting_page_markers"}
)
_WAITING_ON_TRANSCRIPTION_CAUSE = "low_confidence"
_NEEDS_PERSON_CAUSE = "needs_person"
_ANSWER_DIFFERS_CAUSE = "answer_differs_from_key"
_AMBIGUOUS_PROBLEM_ID_CAUSE = "ambiguous_problem_id"

_CAUSE_LABELS: dict[str, str] = {
    _WAITING_ON_KEY_CAUSE: "Waiting on an answer key",
    "unknown_page": "Waiting on page identity",
    "partial_page_markers": "Waiting on page identity",
    "conflicting_page_markers": "Waiting on page identity",
    _WAITING_ON_TRANSCRIPTION_CAUSE: "Transcription could not be read",
    _NEEDS_PERSON_CAUSE: "Needs a person to judge",
    _ANSWER_DIFFERS_CAUSE: "Answer differs from the key",
    _AMBIGUOUS_PROBLEM_ID_CAUSE: "Question number not identified",
}
_UNKNOWN_CAUSE_LABEL = "Needs a look"
"""A legacy row with no cause at all (predates the needs_human_cause column)
-- genuinely unknown, not a guess dressed up as a specific one."""


@dataclass(frozen=True)
class PendingItemDisplay:
    """One pending problem within a CaptureGroup, its cause spelled out as a
    label rather than left for a parent to infer from a section heading --
    "group by capture, not flat by cause" (2026-08-22) still needs the cause
    said somewhere, it just stops being the thing rows are sorted into."""

    row: sessions.PendingProblemRow
    cause_label: str


@dataclass(frozen=True)
class CaptureGroup:
    """One photograph, its image, its still-pending items beneath it -- the
    parent-facing pending list's real unit of display (2026-08-22, replacing
    the flat-by-cause list). `earlier_attempts` folds in every capture this
    one's resolved page_number superseded, per `_group_pending_by_capture`'s
    tiebreak; `has_key_image` is a plain existence check against
    `k12ta.store.key_page_images`, real only for a key confirmed after that
    table started being written to."""

    capture_id: str
    session_id: str
    """Every graded_problems row a capture ever produces shares one session_id
    (k12ta.pipeline.process mints exactly one per capture) -- carried here so
    the ask-and-confirm flow's commit step (regrade_capture_for_resolved_
    identity) has it without a second query."""
    page_number: int | None
    captured_at: str
    items: tuple[PendingItemDisplay, ...]
    earlier_attempts: int
    has_key_image: bool


def _pick_capture_for_page(
    conn: sqlite3.Connection,
    student_id: str,
    capture_ids: Sequence[str],
    captured_at_by_capture: dict[str, str],
) -> str:
    """Which of several captures resolving to the same page represents it on
    screen. Prefers the most recent capture with a real (correct/incorrect)
    verdict *anywhere* among its own items -- checked across the capture's
    whole graded_problems, not just what's still pending, since a capture
    with a clean verdict on some items has nothing pending for those at all.
    Falls back to plain most-recent only when none of the candidates ever
    produced one. Recency alone is not a proxy for quality (found 2026-08-22:
    the newest of three page-15 captures had the worst transcription of the
    three) -- but a real verdict is a genuine quality signal recency isn't."""
    with_verdict = [
        c for c in capture_ids if sessions.capture_has_decisive_outcome(conn, student_id, c)
    ]
    pool = with_verdict if with_verdict else capture_ids
    return max(pool, key=lambda c: captured_at_by_capture[c])


_NEEDS_REVIEW_CAUSES = frozenset(
    {_NEEDS_PERSON_CAUSE, _ANSWER_DIFFERS_CAUSE, _AMBIGUOUS_PROBLEM_ID_CAUSE}
)


@dataclass(frozen=True)
class EnrollmentSummary:
    """The parent enrollment page's two-second read (2026-08-22 M3.9): a
    count for each of five states, and, for the three that live inside
    "Pending review," which capture-group is the first to contain a
    matching item -- that section is grouped by capture, not by cause
    (M3.8), so there is no separate cause-labelled section left to link to;
    the summary jumps to the first block that has one instead. `None` when
    a state has nothing pending -- the template renders that count as plain
    text, never a dead link."""

    needs_review_count: int
    waiting_on_identity_count: int
    waiting_on_key_count: int
    correct_count: int
    partially_correct_count: int
    incorrect_count: int
    first_needs_review_capture_id: str | None
    first_waiting_on_identity_capture_id: str | None
    first_waiting_on_key_capture_id: str | None


def _summarize_enrollment(
    pending: Sequence[sessions.PendingProblemRow],
    capture_groups: Sequence[CaptureGroup],
    resolved: Sequence[sessions.ResolvedProblemRow],
) -> EnrollmentSummary:
    first_needs_review: str | None = None
    first_identity: str | None = None
    first_key: str | None = None
    for group in capture_groups:
        causes = {item.row.needs_human_cause for item in group.items}
        if first_needs_review is None and causes & _NEEDS_REVIEW_CAUSES:
            first_needs_review = group.capture_id
        if first_identity is None and causes & _WAITING_ON_IDENTITY_CAUSES:
            first_identity = group.capture_id
        if first_key is None and _WAITING_ON_KEY_CAUSE in causes:
            first_key = group.capture_id

    return EnrollmentSummary(
        needs_review_count=sum(
            1 for row in pending if row.needs_human_cause in _NEEDS_REVIEW_CAUSES
        ),
        waiting_on_identity_count=sum(
            1 for row in pending if row.needs_human_cause in _WAITING_ON_IDENTITY_CAUSES
        ),
        waiting_on_key_count=sum(
            1 for row in pending if row.needs_human_cause == _WAITING_ON_KEY_CAUSE
        ),
        correct_count=sum(1 for row in resolved if row.outcome == "correct"),
        partially_correct_count=sum(1 for row in resolved if row.outcome == "partially_correct"),
        incorrect_count=sum(1 for row in resolved if row.outcome == "incorrect"),
        first_needs_review_capture_id=first_needs_review,
        first_waiting_on_identity_capture_id=first_identity,
        first_waiting_on_key_capture_id=first_key,
    )


def _resolve_duplicate_root(capture_id: str, duplicate_of: dict[str, str]) -> str:
    """Follows capture_duplicates' one-hop mapping to its end -- a parent
    marking B a duplicate of A, then later C a duplicate of B, means C's
    items belong with A's group, not their own separate one. Stops the
    moment following the chain would revisit a capture already seen, so a
    cycle (A duplicate of B, B duplicate of A) can never loop forever; it
    just returns wherever the walk got to."""
    seen = {capture_id}
    current = capture_id
    while current in duplicate_of and duplicate_of[current] not in seen:
        current = duplicate_of[current]
        seen.add(current)
    return current


def _group_pending_by_capture(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    pending: Sequence[sessions.PendingProblemRow],
) -> tuple[list[CaptureGroup], int]:
    """Display-layer only -- deletes and regrades nothing;
    `k12ta.store.sessions.list_pending_for_source` itself, and
    `submit_regrade_pending`'s actual regrading, see every row underneath
    regardless of what this collapses on screen.

    Groups by capture first (2026-08-22: "group by capture, not flat by
    cause" -- one photograph, its image, its items beneath), then, among
    captures sharing the same *resolved* page_number, keeps only one per
    page via `_pick_capture_for_page`, folding the rest into that
    survivor's `earlier_attempts` count. A capture with no resolved page_number
    yet is never grouped against another -- there's nothing to dedupe until
    it resolves, every such capture is its own group.

    Returns (groups in capture order, how many problems are now gradable
    because a key has since been added for their page -- unchanged from the
    old _bucket_pending, just no longer nested under a "waiting on a key"
    bucket key)."""
    by_capture: dict[str, list[sessions.PendingProblemRow]] = {}
    for row in pending:
        by_capture.setdefault(row.capture_id, []).append(row)

    now_gradable_captures: set[str] = set()
    for capture_id, rows in by_capture.items():
        for row in rows:
            if row.needs_human_cause == _WAITING_ON_KEY_CAUSE and row.page_number is not None:
                key_entry = find_key_entry(
                    answer_keys.get_entries_for_page(conn, student_id, source_id, row.page_number),
                    row.problem_id,
                )
                if key_entry is not None:
                    now_gradable_captures.add(capture_id)

    captured_at_by_capture = {
        capture_id: rows[0].captured_at for capture_id, rows in by_capture.items()
    }

    # Manual duplicates (2026-08-22 M3.9) -- the fallback for unresolved
    # captures, which have no page_number to auto-dedupe by at all. Only
    # applied among still-unresolved captures (a resolved page's own dedup,
    # above/below, is automatic and never needs a parent's say-so).
    # _resolve_duplicate_root follows a chain and guards against a cycle;
    # a target that isn't itself in this source's pending set (already
    # cleared, or never here) is treated as no mark at all -- the marked
    # capture keeps its own group rather than vanishing.
    duplicate_of = capture_duplicates.get_duplicate_map(conn, student_id)
    manual_extra_attempts: dict[str, int] = {}
    folded_into_duplicate: set[str] = set()
    for capture_id, rows in by_capture.items():
        if rows[0].page_number is not None:
            continue
        root = _resolve_duplicate_root(capture_id, duplicate_of)
        if root != capture_id and root in by_capture and by_capture[root][0].page_number is None:
            manual_extra_attempts[root] = manual_extra_attempts.get(root, 0) + 1
            folded_into_duplicate.add(capture_id)

    by_page: dict[int, list[str]] = {}
    surviving_capture_ids: dict[str, int] = {}
    for capture_id, rows in by_capture.items():
        if capture_id in folded_into_duplicate:
            continue
        page_number = rows[0].page_number  # every row from one capture shares it
        if page_number is None:
            surviving_capture_ids[capture_id] = manual_extra_attempts.get(capture_id, 0)
        else:
            by_page.setdefault(page_number, []).append(capture_id)
    for capture_ids in by_page.values():
        chosen = _pick_capture_for_page(conn, student_id, capture_ids, captured_at_by_capture)
        surviving_capture_ids[chosen] = len(capture_ids) - 1

    key_image_pages: dict[int, bool] = {}
    groups: list[CaptureGroup] = []
    for capture_id, earlier_attempts in surviving_capture_ids.items():
        rows = by_capture[capture_id]
        page_number = rows[0].page_number
        if page_number is not None and page_number not in key_image_pages:
            key_image_pages[page_number] = (
                key_page_images.get_image_path(conn, student_id, source_id, page_number) is not None
            )
        groups.append(
            CaptureGroup(
                capture_id=capture_id,
                session_id=rows[0].session_id,
                page_number=page_number,
                captured_at=rows[0].captured_at,
                items=tuple(
                    PendingItemDisplay(
                        row=row,
                        cause_label=(
                            _CAUSE_LABELS.get(row.needs_human_cause, _UNKNOWN_CAUSE_LABEL)
                            if row.needs_human_cause is not None
                            else _UNKNOWN_CAUSE_LABEL
                        ),
                    )
                    for row in rows
                ),
                earlier_attempts=earlier_attempts,
                has_key_image=key_image_pages.get(page_number, False)
                if page_number is not None
                else False,
            )
        )
    groups.sort(key=lambda g: g.captured_at)
    return groups, len(now_gradable_captures)


@app.post("/keys/{student_id}/{source_id}/regrade-pending")
def submit_regrade_pending(
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """A parent's deliberate act, never automatic -- re-grading days-old work
    silently, the moment a key is added, was explicitly the wrong trade: a
    parent should see what would now be gradable and choose to trigger it.
    Re-decides every no_key_for_page capture whose page now has a key for at
    least one of its problems, using k12ta.pipeline.process.regrade_capture_
    for_resolved_identity -- the page_number was already known (that's what
    no_key_for_page means), so this never re-transcribes and never spends
    quota. A capture where the key still doesn't cover every problem on it
    can land back on no_key_for_page for the ones it doesn't -- honest, not
    a guess, exactly as at capture time."""
    _require_student_and_source(conn, student_id, source_id)
    pending = sessions.list_pending_for_source(conn, student_id, source_id)
    regraded_captures: set[str] = set()
    for row in pending:
        if row.needs_human_cause != _WAITING_ON_KEY_CAUSE or row.page_number is None:
            continue
        if row.capture_id in regraded_captures:
            continue
        regraded_captures.add(row.capture_id)
        regrade_capture_for_resolved_identity(
            conn, student_id, row.session_id, row.capture_id, source_id, row.page_number
        )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


_VERDICTS = frozenset({"correct", "partially_correct", "incorrect"})


@app.post("/keys/{student_id}/{source_id}/answer-verdict", response_model=None)
def submit_answer_verdict(
    request: Request,
    student_id: str,
    source_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    verdict: str = Form(...),
    student_answer_raw: str | None = Form(None),
    key_answer_text: str | None = Form(None),
    page_number: int | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse | HTMLResponse:
    """A parent's one-tap verdict on any NEEDS_HUMAN row the grader deliberately
    would not call right or wrong itself (see k12ta.grading.needs_human.decide)
    -- ANSWER_DIFFERS_FROM_KEY, where a key answer exists to disagree with,
    NEEDS_PERSON, where none does and a parent reads the child's own written
    answer instead, and, as of parent feedback 2026-08-30, LOW_CONFIDENCE,
    where the model did transcribe something but wasn't sure enough of it to
    grade automatically. Cause-agnostic on purpose: this is a direct write of a
    person's judgment, not another pass through decide(), and decide() is the
    one place that already knows which causes exist. A malformed verdict
    value is rejected rather than silently ignored: unlike a stale identity
    pick (k12ta.web.app.submit_identity_pick), there is no "current candidate
    set" to re-validate against here, so there is nothing to check but the
    value itself.

    `student_answer_raw`, if given and non-blank, corrects the stored
    transcription before the verdict is applied -- evaluations.html always
    submits this pre-filled with what's on file, so a parent confirms it
    unchanged, fixes a misread character, or clears and retypes it entirely,
    all through the same two buttons. See k12ta.store.captures.
    update_student_answer_raw for why this is trusted the same way a parent's
    own typed answer-key entry already is.

    `key_answer_text` + `page_number` (parent feedback 2026-08-30) are
    NO_KEY_FOR_PAGE's own variant: there is no key to disagree with yet, so
    evaluations.html offers a field to type the real answer and teach it as
    the key, in the same tap that judges this one instance. Goes through
    _save_answer_entry, the same never-silently-overwrite path every other
    key write in this app uses -- NO_KEY_FOR_PAGE means no entry exists yet,
    so a conflict here should be rare, but is handled exactly like any other:
    held back and shown on resolve.html rather than risking a wrong key
    silently beating a right one. The verdict is applied only once the key
    write has a clear outcome (no conflict) -- a held-back conflict leaves
    this row exactly as it was, to judge again once resolved."""
    _require_student_and_source(conn, student_id, source_id)
    if verdict not in _VERDICTS:
        raise HTTPException(400, "verdict must be 'correct', 'partially_correct', or 'incorrect'")
    previous_graded = sessions.get_graded_problem(
        conn, student_id, session_id, capture_id, problem_id
    )
    previous_problem = captures.get_problem(conn, student_id, capture_id, problem_id)
    if student_answer_raw is not None and student_answer_raw.strip():
        captures.update_student_answer_raw(
            conn, student_id, capture_id, problem_id, student_answer_raw.strip()
        )
    if key_answer_text is not None and key_answer_text.strip() and page_number is not None:
        now = datetime.now(UTC).isoformat()
        conflict = _save_answer_entry(
            conn,
            student_id,
            source_id,
            page_number,
            problem_id,
            key_answer_text.strip(),
            None,
            "manual",
            now,
        )
        if conflict is not None:
            student, source = _require_student_and_source(conn, student_id, source_id)
            return templates.TemplateResponse(
                request,
                "resolve.html",
                {
                    "student": student,
                    "source": source,
                    "conflicts": [conflict],
                    "redirect_to": f"/keys/{student_id}/{source_id}/evaluations",
                },
            )
    sessions.apply_human_verdict(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        outcome=verdict,
    )
    if previous_graded is not None and previous_problem is not None:
        new_problem = captures.get_problem(conn, student_id, capture_id, problem_id)
        verdict_correction_audit.insert_audit_row(
            conn,
            verdict_correction_audit.VerdictCorrectionAuditRow(
                student_id=student_id,
                session_id=session_id,
                capture_id=capture_id,
                problem_id=problem_id,
                corrected_at=datetime.now(UTC).isoformat(),
                previous_outcome=previous_graded.outcome,
                previous_needs_human_cause=previous_graded.needs_human_cause,
                new_outcome=verdict,
                previous_student_answer_raw=previous_problem.student_answer_raw,
                new_student_answer_raw=(
                    new_problem.student_answer_raw
                    if new_problem is not None
                    else previous_problem.student_answer_raw
                ),
                source=verdict_correction_audit.VerdictCorrectionSource.NEEDS_HUMAN_RESOLUTION,
            ),
        )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.post("/keys/{student_id}/{source_id}/correct-verdict")
def submit_verdict_correction(
    student_id: str,
    source_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    verdict: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """docs/ROADMAP.md's M5: a parent correcting a verdict the grader already
    called (correct/partially_correct/incorrect) -- evaluations.html's
    "Graded correct/partially correct/incorrect" sections, not the pending
    queue those already have their own verdict form for
    (k12ta.store.sessions.apply_human_verdict, above). Distinct from a
    dispute: this is the parent's own initiative, with no child contest
    involved, so it is refused while a dispute on this exact row is still
    open -- k12ta.store.disputes.resolve is the one designated path for
    that case, and letting this endpoint race it would let a correction
    bypass the required resolution comment a dispute demands. A row with no
    prior grade at all (never seen by k12ta.store.sessions.get_graded_
    problem) is a 404: there is nothing here to correct, only apply_human_
    verdict's first-time path applies to it. No parent-PIN gate, matching
    apply_human_verdict -- docs/ROADMAP.md's M5 section leaves that an open
    question, unchanged by this pass."""
    _require_student_and_source(conn, student_id, source_id)
    if verdict not in _VERDICTS:
        raise HTTPException(400, "verdict must be 'correct', 'partially_correct', or 'incorrect'")
    previous_graded = sessions.get_graded_problem(
        conn, student_id, session_id, capture_id, problem_id
    )
    if previous_graded is None:
        raise HTTPException(404, "no such graded problem")
    open_dispute = disputes.get(conn, student_id, session_id, capture_id, problem_id)
    if open_dispute is not None and open_dispute.resolved_at is None:
        raise HTTPException(409, "this row has an open dispute -- resolve that first")
    sessions.correct_decided_verdict(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        outcome=verdict,
    )
    problem = captures.get_problem(conn, student_id, capture_id, problem_id)
    answer_raw = problem.student_answer_raw if problem is not None else ""
    verdict_correction_audit.insert_audit_row(
        conn,
        verdict_correction_audit.VerdictCorrectionAuditRow(
            student_id=student_id,
            session_id=session_id,
            capture_id=capture_id,
            problem_id=problem_id,
            corrected_at=datetime.now(UTC).isoformat(),
            previous_outcome=previous_graded.outcome,
            previous_needs_human_cause=previous_graded.needs_human_cause,
            new_outcome=verdict,
            previous_student_answer_raw=answer_raw,
            new_student_answer_raw=answer_raw,
            source=verdict_correction_audit.VerdictCorrectionSource.DECIDED_VERDICT_CORRECTION,
        ),
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.get("/keys/{student_id}/{source_id}/captures/{capture_id}/image")
def pending_capture_image(
    student_id: str,
    source_id: str,
    capture_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> FileResponse:
    """A student capture's own photo, for the parent-facing pending list --
    reads the same page_captures row and the same file k12ta.web.app's own
    image route does; separate route because k12ta.keys is its own app, own
    process, and cannot reach into k12ta.web's routes (docs/ARCHITECTURE.md)."""
    _require_student_and_source(conn, student_id, source_id)
    capture = captures.get_page_capture(conn, student_id, capture_id)
    if capture is None:
        raise HTTPException(404, "no such capture")
    return FileResponse(capture.image_path, media_type="image/jpeg")


@app.get("/keys/{student_id}/{source_id}/key-image/{page_number}")
def key_page_image(
    student_id: str,
    source_id: str,
    page_number: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> FileResponse:
    """The key scan behind one page's confirmed answers, if one was saved
    (k12ta.store.key_page_images -- persisted going forward only, 2026-08-22;
    a page confirmed before this exists has no row and 404s honestly)."""
    _require_student_and_source(conn, student_id, source_id)
    image_path = key_page_images.get_image_path(conn, student_id, source_id, page_number)
    if image_path is None:
        raise HTTPException(404, "no key image on file for this page")
    return FileResponse(image_path, media_type="image/jpeg")


def _enrollment_summary(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> EnrollmentSummary:
    """The at-a-glance counts both the landing page and the evaluations page
    need. Recomputed by each caller rather than cached anywhere -- this is a
    single household's local sqlite file, and a repeated query is far cheaper
    than a caching layer would be to get right."""
    pending = sessions.list_pending_for_source(conn, student_id, source_id)
    capture_groups, _ = _group_pending_by_capture(conn, student_id, source_id, pending)
    resolved = sessions.list_resolved_for_source(conn, student_id, source_id)
    return _summarize_enrollment(pending, capture_groups, resolved)


@app.get("/keys/{student_id}/{source_id}", response_class=HTMLResponse)
def enrollment_landing(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """The first thing a parent sees after picking an enrollment -- a
    dashboard-style landing, not the pending/graded detail (docs/ROADMAP.md,
    parent nav restructure): a summary, then "add a key" / "view answer keys"
    / "view evaluations" / "page identity setup" as plain links to their own
    screens. Nothing here is itself a pending or graded item, so this route
    never needs the heavier per-capture image lookups evaluations_screen
    does beyond what _enrollment_summary already computes for the counts."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    summary = _enrollment_summary(conn, student_id, source_id)

    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, source_id)
    identity_counts = {
        "resolved": counts.get("resolved", 0),
        "below_floor": counts.get("below_floor", 0),
        "no_mapping": counts.get("no_mapping", 0),
        "conflicting": counts.get("conflicting", 0),
        "partial": counts.get("partial", 0),
        "no_markers": counts.get("no_markers", 0),
        "no_schema": counts.get("no_schema", 0),
    }
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    current_version = page_identity_schemas.get_current_version(conn, student_id, source_id)
    stale_mapping_count = page_identities.count_stale_for_source(
        conn, student_id, source_id, current_version
    )

    return templates.TemplateResponse(
        request,
        "enrollment_landing.html",
        {
            "student": student,
            "source": source,
            "summary": summary,
            "identity_counts": identity_counts,
            "schema": schema,
            "stale_mapping_count": stale_mapping_count,
        },
    )


@app.get("/keys/{student_id}/{source_id}/evaluations", response_class=HTMLResponse)
def evaluations_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """What's pending, what's graded, and repeated attempts -- reached only
    by a parent choosing "View evaluations" from enrollment_landing, not the
    first thing shown for an enrollment (docs/ROADMAP.md, parent nav
    restructure). Was this module's `enrollment_detail`, minus the "add a
    key" links and the page-identity section, both moved to the landing."""
    student, source = _require_student_and_source(conn, student_id, source_id)

    # Repeated attempts on the same problem are a signal worth a parent seeing
    # only where the coach is withholding the answer -- in FULL mode the answer
    # is disclosed on attempt one, so a repeat count there means nothing. See
    # k12ta.domain.attempts: a plain count, not the guesses themselves, and
    # never shown to the student (k12ta.web never touches this data).
    override = policy_overrides.get_override(conn, student_id, source_id)
    mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
        parent_override=FeedbackMode(override.mode) if override is not None else None,
    )
    rules = rules_for(mode)
    repeated_problems: list[dict[str, int | str]] = []
    if not rules.reveal_final_answer:
        history = _group_by_problem(
            sessions.list_graded_attempts_for_source(conn, student_id, source_id)
        )
        for (page_number, problem_id), attempts in sorted(history.items()):
            past = [
                PastAttempt(outcome=a.outcome, student_answer_raw=a.student_answer_raw)
                for a in attempts[:-1]
            ]
            count = attempt_number(past, attempts[-1].student_answer_raw)
            if count > 1:
                repeated_problems.append(
                    {"page_number": page_number, "problem_id": problem_id, "attempt_count": count}
                )

    pending = sessions.list_pending_for_source(conn, student_id, source_id)
    capture_groups, now_gradable_count = _group_pending_by_capture(
        conn, student_id, source_id, pending
    )
    resolved = sessions.list_resolved_for_source(conn, student_id, source_id)
    summary = _summarize_enrollment(pending, capture_groups, resolved)
    identity_schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    # Gap K (docs/USER_WORKFLOWS.md): child-escalated items, surfaced and
    # prioritized above the app-requested queue below -- rendered first in
    # evaluations.html, not merged into capture_groups (a dispute is on an
    # already-decided row, not a needs_human one; the two queues are
    # different in kind, not just in urgency).
    open_disputes = disputes.list_open_for_source(conn, student_id, source_id)

    return templates.TemplateResponse(
        request,
        "evaluations.html",
        {
            "student": student,
            "source": source,
            "show_repeated_attempts": not rules.reveal_final_answer,
            "repeated_problems": repeated_problems,
            "open_disputes": open_disputes,
            "capture_groups": capture_groups,
            "now_gradable_count": now_gradable_count,
            "summary": summary,
            "correct_items": [r for r in resolved if r.outcome == "correct"],
            "partially_correct_items": [r for r in resolved if r.outcome == "partially_correct"],
            "incorrect_items": [r for r in resolved if r.outcome == "incorrect"],
            "identity_schema": identity_schema,
        },
    )


_DISPUTE_RESOLUTIONS = frozenset({"upheld", "overturned"})


@app.post("/keys/{student_id}/{source_id}/resolve-dispute")
def submit_dispute_resolution(
    student_id: str,
    source_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    resolution: str = Form(...),
    comment: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Gap L (docs/USER_WORKFLOWS.md): a parent's answer to a child's
    dispute -- "upheld" (the incorrect verdict stands) or "overturned" (the
    child was right, k12ta.store.sessions.overturn_dispute_to_correct flips
    the grade in the same action). A comment is required here specifically
    (household decision: mandatory for a dispute, unlike an ordinary
    NEEDS_HUMAN verdict where one stays optional) -- a blank one is refused
    with a loud 400, not silently dropped, since the entire point of this
    action is the explanation the child will see. Resolving twice is a
    silent no-op (disputes.resolve's own contract): the parent's word is
    final, so a second attempt at the same item changes nothing rather than
    erroring on a stale page."""
    _require_student_and_source(conn, student_id, source_id)
    if resolution not in _DISPUTE_RESOLUTIONS:
        raise HTTPException(400, "invalid resolution")
    if not comment.strip():
        raise HTTPException(400, "a comment is required")
    previous_graded = sessions.get_graded_problem(
        conn, student_id, session_id, capture_id, problem_id
    )
    resolved_at = datetime.now(UTC).isoformat()
    changed = disputes.resolve(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        resolution=resolution,
        resolution_comment=comment.strip(),
        resolved_at=resolved_at,
    )
    if changed and resolution == "overturned":
        sessions.overturn_dispute_to_correct(
            conn,
            student_id=student_id,
            session_id=session_id,
            capture_id=capture_id,
            problem_id=problem_id,
        )
        if previous_graded is not None:
            problem = captures.get_problem(conn, student_id, capture_id, problem_id)
            answer_raw = problem.student_answer_raw if problem is not None else ""
            verdict_correction_audit.insert_audit_row(
                conn,
                verdict_correction_audit.VerdictCorrectionAuditRow(
                    student_id=student_id,
                    session_id=session_id,
                    capture_id=capture_id,
                    problem_id=problem_id,
                    corrected_at=resolved_at,
                    previous_outcome=previous_graded.outcome,
                    previous_needs_human_cause=previous_graded.needs_human_cause,
                    new_outcome="correct",
                    previous_student_answer_raw=answer_raw,
                    new_student_answer_raw=answer_raw,
                    source=verdict_correction_audit.VerdictCorrectionSource.DISPUTE_OVERTURNED,
                ),
            )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.get("/keys/{student_id}/{source_id}/answer-keys", response_class=HTMLResponse)
def answer_keys_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """What's actually on file, by page -- a parent could previously only see
    key answers indirectly, quoted back inside a pending or graded row.
    Reuses answer_keys.list_entries_for_source directly; nothing new to
    query, this is the first screen to show that list on its own."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    entries = answer_keys.list_entries_for_source(conn, student_id, source_id)
    by_page: dict[int, list[answer_keys.AnswerKeyEntryRow]] = {}
    for entry in entries:
        by_page.setdefault(entry.page_number, []).append(entry)
    pages = [
        (
            page_number,
            sorted(rows, key=lambda e: _problem_number_sort_key(e.problem_number)),
            key_page_images.get_image_path(conn, student_id, source_id, page_number),
        )
        for page_number, rows in sorted(by_page.items())
    ]
    return templates.TemplateResponse(
        request,
        "answer_keys.html",
        {"student": student, "source": source, "pages": pages},
    )


@app.get("/keys/{student_id}/{source_id}/manage", response_class=HTMLResponse)
def manage_source_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Rename or delete this enrollment -- the gap found 2026-08-22
    (docs/ROADMAP.md): `seed_dev_data` creates sources a family may never
    use, and there was previously no way to correct a placeholder label or
    remove one. `can_delete` is decided up front so the screen can explain
    *why* delete is unavailable rather than a parent discovering it only
    after submitting."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    return templates.TemplateResponse(
        request,
        "manage_source.html",
        {
            "student": student,
            "source": source,
            "can_delete": not content.source_has_real_activity(conn, student_id, source_id),
        },
    )


@app.post("/keys/{student_id}/{source_id}/rename")
def submit_rename_source(
    student_id: str,
    source_id: str,
    label: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    _require_student_and_source(conn, student_id, source_id)
    stripped = label.strip()
    if stripped:
        content.update_content_source_label(conn, student_id, source_id, stripped)
    return RedirectResponse(f"/keys/{student_id}/{source_id}/manage", status_code=303)


@app.post("/keys/{student_id}/{source_id}/grading-mode")
def submit_grading_mode(
    student_id: str,
    source_id: str,
    has_answer_key: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """docs/ROADMAP.md's V1 "two program paths": a parent switching a program
    between keyed (True -- the parent supplies answers) and keyless (False --
    the AI generates them). Never retroactively regrades -- content.set_has_
    answer_key is a plain field update, nothing here calls a regrade path."""
    _require_student_and_source(conn, student_id, source_id)
    content.set_has_answer_key(conn, student_id, source_id, has_answer_key == "1")
    return RedirectResponse(f"/keys/{student_id}/{source_id}/manage", status_code=303)


@app.post("/keys/{student_id}/{source_id}/archive")
def submit_archive_source(
    student_id: str,
    source_id: str,
    archived: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """docs/ROADMAP.md's V1 "Archiving" -- reversible, unlike delete below.
    k12ta.web.app.submit_capture is what actually blocks new child uploads
    once this is set; everything already evaluated stays visible here and on
    every other read path, since none of them filter on this column."""
    _require_student_and_source(conn, student_id, source_id)
    content.set_archived(conn, student_id, source_id, archived == "1")
    return RedirectResponse(f"/keys/{student_id}/{source_id}/manage", status_code=303)


@app.post("/keys/{student_id}/{source_id}/delete")
def submit_delete_source(
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Refuses silently (redirects back to the same screen, nothing changed)
    rather than raising when content.delete_content_source finds real
    activity -- the screen already explained why beforehand; this is a
    stale-submission guard (the activity could only appear between page load
    and submit if a capture landed in that exact window), not the primary
    way a parent learns delete is unavailable."""
    _require_student_and_source(conn, student_id, source_id)
    if content.delete_content_source(conn, student_id, source_id):
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/keys/{student_id}/{source_id}/manage", status_code=303)


_PAGE_ENTRY_PREVIEW_COUNT = 3
"""How many of the typed page's own confirmed answers the confirm step shows
-- same constant, same reasoning, as k12ta.web.app's student-side version:
enough to recognise the page by its actual content, not so many the confirm
screen stops being a quick check."""


@app.post(
    "/keys/{student_id}/{source_id}/preview-page-entry",
    response_class=HTMLResponse,
    response_model=None,
)
async def preview_page_entry(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """Parent-side twin of k12ta.web.app's preview_page_entry -- same shape,
    same reasoning, a separate route because k12ta.keys is its own app and
    cannot import a route from k12ta.web (docs/ARCHITECTURE.md). She reads
    the page's identity off the photo herself, this shows what she's about
    to confirm (the photo again, plus the page's own first few answers), and
    commits nothing yet.

    Body-parsed rather than typed Form(...) params, same reason as
    submit_confirm: the field shape varies with the source's current schema
    -- a bare `page_number` for 0/1 components, one `component_{name}` per
    component for 2+ (see k12ta.web.app.preview_page_entry's docstring for
    why a single raw value can't be trusted as page_number once a second
    component exists). For a 2+-component schema this only looks an existing
    composite up -- never mints a new one -- so an untaught combination
    redirects back unchanged, same honest no-op as a mistyped bare page
    number always has here; a parent who wants to teach a durable mapping
    uses the existing manual-mapping screen instead."""
    student, source = _require_student_and_source(conn, student_id, source_id)

    data = parse_qs((await request.body()).decode())
    capture_id = _get(data, "capture_id")
    session_id = _get(data, "session_id")
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    if len(schema) >= 2:
        values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
        if not all(values):
            return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
        version = page_identity_schemas.get_current_version(conn, student_id, source_id)
        found = page_identities.get_page_number(
            conn, student_id, source_id, build_composite_key(values), version
        )
        if found is None:
            return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
        parsed_page_number = found
    else:
        page_number_raw = _get(data, "page_number").strip()
        if not page_number_raw.isdigit() or int(page_number_raw) <= 0:
            return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
        parsed_page_number = int(page_number_raw)

    entries = answer_keys.get_entries_for_page(conn, student_id, source_id, parsed_page_number)
    preview = sorted(entries, key=lambda e: _problem_number_sort_key(e.problem_number))[
        :_PAGE_ENTRY_PREVIEW_COUNT
    ]

    return templates.TemplateResponse(
        request,
        "confirm_page_entry.html",
        {
            "student": student,
            "source": source,
            "capture_id": capture_id,
            "session_id": session_id,
            "page_number": parsed_page_number,
            "preview": preview,
        },
    )


@app.post("/keys/{student_id}/{source_id}/commit-page-entry")
def commit_page_entry(
    student_id: str,
    source_id: str,
    capture_id: str = Form(...),
    session_id: str = Form(...),
    page_number: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Her real, informed confirmation, after seeing the photo and this
    page's own answers on the preview step. Logged as RESOLVED_BY_PARENT_
    ENTRY, distinct from every other resolution path -- see its own
    docstring in k12ta.grading.page_identity."""
    _require_student_and_source(conn, student_id, source_id)

    regrade_capture_for_resolved_identity(
        conn, student_id, session_id, capture_id, source_id, page_number
    )
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id=student_id,
            source_id=source_id,
            capture_id=capture_id,
            outcome=page_identity.RESOLVED_BY_PARENT_ENTRY,
            resolved_page_number=page_number,
            created_at=datetime.now(UTC).isoformat(),
        ),
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.post("/keys/{student_id}/{source_id}/mark-duplicate")
def submit_mark_duplicate(
    student_id: str,
    source_id: str,
    capture_id: str = Form(...),
    duplicate_of_capture_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """The manual fallback for unresolved captures (2026-08-22 M3.9): a
    parent's own "this photo is the same page as that one," for the case
    automatic dedup can't reach at all -- an unresolved capture has no
    page_number to group by. Deletes and regrades nothing; only changes
    which block `_group_pending_by_capture` folds this capture's items
    into. A self-reference or a capture_id that doesn't actually belong to
    this student is silently ignored -- same "stale submission, nothing
    happens" honesty as submit_identity_pick, not a 500 for a tampered or
    stale form."""
    _require_student_and_source(conn, student_id, source_id)
    if capture_id == duplicate_of_capture_id:
        return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
    if captures.get_page_capture(conn, student_id, capture_id) is None:
        return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
    if captures.get_page_capture(conn, student_id, duplicate_of_capture_id) is None:
        return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)

    capture_duplicates.mark_duplicate(
        conn,
        capture_duplicates.CaptureDuplicateRow(
            student_id=student_id,
            capture_id=capture_id,
            duplicate_of_capture_id=duplicate_of_capture_id,
            marked_at=datetime.now(UTC).isoformat(),
        ),
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.post("/keys/{student_id}/{source_id}/reassign-page")
def submit_reassign_page(
    student_id: str,
    source_id: str,
    capture_id: str = Form(...),
    session_id: str = Form(...),
    page_number: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Parent feedback (2026-08-30): the residual risk docs/ARCHITECTURE.md's
    "asking when exactly one component is missing" section names explicitly
    -- a student's (or a parent's) pick among real, already-confirmed
    candidates can still be the wrong one, and the system has no way to
    detect that on its own. Found on real household data: the same physical
    page, photographed twice, resolved to two different page numbers because
    one of those picks disagreed with the other. This is the fix: a parent
    who can see the actual page and the actual answer key knows definitively
    which page this capture really belongs to, and can say so directly.

    Deliberately just a thin call to regrade_capture_for_resolved_identity --
    the exact same zero-model-call, re-decide-from-stored-transcription
    primitive every other "identity is now known, grade against it" path in
    this app already uses (a student's constrained pick, a parent adding a
    key). The only difference is this capture already had a page_number;
    that function has no opinion about what it was before, so overriding an
    existing (wrong) one works identically to resolving a previously-unknown
    one. Deliberately does not touch page_identities -- this corrects one
    capture's own assignment, not the underlying composite -> page mapping
    that produced the wrong pick in the first place (k12ta.store.
    page_identities.upsert_identity, via the manual-mapping screen, is the
    tool for that, if the same misread would keep happening to future
    captures of this same page)."""
    _require_student_and_source(conn, student_id, source_id)
    if page_number <= 0:
        raise HTTPException(400, "page_number must be positive")
    regrade_capture_for_resolved_identity(
        conn, student_id, session_id, capture_id, source_id, page_number
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


@app.post("/keys/{student_id}/{source_id}/set-problem-number")
def submit_problem_number(
    student_id: str,
    source_id: str,
    capture_id: str = Form(...),
    session_id: str = Form(...),
    old_problem_id: str = Form(...),
    problem_id: str = Form(...),
    page_number: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """A parent typing the real printed question number over a synthesized
    AMBIGUOUS_PROBLEM_ID_PREFIX placeholder (k12ta.pipeline.process,
    NeedsHumanCause.AMBIGUOUS_PROBLEM_ID) -- one-tap, the same risk profile
    as submit_mark_duplicate's own one-tap correction just above, not the
    heavier preview-then-confirm page-identity ask: a wrong *page* grades
    against a stranger's answers, but a wrong question number on an
    already-known page is a narrower mistake a parent can see and redo
    immediately from the same row. A blank submission, or one that collides
    with a real problem already on this capture (k12ta.store.captures.
    rename_problem_id raises ValueError for that), is silently ignored --
    same "stale or malformed submission, nothing happens" honesty as
    submit_mark_duplicate, not a 500. Regrades only when this capture's page
    is already resolved -- k12ta.store.captures.rename_problem_id has
    already run either way, so a still-unresolved page just keeps waiting on
    identity as before, now under its real question number."""
    _require_student_and_source(conn, student_id, source_id)
    new_id = problem_id.strip()
    if not new_id:
        return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
    try:
        captures.rename_problem_id(conn, student_id, capture_id, old_problem_id, new_id)
    except ValueError:
        return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)
    if page_number.isdigit():
        regrade_capture_for_resolved_identity(
            conn, student_id, session_id, capture_id, source_id, int(page_number)
        )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/evaluations", status_code=303)


_BLANK_SCHEMA_ROWS = 3
"""How many empty rows the standalone schema editor offers for adding new
components, beyond whatever the source's current schema already has."""


def _normalize_component_name(raw: str) -> str:
    """A stable internal key derived from whatever a parent types (the standalone
    editor) or whatever the model reported (the confirm screen's discovery panel,
    already close to this shape) -- lowercase, non-alphanumeric runs collapsed to
    a single underscore, so it's always safe to use in a composite key and an
    HTML field name."""
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def _policy_override_context(
    conn: sqlite3.Connection,
    student: students.StudentRow,
    source: content.ContentSourceRow,
    settings: Settings,
    error: str | None = None,
) -> dict[str, object]:
    default_mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
    )
    override = policy_overrides.get_override(conn, student.student_id, source.source_id)
    return {
        "student": student,
        "source": source,
        "pin_configured": settings.parent_pin is not None,
        "default_mode": default_mode.value,
        "current_override": override,
        "mode_labels": FEEDBACK_MODE_LABELS,
        "error": error,
    }


@app.get("/keys/{student_id}/{source_id}/policy-override", response_class=HTMLResponse)
def policy_override_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """The one PIN-gated action in either app (docs/ARCHITECTURE.md): forcing
    a specific feedback mode for this enrollment regardless of what its
    default or "graded by someone else" flag would otherwise resolve to.
    Reachable only from k12ta.keys -- "a student can never change this; only
    a parent-authenticated action can" (k12ta.domain.policy.resolve_mode's
    own docstring)."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    return templates.TemplateResponse(
        request,
        "policy_override.html",
        _policy_override_context(conn, student, source, settings),
    )


@app.post(
    "/keys/{student_id}/{source_id}/policy-override",
    response_class=HTMLResponse,
    response_model=None,
)
def submit_policy_override(
    request: Request,
    student_id: str,
    source_id: str,
    pin: str = Form(""),
    action: str = Form(...),
    mode: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse | RedirectResponse:
    """`action` is "set" or "clear". Checked with secrets.compare_digest, not
    `==` -- a real secret comparison, cheap to do right, even though the
    stakes here (a household's own feedback policy, not account access) are
    modest. Not a login: nothing is set on success beyond this one write and
    its audit row -- no session, no cookie, this PIN is never checked again
    until the next override action."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    if settings.parent_pin is None:
        error = "No parent PIN is configured -- set K12TA_PARENT_PIN to use this."
    elif not secrets.compare_digest(pin, settings.parent_pin):
        error = "Wrong PIN."
    elif action == "set" and mode not in FEEDBACK_MODE_LABELS:
        error = "Choose a mode."
    else:
        error = None

    if error is not None:
        return templates.TemplateResponse(
            request,
            "policy_override.html",
            _policy_override_context(conn, student, source, settings, error=error),
        )

    previous = policy_overrides.get_override(conn, student_id, source_id)
    previous_mode = previous.mode if previous is not None else None
    new_mode = mode if action == "set" else None
    if new_mode is not None:
        policy_overrides.set_override(
            conn,
            policy_overrides.PolicyOverrideRow(
                student_id=student_id,
                source_id=source_id,
                mode=new_mode,
                set_at=datetime.now(UTC).isoformat(),
            ),
        )
    else:
        policy_overrides.clear_override(conn, student_id, source_id)
    policy_override_audit.insert_audit_row(
        conn,
        policy_override_audit.PolicyOverrideAuditRow(
            student_id=student_id,
            source_id=source_id,
            previous_mode=previous_mode,
            new_mode=new_mode,
            recorded_at=datetime.now(UTC).isoformat(),
        ),
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}/policy-override", status_code=303)


@app.get("/keys/{student_id}/{source_id}/identity-schema", response_class=HTMLResponse)
def identity_schema_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Revisable any time, not a one-shot commitment -- pre-filled with whatever
    the current schema already has, plus blank rows to add more."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    rows = [(c.component_name, c.label, c.example) for c in schema]
    rows += [("", "", "")] * _BLANK_SCHEMA_ROWS
    provenance = page_identity_schemas.get_current_schema_provenance(conn, student_id, source_id)
    return templates.TemplateResponse(
        request,
        "identity_schema.html",
        {
            "student": student,
            "source": source,
            "rows": rows,
            # Gap O (docs/USER_WORKFLOWS.md): "unconfirmed" only ever means
            # a child/app proposed this schema and no parent has acted on it
            # yet -- the banner and this screen's own Save button are the
            # whole confirm-or-correct action, no separate button needed.
            "is_unconfirmed": provenance not in (None, "parent"),
        },
    )


def _parse_standalone_schema_form(data: dict[str, list[str]]) -> list[tuple[str, str, str | None]]:
    count = int(_get(data, "component_count", "0"))
    components = []
    for j in range(count):
        raw_name = _get(data, f"component_name_{j}").strip()
        if not raw_name:
            continue
        name = _normalize_component_name(raw_name)
        if not name:
            continue
        label = _get(data, f"component_label_{j}").strip() or raw_name
        example = _get(data, f"component_example_{j}").strip() or None
        components.append((name, label, example))
    return components


@app.post("/keys/{student_id}/{source_id}/identity-schema")
async def submit_identity_schema(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """The only intended way a source's identity schema is set after a first
    scan -- never by hand-editing the database. A submission with no non-blank
    rows leaves the schema exactly as it was; it does not clear it.

    This screen pre-fills the form with the current schema (see
    identity_schema_screen), so opening it and hitting Save without changing
    anything is an ordinary path, not a mistake -- it must not be a version
    bump. `save_new_schema` has no idempotency check of its own (each call is a
    real edit as far as it's concerned), so the guard belongs here: skip the
    save entirely when the submission is identical, component for component, to
    what's already stored. Every real version bump strands every mapping
    confirmed under the old one (see k12ta.store.page_identities' staleness
    rule) -- that cost must only be paid for an actual change. This was not
    hypothetical: an identical resubmission against the real household database
    produced two byte-identical schema versions and stranded 40 confirmed
    mappings under the first one.

    Gap O (docs/USER_WORKFLOWS.md): this is also the whole "confirm or
    correct a child/app-proposed schema" action -- no separate button. If
    the current schema isn't yet parent-authored ("unconfirmed" -- always
    exactly version 1, see get_current_schema_provenance's own docstring for
    why), saving it unchanged confirms it in place (confirm_current_schema,
    no new version, nothing to regrade: every capture already graded under
    it graded correctly). Saving it *changed* is a correction: the new
    version is always "parent" (a parent just submitted it), and because the
    version it replaces was never trusted, every already-resolved capture
    for this source is automatically re-decided against the fixed structure
    (k12ta.pipeline.process.replay_source) and the child is left a notice
    (k12ta.store.identity_corrections) -- the one place in this whole app a
    regrade fires without a parent separately choosing to trigger it, and
    only because closing this exact loop is a promise already made to the
    child the moment a provisional result was shown to her (see
    docs/USER_WORKFLOWS.md §3.5 for why every *other* regrade trigger stays
    manual)."""
    _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    components = _parse_standalone_schema_form(data)
    if components:
        current = page_identity_schemas.get_current_schema(conn, student_id, source_id)
        current_components = [(c.component_name, c.label, c.example) for c in current]
        old_provenance = page_identity_schemas.get_current_schema_provenance(
            conn, student_id, source_id
        )
        was_unconfirmed = old_provenance not in (None, "parent")
        if components == current_components:
            if was_unconfirmed:
                page_identity_schemas.confirm_current_schema(conn, student_id, source_id)
        else:
            page_identity_schemas.save_new_schema(
                conn, student_id, source_id, components, provenance="parent"
            )
            if was_unconfirmed:
                replay_source(conn, student_id, source_id)
                identity_corrections.record_correction(
                    conn, student_id, source_id, datetime.now(UTC).isoformat()
                )
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


@app.get("/keys/{student_id}/{source_id}/identity/manual-entry", response_class=HTMLResponse)
def manual_mapping_screen(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """A bare page_number + one field per current schema component, no photo --
    the backfill mechanism for a mapping you've verified against the physical
    book yourself (e.g. a day-to-page table checked against the workbook),
    without spending quota re-scanning a key page already on file just to
    re-derive what you already know."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    return templates.TemplateResponse(
        request,
        "manual_entry.html",
        {"student": student, "source": source, "schema": schema},
    )


@app.post("/keys/{student_id}/{source_id}/identity/manual-entry")
async def submit_manual_mapping(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Always recorded `source="manual"` -- this route exists precisely for
    values a parent supplies from their own knowledge, never the model's, so the
    eval must never count one of these as a model success. A 2+-component
    schema derives page_number from the full composite (`resolve_or_assign_
    page_number`) rather than trusting a bare typed field, for the same
    collision reason `submit_confirm` does; a 0/1-component schema keeps
    trusting a directly-typed literal, exactly as before this changed."""
    _require_student_and_source(conn, student_id, source_id)
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    if not schema:
        raise HTTPException(400, "no identity schema configured for this source yet")
    data = parse_qs((await request.body()).decode())
    values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
    if not all(values):
        return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)
    version = page_identity_schemas.get_current_version(conn, student_id, source_id)
    composite_key = build_composite_key(values)
    if len(schema) >= 2:
        page_number, _ = page_identities.resolve_or_assign_page_number(
            conn, student_id, source_id, composite_key, version
        )
    else:
        page_number_raw = _get(data, "page_number").strip()
        if not page_number_raw.isdigit():
            return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)
        page_number = int(page_number_raw)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id=source_id,
            page_number=page_number,
            composite_key=composite_key,
            schema_version=version,
            confirmed_at=datetime.now(UTC).isoformat(),
            source="manual",
        ),
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


_MANUAL_ANSWER_ROWS = 20
"""Default row count for the manual answer-entry table -- a full page at this
project's own scale (up to 22 problems seen in real Summer Bridge data) fits in
one sitting without reaching for "add more rows" first."""


@app.get("/keys/{student_id}/{source_id}/answers/manual-entry", response_class=HTMLResponse)
def manual_answers_screen(
    request: Request,
    student_id: str,
    source_id: str,
    page_number: int | None = None,
    redirect_to: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """M3.4: a parent types a page's answers directly, no photograph, no model
    call -- the bridge for a source with no printed answer key (RSM, Kumon).
    Unlike /identity/manual-entry, this renders with no schema too: a stored
    answer isn't useless without one, only unreachable from a future photo
    until one exists (docs/ROADMAP.md's M3.4 note).

    `page_number` and `redirect_to` (parent feedback 2026-08-30) let
    evaluations.html link straight here pre-filled for a specific page,
    landing back on evaluations once saved instead of dead-ending on
    saved.html -- see submit_manual_answers for the actual redirect and its
    validation. Pre-fill is 0/1-component schemas only: a 2+-component
    composite's values aren't known here from a bare page_number alone (the
    surrogate is source-wide, not derived from the components), so a parent
    linking in for one of those still retypes the identity fields."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    return templates.TemplateResponse(
        request,
        "manual_answers.html",
        {
            "student": student,
            "source": source,
            "schema": schema,
            "rows": range(_MANUAL_ANSWER_ROWS),
            "ungradeable_reasons": UNGRADEABLE_REASONS,
            "prefill_page_number": page_number if len(schema) < 2 else None,
            "redirect_to": redirect_to,
        },
    )


def _manual_answer_row(data: dict[str, list[str]], i: int) -> tuple[str, str | None, str | None]:
    """Row i's (problem_number, answer_text, ungradeable_reason) from
    manual_answers.html's submitted form -- same shape as _confirm_answer_content,
    minus a per-row page_number: a manual session is scoped to one page,
    entered once at the top of the form, not repeated per row."""
    problem_number = _get(data, f"problem_number_{i}").strip()
    if not problem_number:
        return "", None, None
    if _get(data, f"ungradeable_{i}") == "1":
        reason = _get(data, f"ungradeable_reason_{i}").strip() or UNGRADEABLE_REASONS[0]
        return problem_number, None, reason
    answer_text = _get(data, f"answer_text_{i}").strip()
    if answer_text:
        return problem_number, answer_text, None
    return "", None, None


def _safe_redirect_to(redirect_to: str) -> str | None:
    """A same-app relative path only -- never lets this app's own response
    redirect somewhere external. Parent feedback (2026-08-30): several
    keys.app screens now accept an optional `redirect_to` so a parent fixing
    something from evaluations.html lands back there instead of a bare
    confirmation screen, same pattern k12ta.web.app.submit_dispute already
    established. Blank or invalid input (including "" -- a form field that
    was simply never supplied) is treated as "no redirect requested," not an
    error -- the worst case is the existing saved.html dead end, not a
    security concern, so there is nothing worth a loud 400 over here."""
    if redirect_to and redirect_to.startswith("/") and not redirect_to.startswith("//"):
        return redirect_to
    return None


@app.post(
    "/keys/{student_id}/{source_id}/answers/manual-entry",
    response_class=HTMLResponse,
    response_model=None,
)
async def submit_manual_answers(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """Always recorded source="manual" on every row saved, answers and identity
    alike -- these are a parent's own typed values, never the model's, same
    reasoning as submit_manual_mapping. The identity mapping, if this source
    has a schema and every component was filled in, is saved in the same
    submission: a parent typing a page's answers from the book already knows
    that page's identity too, no reason to make this two trips. Answer rows
    go through _save_answer_entry, the same never-silently-overwrite path
    submit_confirm's scanned rows use -- a conflict here renders resolve.html
    exactly like a scanned one would. For a 0/1-component schema, page_number
    is required as a bare typed field (unlike /identity/manual-entry's silent
    no-op on a blank field): discarding up to twenty typed answers over one
    missing field is a worse failure than a loud 400. For a 2+-component
    schema, page_number comes from the full composite instead (same collision
    reasoning as submit_confirm/submit_manual_mapping) -- there, it's the
    identity component fields that are required, not a bare number."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    row_count = int(_get(data, "row_count", "0"))
    now = datetime.now(UTC).isoformat()

    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    if len(schema) >= 2:
        values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
        if not all(values):
            raise HTTPException(400, "every identity field is required for this program")
        version = page_identity_schemas.get_current_version(conn, student_id, source_id)
        composite_key = build_composite_key(values)
        page_number, _ = page_identities.resolve_or_assign_page_number(
            conn, student_id, source_id, composite_key, version
        )
        page_identities.upsert_identity(
            conn,
            page_identities.PageIdentityRow(
                student_id=student_id,
                source_id=source_id,
                page_number=page_number,
                composite_key=composite_key,
                schema_version=version,
                confirmed_at=now,
                source="manual",
            ),
        )
    else:
        page_number_raw = _get(data, "page_number").strip()
        if not page_number_raw.isdigit():
            raise HTTPException(400, "page_number is required")
        page_number = int(page_number_raw)
        if schema:
            values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
            if all(values):
                version = page_identity_schemas.get_current_version(conn, student_id, source_id)
                page_identities.upsert_identity(
                    conn,
                    page_identities.PageIdentityRow(
                        student_id=student_id,
                        source_id=source_id,
                        page_number=page_number,
                        composite_key=build_composite_key(values),
                        schema_version=version,
                        confirmed_at=now,
                        source="manual",
                    ),
                )

    saved = 0
    conflicts = []
    for i in range(row_count):
        problem_number, answer_text, ungradeable_reason = _manual_answer_row(data, i)
        if not problem_number:
            continue
        conflict = _save_answer_entry(
            conn,
            student_id,
            source_id,
            page_number,
            problem_number,
            answer_text,
            ungradeable_reason,
            "manual",
            now,
        )
        if conflict is None:
            saved += 1
        else:
            conflicts.append(conflict)

    redirect_to = _safe_redirect_to(_get(data, "redirect_to"))
    if conflicts:
        return templates.TemplateResponse(
            request,
            "resolve.html",
            {
                "student": student,
                "source": source,
                "conflicts": conflicts,
                "redirect_to": redirect_to,
            },
        )
    if redirect_to:
        return RedirectResponse(redirect_to, status_code=303)
    return templates.TemplateResponse(
        request,
        "saved.html",
        {"student": student, "source": source, "saved_count": saved},
    )


@app.get("/keys/{student_id}/{source_id}/upload", response_class=HTMLResponse)
def upload_screen(
    request: Request,
    student_id: str,
    source_id: str,
    redirect_to: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """`redirect_to` (parent feedback 2026-08-30): lets evaluations.html link
    straight here for a "waiting on an answer key" page, so a clean scan
    lands back on evaluations instead of dead-ending on saved.html -- carried
    through the whole upload -> confirm -> save chain (submit_upload,
    _stream_upload_response, _render_upload_result, confirm.html,
    submit_confirm), the same validated field throughout
    (_safe_redirect_to)."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    has_schema = bool(page_identity_schemas.get_current_schema(conn, student_id, source_id))
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "student": student,
            "source": source,
            "has_schema": has_schema,
            "redirect_to": redirect_to,
        },
    )


def _discover_identity_components(
    entries: tuple[KeyPageEntry, ...], extra: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """Union of every identity component name seen across this scan's entries, in
    order of first appearance, each paired with the first non-empty example value
    found for it -- what the "set up this workbook's page identity" panel offers a
    parent to choose from, when no schema exists yet for this source.

    `extra` is Gap I's bonus signal (docs/USER_WORKFLOWS.md): whatever
    discover_identity_from_example_page found on a parent's optional second
    photo of a plain exercise page. The key scan's own findings take
    priority -- it's the artefact actually being confirmed this round --
    `extra` only fills in names the key page itself never showed."""
    seen: dict[str, str] = {}
    for entry in entries:
        for name, value in entry.identity_values.items():
            if name not in seen and value:
                seen[name] = value
    for name, value in (extra or {}).items():
        if name not in seen and value:
            seen[name] = value
    return list(seen.items())


_BLANK_PANEL_ROWS = 2
"""How many empty rows the confirm screen's discovery panel offers beyond
whatever this scan discovered, so a parent can name a marker by hand even when
the model found nothing at all -- the manual-entry fallback generalized to
"nothing on the page," not just "the model was unsure"."""


def _discovery_panel_rows(
    discovered: list[tuple[str, str]],
) -> list[tuple[int, str, str, str]]:
    """(panel_row_index, name, label, example) for the confirm screen's
    discovery panel and its matching per-row identity fields -- discovered
    candidates first (name/label pre-filled, editable), then blank rows. Row
    index, not name, is what per-row identity fields key off of here (unlike
    the targeted-schema case): a name a parent is about to define for the first
    time has no history of a matching field name to align with yet."""
    rows = [(j, name, name.capitalize(), example) for j, (name, example) in enumerate(discovered)]
    start = len(discovered)
    rows += [(start + k, "", "", "") for k in range(_BLANK_PANEL_ROWS)]
    return rows


def _render_upload_result(
    request: Request,
    student: students.StudentRow,
    source: content.ContentSourceRow,
    outcome: KeyIngestionOutcome,
    conn: sqlite3.Connection,
    settings: Settings,
    extra_identity: Mapping[str, str] | None = None,
    redirect_to: str | None = None,
) -> str:
    """The same three outcomes submit_upload has always rendered, as a raw HTML
    string rather than a Response -- called from inside the streaming generator
    below, where returning a Response object makes no sense."""
    if outcome.status is KeyIngestionStatus.QUOTA_EXHAUSTED:
        return templates.get_template("message.html").render(
            request=request, message=QUOTA_EXHAUSTED_MESSAGE, student=student, source=source
        )
    if outcome.status is KeyIngestionStatus.TRANSCRIBE_FAILED:
        return templates.get_template("message.html").render(
            request=request,
            message=f"Could not read that page: {outcome.failure_reason}",
            student=student,
            source=source,
            # Unlike a quota-exhausted stop (retrying now cannot help), a
            # transcribe failure is worth a clear, immediate retry affordance --
            # already-retried-with-backoff server-side by the time a parent ever
            # sees this message; see k12ta.llm.gemini's 429/5xx retry loop.
            show_retry=True,
        )

    assert outcome.normalized_image_bytes is not None  # TRANSCRIBED always sets this
    photo_data_uri = "data:image/jpeg;base64," + base64.b64encode(
        outcome.normalized_image_bytes
    ).decode("ascii")
    # Saved here, not at raw upload -- see save_key_page_image's docstring.
    # submit_confirm links whichever page numbers actually get saved to this
    # path in k12ta.store.key_page_images; a parent who abandons this screen
    # leaves an unreferenced file on disk, harmless at this volume.
    image_path = save_key_page_image(settings, outcome.normalized_image_bytes)
    schema = page_identity_schemas.get_current_schema(conn, student.student_id, source.source_id)
    # A schema's own components when one exists, read by stable component_name.
    # Otherwise (first scan for this source) a discovery panel offering whatever
    # this scan found, plus blank rows to name a marker by hand -- one submit
    # both teaches the schema and confirms this scan's mapping, no separate
    # schema-only step. The panel's rows are keyed by position, not name: a
    # marker a parent is about to define for the first time has no history of a
    # matching per-row field name to align with yet.
    panel_rows = (
        []
        if schema
        else _discovery_panel_rows(_discover_identity_components(outcome.entries, extra_identity))
    )
    return templates.get_template("confirm.html").render(
        request=request,
        student=student,
        source=source,
        entries=_sorted_for_confirm(outcome.entries),
        photo_data_uri=photo_data_uri,
        image_path=image_path,
        ungradeable_reasons=UNGRADEABLE_REASONS,
        identifier_confidence_floor=CONFIDENCE_FLOOR,
        schema=schema,
        panel_rows=panel_rows,
        redirect_to=redirect_to,
    )


def _stream_upload_response(
    request: Request,
    student: students.StudentRow,
    source: content.ContentSourceRow,
    conn: sqlite3.Connection,
    settings: Settings,
    image_bytes: bytes,
    example_page_bytes: bytes | None = None,
    redirect_to: str | None = None,
) -> Iterator[str]:
    """Newline-delimited JSON: zero or more `{"type": "progress", "chars": N}`
    lines while the model call is in flight, then exactly one `{"type": "final",
    "html": "..."}` line carrying the same HTML submit_upload always returned in
    one shot. A parent watching a static spinner for a call that can legitimately
    run minutes has no way to tell "still working" from "stuck" -- see
    docs/ROADMAP.md's M2 note. `on_progress` is called from a worker thread (the
    real transcribe call keeps running there while this generator's own thread,
    dispatched by Starlette per iteration step, blocks on the queue between
    updates) so a plain queue.Queue bridges the two rather than anything async --
    this generator, and the route that returns it, are both deliberately still
    plain sync code, not async def, for the same reason submit_upload already was.
    """
    updates: queue.Queue[tuple[str, object]] = queue.Queue()
    schema = page_identity_schemas.get_current_schema(conn, student.student_id, source.source_id)
    identity_schema = [(c.component_name, c.example) for c in schema]

    def on_progress(chars: int) -> None:
        updates.put(("progress", chars))

    def worker() -> None:
        outcome = transcribe_key_page(
            conn,
            settings,
            lambda: get_transcriber(settings),
            image_bytes,
            on_progress=on_progress,
            identity_schema=identity_schema,
        )
        # Gap I (docs/USER_WORKFLOWS.md): only worth the extra call when
        # there's discovery to help with (no schema yet) and a parent
        # actually supplied a second photo -- never on a targeted-schema
        # upload, where an example page's markers have nothing left to add.
        extra_identity: Mapping[str, str] = {}
        if (
            not schema
            and example_page_bytes is not None
            and outcome.status is KeyIngestionStatus.TRANSCRIBED
        ):
            extra_identity = discover_identity_from_example_page(
                conn, settings, lambda: get_page_transcriber(settings), example_page_bytes
            )
        updates.put(("outcome", (outcome, extra_identity)))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        kind, payload = updates.get()
        if kind == "progress":
            yield json.dumps({"type": "progress", "chars": payload}) + "\n"
            continue
        assert isinstance(payload, tuple)
        outcome, extra_identity = payload
        assert isinstance(outcome, KeyIngestionOutcome)
        assert isinstance(extra_identity, Mapping)
        html = _render_upload_result(
            request, student, source, outcome, conn, settings, extra_identity, redirect_to
        )
        yield json.dumps({"type": "final", "html": html}) + "\n"
        break
    thread.join()


@app.post("/keys/{student_id}/{source_id}/upload")
def submit_upload(
    request: Request,
    student_id: str,
    source_id: str,
    photo: UploadFile = File(...),
    example_page: UploadFile | None = File(None),
    redirect_to: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """A plain `def`, not `async def`, on purpose: Starlette dispatches a sync route
    to a worker thread automatically, which is the only thing that keeps a slow
    (tens-of-seconds) Gemini call for a dense key page from freezing the single
    process's entire event loop -- an `async def` route calling this same blocking
    transcribe chain directly blocked every other request, including the one already
    in flight, for the call's whole duration. See test_upload_does_not_block_other_
    requests_while_transcribing in tests/test_keys_app.py. The response itself is
    streamed too, for a different reason -- see _stream_upload_response.

    `example_page` is Gap I's optional second photo (docs/USER_WORKFLOWS.md) --
    an ordinary exercise page, no answers needed, uploaded alongside the key
    page to help discovery see markers the isolated key page might not show.
    An empty file field still arrives as an UploadFile with no filename, not
    None -- checked here rather than left to _stream_upload_response, so that
    module only ever sees "a real second photo" or nothing."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    image_bytes = photo.file.read()
    example_page_bytes = (
        example_page.file.read() if example_page is not None and example_page.filename else None
    )
    safe_redirect_to = _safe_redirect_to(redirect_to) if redirect_to else None

    return StreamingResponse(
        _stream_upload_response(
            request,
            student,
            source,
            conn,
            settings,
            image_bytes,
            example_page_bytes,
            safe_redirect_to,
        ),
        media_type="application/x-ndjson",
    )


def _parse_discovery_panel_submission(
    data: dict[str, list[str]],
) -> list[tuple[int, str, str, str | None]]:
    """The confirm screen's discovery panel, shown only when no schema exists yet
    (see `_discovery_panel_rows`) -- which rows the parent kept (checked, with a
    non-blank name -- covers both a kept discovered candidate and a blank row
    named by hand), in DOM order, becoming the new schema's component order.
    `panel_row_index` is kept alongside each component because that index, not
    the (just-defined) component name, is what this same submission's per-row
    identity fields are keyed by -- see `_confirm_identity_composite`. [] if
    nothing was kept, in which case nothing about identity is saved this round."""
    count = int(_get(data, "schema_count", "0"))
    components = []
    for j in range(count):
        if _get(data, f"schema_include_{j}") != "1":
            continue
        raw_name = _get(data, f"schema_name_{j}").strip()
        if not raw_name:
            continue
        name = _normalize_component_name(raw_name)
        if not name:
            continue
        label = _get(data, f"schema_label_{j}").strip() or raw_name
        example = _get(data, f"schema_example_{j}").strip() or None
        components.append((j, name, label, example))
    return components


def _confirm_answer_content(
    data: dict[str, list[str]], i: int
) -> tuple[str, str | None, str | None]:
    """Row i's (problem_number, answer_text, ungradeable_reason) from
    `confirm.html`'s submitted form, or ("", None, None) when the row is an
    unused slot or has nothing valid to store (neither an answer nor
    "ungradeable" -- storing it would violate answer_key_entries' CHECK
    constraint anyway). Page-number determination is `submit_confirm`'s own
    job now, not this function's -- it depends on the source's schema shape
    (a bare typed field for 0/1 components, the full composite for 2+), not
    just this one row's fields."""
    problem_number = _get(data, f"problem_number_{i}").strip()
    if not problem_number:
        return "", None, None
    if _get(data, f"ungradeable_{i}") == "1":
        reason = _get(data, f"ungradeable_reason_{i}").strip() or UNGRADEABLE_REASONS[0]
        return problem_number, None, reason
    answer_text = _get(data, f"answer_text_{i}").strip()
    if answer_text:
        return problem_number, answer_text, None
    return "", None, None


def _answer_source(
    data: dict[str, list[str]], i: int, answer_text: str | None, ungradeable_reason: str | None
) -> str:
    """ "model" if row i's final answer_text/ungradeable_reason match what
    `confirm.html` rendered as `answer_text_original_{i}`/
    `ungradeable_reason_original_{i}` (the model's own transcription at
    render time), "manual" if a parent changed either on screen before
    saving -- same reasoning as `_confirm_identity_composite`: one hand-
    corrected field is enough to make the whole row a manual entry, not a
    model success."""
    original_answer = _get(data, f"answer_text_original_{i}").strip() or None
    original_reason = _get(data, f"ungradeable_reason_original_{i}").strip() or None
    if answer_text != original_answer or ungradeable_reason != original_reason:
        return "manual"
    return "model"


def _confirm_identity_composite(
    data: dict[str, list[str]], i: int, field_keys: list[str]
) -> tuple[str | None, str]:
    """Row i's composite identity key, built in schema order, and its source
    ("model"/"manual") -- or (None, "model") if any component is missing a
    value for this row, since a partial composite can never match a future
    capture's fully-populated one anyway. `field_keys` is the schema's
    component_names (targeted mode -- stable, existed before this submit) or
    the discovery panel's row indices as strings (this submit only defined
    them, so they have no other identity yet) -- either way, the field to read
    is `identity_{key}_{i}` / `identity_{key}_original_{i}`. "manual" if any
    single component's submitted value differs from what was originally
    extracted for it (or was typed from scratch, i.e. the original was empty)
    -- one hand-corrected component is enough to make the whole row a manual
    entry, not a model success, same reasoning as
    `page_identities.PageIdentityRow.source`'s docstring."""
    values = []
    any_edited = False
    for key in field_keys:
        value = _get(data, f"identity_{key}_{i}").strip()
        original = _get(data, f"identity_{key}_original_{i}").strip()
        if value != original:
            any_edited = True
        values.append(value)
    if not all(values):
        return None, "model"
    return build_composite_key(values), ("manual" if any_edited else "model")


def _save_answer_entry(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    page_number: int,
    problem_number: str,
    answer_text: str | None,
    ungradeable_reason: str | None,
    source: str,
    now: str,
) -> dict[str, object] | None:
    """Create, no-op-confirm, or report a conflict for one answer_key_entries
    row -- the shared save path every writer of this table goes through
    (submit_confirm's scanned rows below, submit_manual_answers' typed ones,
    M3.4), so a wrong key can never silently overwrite a right one regardless
    of how the value arrived. Never overwrites a stored answer that
    disagrees with a new one -- that's submit_resolve's job, once a parent
    has explicitly chosen, never this function's. Always writes an audit
    row. Returns None once saved (created or already matched); a conflict
    dict, in the exact shape resolve.html expects, otherwise."""
    existing = answer_keys.get_entry(conn, student_id, source_id, page_number, problem_number)
    if existing is None:
        answer_keys.upsert_entry(
            conn,
            answer_keys.AnswerKeyEntryRow(
                student_id=student_id,
                source_id=source_id,
                page_number=page_number,
                problem_number=problem_number,
                answer_text=answer_text,
                ungradeable_reason=ungradeable_reason,
                confirmed_at=now,
                source=source,
            ),
        )
        answer_key_audit.insert_audit_row(
            conn,
            answer_key_audit.AnswerKeyAuditRow(
                student_id=student_id,
                source_id=source_id,
                page_number=page_number,
                problem_number=problem_number,
                action="created",
                old_answer_text=None,
                old_ungradeable_reason=None,
                new_answer_text=answer_text,
                new_ungradeable_reason=ungradeable_reason,
                resolution=None,
                recorded_at=now,
            ),
        )
        return None
    if existing.answer_text == answer_text and existing.ungradeable_reason == ungradeable_reason:
        answer_key_audit.insert_audit_row(
            conn,
            answer_key_audit.AnswerKeyAuditRow(
                student_id=student_id,
                source_id=source_id,
                page_number=page_number,
                problem_number=problem_number,
                action="matched",
                old_answer_text=existing.answer_text,
                old_ungradeable_reason=existing.ungradeable_reason,
                new_answer_text=answer_text,
                new_ungradeable_reason=ungradeable_reason,
                resolution=None,
                recorded_at=now,
            ),
        )
        return None
    return {
        "page_number": page_number,
        "problem_number": problem_number,
        "old_answer_text": existing.answer_text,
        "old_ungradeable_reason": existing.ungradeable_reason,
        "new_answer_text": answer_text,
        "new_ungradeable_reason": ungradeable_reason,
    }


@app.post(
    "/keys/{student_id}/{source_id}/confirm", response_class=HTMLResponse, response_model=None
)
async def submit_confirm(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """Never silently overwrites a stored answer that disagrees with a new scan --
    a wrong key marks correct work wrong, the worst failure this system has. A new
    entry is stored immediately; an identical re-scan is a no-op; anything that
    disagrees is held back and shown to the parent as a conflict to resolve
    explicitly, in `submit_resolve`, not written here."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    row_count = int(_get(data, "row_count", "0"))
    now = datetime.now(UTC).isoformat()

    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    # Targeted mode reads by the schema's own stable component_name; discovery
    # mode (first scan, no schema until this very submit) reads by the panel's
    # row index instead, since a name just defined here has no other identity
    # yet for a per-row field to have been rendered under -- see
    # `_discovery_panel_rows` and `_confirm_identity_composite`.
    field_keys: list[str] = []
    if not schema:
        # First scan for this source: the confirm screen's discovery panel, if
        # the parent kept anything from it, teaches the schema and confirms this
        # same scan's mapping in one submit -- no separate schema-only step.
        new_components = _parse_discovery_panel_submission(data)
        if new_components:
            page_identity_schemas.save_new_schema(
                conn,
                student_id,
                source_id,
                [(name, label, example) for _, name, label, example in new_components],
            )
            schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
            field_keys = [str(j) for j, *_ in new_components]
    else:
        field_keys = [c.component_name for c in schema]
    schema_version = page_identity_schemas.get_current_version(conn, student_id, source_id)

    image_path = _get(data, "image_path")
    pages_scanned: set[int] = set()

    saved = 0
    conflicts = []
    for i in range(row_count):
        problem_number, answer_text, ungradeable_reason = _confirm_answer_content(data, i)
        if not problem_number:
            continue

        composite_key: str | None = None
        identity_source = "model"
        if schema:
            composite_key, identity_source = _confirm_identity_composite(data, i, field_keys)

        if len(schema) >= 2:
            # A single component's raw printed value (a chapter's page-footer
            # digit, say) is not safe to trust as this source's page_number --
            # two different chapters' printed "page 4" would otherwise collide
            # in answer_key_entries' primary key. The full composite is the
            # only thing that can safely determine page_number here, so a row
            # missing any one component has no page to attach it to at all.
            if composite_key is None:
                continue
            page_number, _ = page_identities.resolve_or_assign_page_number(
                conn, student_id, source_id, composite_key, schema_version
            )
        else:
            # 0/1-component schema: a single component's own value already IS
            # source-wide unique (Summer Bridge today), so the human confirming
            # it keeps typing/editing a literal integer directly, exactly as
            # before this change.
            page_number_raw = _get(data, f"page_number_{i}").strip()
            if not page_number_raw.isdigit():
                continue
            page_number = int(page_number_raw)
        pages_scanned.add(page_number)

        if composite_key:
            # The composite -> page_number mapping a student capture later
            # resolves against. Populated here, not at upload time: this is
            # the parent's *confirmed* page_number, which may differ from
            # whatever the model originally guessed (same reasoning as
            # answer_key_entries itself -- the confirmed value is what gets
            # stored).
            page_identities.upsert_identity(
                conn,
                page_identities.PageIdentityRow(
                    student_id=student_id,
                    source_id=source_id,
                    page_number=page_number,
                    composite_key=composite_key,
                    schema_version=schema_version,
                    confirmed_at=now,
                    source=identity_source,
                ),
            )

        conflict = _save_answer_entry(
            conn,
            student_id,
            source_id,
            page_number,
            problem_number,
            answer_text,
            ungradeable_reason,
            _answer_source(data, i, answer_text, ungradeable_reason),
            now,
        )
        if conflict is None:
            saved += 1
        else:
            conflicts.append(conflict)

    if image_path:
        # Linked for every page this scan touched, whether or not each of its
        # answers landed as a new save or a held-back conflict -- the photo is
        # honestly "what was scanned for this page" either way. Empty
        # image_path (no photo behind this submit at all -- the no-photo
        # manual-entry routes) is the normal, expected no-op case, not an
        # error.
        for page_number in pages_scanned:
            key_page_images.upsert_image(
                conn,
                key_page_images.KeyPageImageRow(
                    student_id=student_id,
                    source_id=source_id,
                    page_number=page_number,
                    image_path=image_path,
                    confirmed_at=now,
                ),
            )

    redirect_to = _safe_redirect_to(_get(data, "redirect_to"))
    if conflicts:
        return templates.TemplateResponse(
            request,
            "resolve.html",
            {
                "student": student,
                "source": source,
                "conflicts": conflicts,
                "redirect_to": redirect_to,
            },
        )

    if redirect_to:
        return RedirectResponse(redirect_to, status_code=303)
    return templates.TemplateResponse(
        request,
        "saved.html",
        {"student": student, "source": source, "saved_count": saved},
    )


@app.post(
    "/keys/{student_id}/{source_id}/resolve", response_class=HTMLResponse, response_model=None
)
async def submit_resolve(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """The parent's explicit choice per conflicting row from `resolve.html`. Always
    writes an audit row, whichever way it was resolved. Shared by both callers that
    can land here -- submit_manual_answers and submit_confirm -- so `redirect_to`
    (parent feedback 2026-08-30) only needs handling once."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    row_count = int(_get(data, "row_count", "0"))
    now = datetime.now(UTC).isoformat()

    resolved = 0
    for i in range(row_count):
        problem_number = _get(data, f"problem_number_{i}").strip()
        page_number_raw = _get(data, f"page_number_{i}").strip()
        if not problem_number or not page_number_raw.isdigit():
            continue
        page_number = int(page_number_raw)
        new_answer_text = _get(data, f"new_answer_text_{i}").strip() or None
        new_ungradeable_reason = _get(data, f"new_ungradeable_reason_{i}").strip() or None
        resolution = _get(data, f"resolution_{i}").strip()

        existing = answer_keys.get_entry(conn, student_id, source_id, page_number, problem_number)
        old_answer_text = existing.answer_text if existing is not None else None
        old_ungradeable_reason = existing.ungradeable_reason if existing is not None else None

        if resolution == "used_new":
            answer_keys.upsert_entry(
                conn,
                answer_keys.AnswerKeyEntryRow(
                    student_id=student_id,
                    source_id=source_id,
                    page_number=page_number,
                    problem_number=problem_number,
                    answer_text=new_answer_text,
                    ungradeable_reason=new_ungradeable_reason,
                    confirmed_at=now,
                ),
            )

        answer_key_audit.insert_audit_row(
            conn,
            answer_key_audit.AnswerKeyAuditRow(
                student_id=student_id,
                source_id=source_id,
                page_number=page_number,
                problem_number=problem_number,
                action="conflict_resolved",
                old_answer_text=old_answer_text,
                old_ungradeable_reason=old_ungradeable_reason,
                new_answer_text=new_answer_text,
                new_ungradeable_reason=new_ungradeable_reason,
                resolution=resolution,
                recorded_at=now,
            ),
        )
        resolved += 1

    redirect_to = _safe_redirect_to(_get(data, "redirect_to"))
    if redirect_to:
        return RedirectResponse(redirect_to, status_code=303)
    return templates.TemplateResponse(
        request,
        "saved.html",
        {"student": student, "source": source, "saved_count": resolved},
    )
