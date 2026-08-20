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
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.domain.attempts import PastAttempt, attempt_number
from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for
from k12ta.grading.key_grader import CONFIDENCE_FLOOR, find_key_entry
from k12ta.grading.page_identity import build_composite_key
from k12ta.llm import build_vision_model
from k12ta.pipeline.key_ingestion import (
    KeyIngestionOutcome,
    KeyIngestionStatus,
    transcribe_key_page,
)
from k12ta.pipeline.process import regrade_capture_for_resolved_identity
from k12ta.store import (
    answer_key_audit,
    answer_keys,
    content,
    db,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    sessions,
    students,
)
from k12ta.transcribe.key_page import KeyPageEntry, KeyTranscriber, VisionLLMKeyTranscriber

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
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_transcriber: KeyTranscriber | None = None


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
        {"student": student, "sources": content.list_content_sources(conn, student.student_id)}
        for student in students.list_students(conn)
    ]
    return templates.TemplateResponse(
        request,
        "home.html",
        {"rows": rows, "no_students_message": NO_STUDENTS_MESSAGE},
    )


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
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


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


def _bucket_pending(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    pending: list[sessions.PendingProblemRow],
) -> dict[str, object]:
    """Groups the waiting list by cause, per the M2 roadmap entry this
    subsumes -- a parent needs to know what to do, not just that something is
    pending, and the fix differs by cause (scan a key page vs. re-photograph
    vs. wait). needs_person and answer_differs are deliberately excluded from
    every "waiting" bucket and returned on their own: neither is waiting on
    more data arriving, both are actionable right now -- needs_person because
    the key itself says the answer varies, answer_differs because only a
    person can tell a valid alternate name from a real mistake (see
    k12ta.grading.needs_human.NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY). A
    legacy row with no cause at all (predates the needs_human_cause column)
    is left out of every bucket rather than folded into one that would
    misstate why it's here -- genuinely unknown, not a guess dressed up as
    one."""
    waiting_on_key = []
    waiting_on_identity = []
    waiting_on_transcription = []
    needs_person = []
    answer_differs = []
    now_gradable_captures: set[str] = set()
    for row in pending:
        if row.needs_human_cause == _WAITING_ON_KEY_CAUSE:
            waiting_on_key.append(row)
            if row.page_number is not None and (
                find_key_entry(
                    answer_keys.get_entries_for_page(conn, student_id, source_id, row.page_number),
                    row.problem_id,
                )
                is not None
            ):
                now_gradable_captures.add(row.capture_id)
        elif row.needs_human_cause in _WAITING_ON_IDENTITY_CAUSES:
            waiting_on_identity.append(row)
        elif row.needs_human_cause == _WAITING_ON_TRANSCRIPTION_CAUSE:
            waiting_on_transcription.append(row)
        elif row.needs_human_cause == _NEEDS_PERSON_CAUSE:
            needs_person.append(row)
        elif row.needs_human_cause == _ANSWER_DIFFERS_CAUSE:
            answer_differs.append(row)
    return {
        "waiting_on_key": waiting_on_key,
        "waiting_on_identity": waiting_on_identity,
        "waiting_on_transcription": waiting_on_transcription,
        "needs_person": needs_person,
        "answer_differs": answer_differs,
        "now_gradable_count": len(now_gradable_captures),
    }


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
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


_VERDICTS = frozenset({"correct", "incorrect"})


@app.post("/keys/{student_id}/{source_id}/answer-verdict")
def submit_answer_verdict(
    student_id: str,
    source_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    verdict: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """A parent's one-tap verdict on an ANSWER_DIFFERS_FROM_KEY row -- the
    grader deliberately would not call this right or wrong itself (see
    k12ta.grading.needs_human.decide), so this is a direct write of a
    person's judgment, not another pass through decide(). A malformed verdict
    value is rejected rather than silently ignored: unlike a stale identity
    pick (k12ta.web.app.submit_identity_pick), there is no "current candidate
    set" to re-validate against here, so there is nothing to check but the
    value itself."""
    _require_student_and_source(conn, student_id, source_id)
    if verdict not in _VERDICTS:
        raise HTTPException(400, "verdict must be 'correct' or 'incorrect'")
    sessions.apply_human_verdict(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        outcome=verdict,
    )
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


@app.get("/keys/{student_id}/{source_id}", response_class=HTMLResponse)
def enrollment_detail(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Scanning a key lives under the enrollment it belongs to, not at the top
    level -- a key only ever means something in the context of one enrollment.
    Recent sessions and pages-waiting-on-a-key say plainly they're not built yet
    rather than showing an empty panel; neither is a feature of this task."""
    student, source = _require_student_and_source(conn, student_id, source_id)
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

    # Repeated attempts on the same problem are a signal worth a parent seeing
    # only where the coach is withholding the answer -- in FULL mode the answer
    # is disclosed on attempt one, so a repeat count there means nothing. See
    # k12ta.domain.attempts: a plain count, not the guesses themselves, and
    # never shown to the student (k12ta.web never touches this data).
    mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
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
    pending_buckets = _bucket_pending(conn, student_id, source_id, pending)

    return templates.TemplateResponse(
        request,
        "enrollment.html",
        {
            "student": student,
            "source": source,
            "identity_counts": identity_counts,
            "schema": schema,
            "stale_mapping_count": stale_mapping_count,
            "show_repeated_attempts": not rules.reveal_final_answer,
            "repeated_problems": repeated_problems,
            "waiting_on_key": pending_buckets["waiting_on_key"],
            "waiting_on_identity": pending_buckets["waiting_on_identity"],
            "waiting_on_transcription": pending_buckets["waiting_on_transcription"],
            "needs_person": pending_buckets["needs_person"],
            "answer_differs": pending_buckets["answer_differs"],
            "now_gradable_count": pending_buckets["now_gradable_count"],
        },
    )


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
    return templates.TemplateResponse(
        request, "identity_schema.html", {"student": student, "source": source, "rows": rows}
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
    mappings under the first one."""
    _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    components = _parse_standalone_schema_form(data)
    if components:
        current = page_identity_schemas.get_current_schema(conn, student_id, source_id)
        current_components = [(c.component_name, c.label, c.example) for c in current]
        if components != current_components:
            page_identity_schemas.save_new_schema(conn, student_id, source_id, components)
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
    eval must never count one of these as a model success."""
    _require_student_and_source(conn, student_id, source_id)
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    if not schema:
        raise HTTPException(400, "no identity schema configured for this source yet")
    data = parse_qs((await request.body()).decode())
    page_number_raw = _get(data, "page_number").strip()
    values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
    if page_number_raw.isdigit() and all(values):
        version = page_identity_schemas.get_current_version(conn, student_id, source_id)
        page_identities.upsert_identity(
            conn,
            page_identities.PageIdentityRow(
                student_id=student_id,
                source_id=source_id,
                page_number=int(page_number_raw),
                composite_key=build_composite_key(values),
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
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """M3.4: a parent types a page's answers directly, no photograph, no model
    call -- the bridge for a source with no printed answer key (RSM, Kumon).
    Unlike /identity/manual-entry, this renders with no schema too: a stored
    answer isn't useless without one, only unreachable from a future photo
    until one exists (docs/ROADMAP.md's M3.4 note)."""
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
        },
    )


def _manual_answer_row(data: dict[str, list[str]], i: int) -> tuple[str, str | None, str | None]:
    """Row i's (problem_number, answer_text, ungradeable_reason) from
    manual_answers.html's submitted form -- same shape as _confirm_answer_row,
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


@app.post("/keys/{student_id}/{source_id}/answers/manual-entry", response_class=HTMLResponse)
async def submit_manual_answers(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Always recorded source="manual" on every row saved, answers and identity
    alike -- these are a parent's own typed values, never the model's, same
    reasoning as submit_manual_mapping. The identity mapping, if this source
    has a schema and every component was filled in, is saved in the same
    submission: a parent typing a page's answers from the book already knows
    that page's identity too, no reason to make this two trips. Answer rows
    go through _save_answer_entry, the same never-silently-overwrite path
    submit_confirm's scanned rows use -- a conflict here renders resolve.html
    exactly like a scanned one would. page_number is required (unlike
    /identity/manual-entry's silent no-op on a blank field): discarding up to
    twenty typed answers over one missing field is a worse failure than a
    loud 400."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    data = parse_qs((await request.body()).decode())
    row_count = int(_get(data, "row_count", "0"))
    page_number_raw = _get(data, "page_number").strip()
    if not page_number_raw.isdigit():
        raise HTTPException(400, "page_number is required")
    page_number = int(page_number_raw)
    now = datetime.now(UTC).isoformat()

    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
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

    if conflicts:
        return templates.TemplateResponse(
            request,
            "resolve.html",
            {"student": student, "source": source, "conflicts": conflicts},
        )
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
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    student, source = _require_student_and_source(conn, student_id, source_id)
    return templates.TemplateResponse(
        request, "upload.html", {"student": student, "source": source}
    )


def _discover_identity_components(entries: tuple[KeyPageEntry, ...]) -> list[tuple[str, str]]:
    """Union of every identity component name seen across this scan's entries, in
    order of first appearance, each paired with the first non-empty example value
    found for it -- what the "set up this workbook's page identity" panel offers a
    parent to choose from, when no schema exists yet for this source."""
    seen: dict[str, str] = {}
    for entry in entries:
        for name, value in entry.identity_values.items():
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
    schema = page_identity_schemas.get_current_schema(conn, student.student_id, source.source_id)
    # A schema's own components when one exists, read by stable component_name.
    # Otherwise (first scan for this source) a discovery panel offering whatever
    # this scan found, plus blank rows to name a marker by hand -- one submit
    # both teaches the schema and confirms this scan's mapping, no separate
    # schema-only step. The panel's rows are keyed by position, not name: a
    # marker a parent is about to define for the first time has no history of a
    # matching per-row field name to align with yet.
    panel_rows = (
        [] if schema else _discovery_panel_rows(_discover_identity_components(outcome.entries))
    )
    return templates.get_template("confirm.html").render(
        request=request,
        student=student,
        source=source,
        entries=_sorted_for_confirm(outcome.entries),
        photo_data_uri=photo_data_uri,
        ungradeable_reasons=UNGRADEABLE_REASONS,
        identifier_confidence_floor=CONFIDENCE_FLOOR,
        schema=schema,
        panel_rows=panel_rows,
    )


def _stream_upload_response(
    request: Request,
    student: students.StudentRow,
    source: content.ContentSourceRow,
    conn: sqlite3.Connection,
    settings: Settings,
    image_bytes: bytes,
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
        updates.put(("outcome", outcome))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        kind, payload = updates.get()
        if kind == "progress":
            yield json.dumps({"type": "progress", "chars": payload}) + "\n"
            continue
        outcome = payload
        assert isinstance(outcome, KeyIngestionOutcome)
        html = _render_upload_result(request, student, source, outcome, conn)
        yield json.dumps({"type": "final", "html": html}) + "\n"
        break
    thread.join()


@app.post("/keys/{student_id}/{source_id}/upload")
def submit_upload(
    request: Request,
    student_id: str,
    source_id: str,
    photo: UploadFile = File(...),
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
    streamed too, for a different reason -- see _stream_upload_response."""
    student, source = _require_student_and_source(conn, student_id, source_id)
    image_bytes = photo.file.read()

    return StreamingResponse(
        _stream_upload_response(request, student, source, conn, settings, image_bytes),
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


def _confirm_answer_row(
    data: dict[str, list[str]], i: int
) -> tuple[str, int | None, str | None, str | None]:
    """Row i's (problem_number, page_number, answer_text, ungradeable_reason)
    from `confirm.html`'s submitted form, or ("", None, None, None) when the row
    is an unused slot or has nothing valid to store (neither an answer nor
    "ungradeable" -- storing it would violate answer_key_entries' CHECK
    constraint anyway)."""
    problem_number = _get(data, f"problem_number_{i}").strip()
    if not problem_number:
        return "", None, None, None
    page_number_raw = _get(data, f"page_number_{i}").strip()
    if not page_number_raw.isdigit():
        return "", None, None, None
    page_number = int(page_number_raw)
    if _get(data, f"ungradeable_{i}") == "1":
        reason = _get(data, f"ungradeable_reason_{i}").strip() or UNGRADEABLE_REASONS[0]
        return problem_number, page_number, None, reason
    answer_text = _get(data, f"answer_text_{i}").strip()
    if answer_text:
        return problem_number, page_number, answer_text, None
    return "", None, None, None


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


@app.post("/keys/{student_id}/{source_id}/confirm", response_class=HTMLResponse)
async def submit_confirm(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
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

    saved = 0
    conflicts = []
    for i in range(row_count):
        problem_number, page_number, answer_text, ungradeable_reason = _confirm_answer_row(data, i)
        if not problem_number or page_number is None:
            continue

        if schema:
            composite_key, identity_source = _confirm_identity_composite(data, i, field_keys)
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
            "model",
            now,
        )
        if conflict is None:
            saved += 1
        else:
            conflicts.append(conflict)

    if conflicts:
        return templates.TemplateResponse(
            request,
            "resolve.html",
            {"student": student, "source": source, "conflicts": conflicts},
        )

    return templates.TemplateResponse(
        request,
        "saved.html",
        {"student": student, "source": source, "saved_count": saved},
    )


@app.post("/keys/{student_id}/{source_id}/resolve", response_class=HTMLResponse)
async def submit_resolve(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """The parent's explicit choice per conflicting row from `resolve.html`. Always
    writes an audit row, whichever way it was resolved."""
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

    return templates.TemplateResponse(
        request,
        "saved.html",
        {"student": student, "source": source, "saved_count": resolved},
    )
