"""The capture surface: two taps, no login, one page per photo.

Tap 1 is choosing a student on `/`. Tap 2 is "Take Photo" on `/capture/{student_id}`,
which opens the device camera directly via a hidden `capture="environment"` file
input; the photo's arrival auto-submits the form (see capture.html), so no third tap
is needed. `k12ta.ingest` and `k12ta.pipeline` own the quality gate, assignment
resolution, transcription, and grading — this module is HTTP and templates only, per
docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.content.source import SourceKind
from k12ta.domain.attempts import PastAttempt
from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for
from k12ta.grading import page_identity
from k12ta.grading.needs_human import NeedsHumanCause
from k12ta.ingest import capture as ingest_capture
from k12ta.ingest import schedule as ingest_schedule
from k12ta.llm import build_vision_model
from k12ta.pipeline.process import (
    PipelineOutcome,
    PipelineStatus,
    process_capture,
    regrade_capture_for_resolved_identity,
)
from k12ta.respond.render import render_student_result
from k12ta.store import (
    captures,
    content,
    db,
    migrate,
    page_identity_resolutions,
    page_identity_schemas,
    sessions,
    students,
)
from k12ta.transcribe.base import Transcriber
from k12ta.transcribe.vision_llm import VisionLLMTranscriber

REJECT_MESSAGES = {
    "too_small": "That photo's a little small — let's try again a bit closer.",
    "too_dark": "That photo's too dark to read — let's try again with more light.",
    "looks_like_two_pages": "That looks like two pages — one page at a time works best.",
    "unreadable_file": "I couldn't open that photo — let's try again.",
    "could_not_transcribe": "I couldn't read this one right now — ask a grown-up if it "
    "keeps happening.",
}
NO_ASSIGNMENT_MESSAGE = "No assignment is set for today yet."
NO_STUDENTS_MESSAGE = (
    "No students yet. Run `python scripts/seed_dev_data.py` against this server's "
    "K12TA_DATA_DIR (M3.1 will replace this with real setup)."
)
QUOTA_EXHAUSTED_MESSAGE = "I have done all my reading for today, ask a grown-up."
NO_PROBLEMS_FOUND_MESSAGE = "I did not find any problems on this page."

load_dotenv()  # must run before any Settings.from_env() call in this module
logging.basicConfig(
    level=Settings.from_env().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_transcriber: Transcriber | None = None


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


def get_transcriber(settings: Settings) -> Transcriber:
    """One transcriber instance, reused across every request for the life of the
    process. A fresh instance per request would reset its request_count every time
    and silently defeat the per-run request cap built into k12ta.llm.gemini.

    Deliberately not a FastAPI dependency: `k12ta.pipeline.process_capture` calls
    this only after its quota gate passes, so a quota-exhausted request never pays
    for building a live vision-model adapter, and a broken provider config never
    500s a request that was never going to reach the model anyway."""
    global _transcriber
    if _transcriber is None:
        vision_model = build_vision_model(settings)
        _transcriber = VisionLLMTranscriber(
            vision_model, provider=settings.llm_provider, model=settings.llm_model
        )
    return _transcriber


@app.get("/", response_class=HTMLResponse)
def student_picker(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "students.html",
        {
            "all_students": students.list_students(conn),
            "no_students_message": NO_STUDENTS_MESSAGE,
        },
    )


@app.get("/capture/{student_id}", response_class=HTMLResponse)
def capture_screen(
    request: Request,
    student_id: str,
    source_id: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")

    today = date.today()
    source = (
        content.get_content_source(conn, student_id, source_id)
        if source_id is not None
        else ingest_schedule.resolve_default_source(conn, student_id, today)
    )
    assignment = None
    if source is not None:
        assignment = ingest_schedule.get_or_create_todays_assignment(
            conn, student_id, source.source_id, today
        )
    return templates.TemplateResponse(
        request,
        "capture.html",
        {
            "student": student,
            "source": source,
            "assignment": assignment,
            "all_sources": content.list_content_sources(conn, student_id),
            "no_assignment_message": NO_ASSIGNMENT_MESSAGE,
            # The framing guide illustrates "one physical page vs. two" -- not
            # meaningful for a screenshot, which has no page edges to frame.
            "is_online_exercise": source is not None
            and source.kind == SourceKind.ONLINE_EXERCISE.value,
        },
    )


def _reject_html(
    request: Request, student: students.StudentRow, assignment_id: str, reason: str
) -> str:
    return templates.get_template("result.html").render(
        request=request,
        status="reject",
        message=REJECT_MESSAGES[reason],
        student=student,
        assignment_id=assignment_id,
    )


def _stream_capture_response(
    request: Request,
    student: students.StudentRow,
    assignment_id: str,
    image_bytes_raw: bytes,
    conn: sqlite3.Connection,
    settings: Settings,
) -> Iterator[str]:
    """The checklist protocol: zero or more {"type": "step", "step": ...,
    "status": "started"|"ok"|"failed", "reason": ...} lines as the pipeline
    advances, then one {"type": "final", ...} line. "final" carries "html"
    when the answer is "stay here and try again" (reject, quota, transcribe
    failure -- result.html rendered in place, same content this route has
    always shown for these), or "redirect" when there's a real graded session
    with its own URL worth keeping in the address bar.

    Every step that starts is always resolved -- ok or failed, with a reason
    -- before "final" ships, on every path, so nothing is ever left visually
    mid-flight. This is what fixed the live bug: a rejected photo used to
    render its message while a working-state spinner (meant only for an
    in-flight request) sat there indefinitely, because CSS had silently
    defeated its hidden="" attribute -- see k12ta.web/templates/base.html.
    That's fixed at the CSS layer now too; this protocol is the other half,
    giving the client real state to render instead of a single opaque wait.
    """

    def step(name: str, status: str, reason: str | None = None) -> str:
        event: dict[str, str] = {"type": "step", "step": name, "status": status}
        if reason is not None:
            event["reason"] = reason
        return json.dumps(event) + "\n"

    def final_html(html: str) -> str:
        return json.dumps({"type": "final", "html": html}) + "\n"

    try:
        image_bytes = ingest_capture.normalize_orientation(image_bytes_raw)
    except Exception:
        # An unsupported or corrupt upload -- AGENTS.md rule 11: a failed call
        # is a plain-language state, not a crash. Not narrowed to a specific
        # Pillow exception type on purpose: the boundary here is "arbitrary
        # bytes a student picked from a file chooser."
        message = REJECT_MESSAGES["unreadable_file"]
        yield step("checked", "failed", message)
        yield final_html(_reject_html(request, student, assignment_id, "unreadable_file"))
        return

    assignment = content.get_assignment(conn, student.student_id, assignment_id)
    source = (
        content.get_content_source(conn, student.student_id, assignment.source_id)
        if assignment is not None
        else None
    )
    # The spread heuristic assumes a photograph of a physical page; a source
    # configured as SourceKind.ONLINE_EXERCISE is a screenshot, whose own aspect
    # ratio has nothing to do with "two pages." Configuration, not inference --
    # source is None only if the assignment somehow outlived its source, in
    # which case the safer default (still check) applies.
    check_for_spread = source is None or source.kind != SourceKind.ONLINE_EXERCISE.value
    verdict = ingest_capture.evaluate_image_quality(image_bytes, check_for_spread=check_for_spread)
    if not verdict.accepted:
        reason = verdict.reason or "too_small"
        yield step("checked", "failed", REJECT_MESSAGES[reason])
        yield final_html(_reject_html(request, student, assignment_id, reason))
        return
    yield step("checked", "ok")

    yield step("read", "started")
    updates: queue.Queue[PipelineOutcome] = queue.Queue()

    def worker() -> None:
        outcome = process_capture(
            conn,
            settings,
            lambda: get_transcriber(settings),
            student.student_id,
            assignment_id,
            image_bytes,
        )
        updates.put(outcome)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    outcome = updates.get()
    thread.join()

    if outcome.status is PipelineStatus.QUOTA_EXHAUSTED:
        yield step("read", "failed", QUOTA_EXHAUSTED_MESSAGE)
        html = templates.get_template("result.html").render(
            request=request,
            status="quota_exhausted",
            message=QUOTA_EXHAUSTED_MESSAGE,
            student=student,
        )
        yield final_html(html)
        return
    if outcome.status is PipelineStatus.TRANSCRIBE_FAILED:
        yield step("read", "failed", REJECT_MESSAGES["could_not_transcribe"])
        yield final_html(_reject_html(request, student, assignment_id, "could_not_transcribe"))
        return

    yield step("read", "ok")
    yield step("graded", "ok")
    assert outcome.session_id is not None  # PipelineStatus.GRADED always sets this
    yield (
        json.dumps(
            {"type": "final", "redirect": f"/session/{student.student_id}/{outcome.session_id}"}
        )
        + "\n"
    )


@app.post("/capture/{student_id}")
def submit_capture(
    request: Request,
    student_id: str,
    assignment_id: str = Form(...),
    photo: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """A plain `def`, not `async def`, on purpose -- same reason as
    k12ta.keys.app.submit_upload: Starlette dispatches a sync route to a
    worker thread automatically, which is what keeps a slow model call from
    freezing the single process's whole event loop. The response itself is
    streamed too -- see _stream_capture_response's docstring for the
    checklist protocol this sends."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    image_bytes_raw = photo.file.read()

    return StreamingResponse(
        _stream_capture_response(request, student, assignment_id, image_bytes_raw, conn, settings),
        media_type="application/x-ndjson",
    )


@dataclass(frozen=True)
class IdentityAskOption:
    value: str
    page_number: int


@dataclass(frozen=True)
class IdentityAsk:
    capture_id: str
    missing_label: str
    """Parent-facing label of the one schema component this capture's photo
    didn't show, e.g. "Section" -- never the internal component_name."""
    options: tuple[IdentityAskOption, ...]


def _resolve_pending_identities(
    conn: sqlite3.Connection,
    student_id: str,
    session_id: str,
    source_id: str,
    graded: list[sessions.GradedProblemRow],
) -> list[IdentityAsk]:
    """For every distinct capture in this session still needing a pick
    (PARTIAL_PAGE_MARKERS with real candidates to offer, per
    k12ta.grading.page_identity.resolve_partial), returns what to ask --
    after opportunistically applying anything that's become auto-resolvable
    since capture time (e.g. a parent has since scanned enough pages that
    only one candidate remains for this photo's known components). Always
    re-derives fresh from the current page_identities table on every call;
    nothing computed at capture time is trusted here."""
    schema = page_identity_schemas.get_current_schema(conn, student_id, source_id)
    asks: list[IdentityAsk] = []
    seen_captures: set[str] = set()
    for row in graded:
        if row.needs_human_cause != NeedsHumanCause.PARTIAL_PAGE_MARKERS.value:
            continue
        if row.capture_id in seen_captures:
            continue
        seen_captures.add(row.capture_id)
        seen_json = page_identity_resolutions.get_seen_values_for_capture(
            conn, student_id, row.capture_id
        )
        if seen_json is None:
            continue
        seen_values: dict[str, str] = json.loads(seen_json)
        photo_candidates: dict[str, tuple[str, ...]] = {
            name: (value,) for name, value in seen_values.items()
        }
        partial = page_identity.resolve_partial(conn, student_id, source_id, photo_candidates)
        if partial.auto_resolved_page_number is not None:
            # No longer ambiguous -- apply it now rather than show a stale ask
            # for a question the household has already, unknowingly, answered.
            regrade_capture_for_resolved_identity(
                conn,
                student_id,
                session_id,
                row.capture_id,
                source_id,
                partial.auto_resolved_page_number,
            )
            continue
        if not partial.matches:
            continue
        missing_component = next((c for c in schema if c.component_name not in seen_values), None)
        if missing_component is None:
            continue
        asks.append(
            IdentityAsk(
                capture_id=row.capture_id,
                missing_label=missing_component.label,
                options=tuple(
                    IdentityAskOption(value=m.missing_value, page_number=m.page_number)
                    for m in partial.matches
                ),
            )
        )
    return asks


@app.post("/session/{student_id}/{session_id}/resolve-identity")
def submit_identity_pick(
    student_id: str,
    session_id: str,
    capture_id: str = Form(...),
    page_number: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """A student's constrained pick among candidates k12ta.grading.page_
    identity.resolve_partial already offered on the results page -- never
    free text, always one of a small set of real, already-confirmed pages.

    Re-validates server-side against freshly re-derived candidates before
    doing anything, per docs/ARCHITECTURE.md's "asking when exactly one
    component is missing" section: this can only catch a stale or tampered
    submission (the picked page_number no longer among the current
    candidates), never whether her choice was factually correct -- that
    residual risk is a deliberate, bounded exception to failing closed, and
    is written down there rather than left implicit. A submission that
    doesn't validate changes nothing; the honest refusal continues to render."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    session = sessions.get_session(conn, student_id, session_id)
    if session is None:
        raise HTTPException(404, "no such session")
    assignment = content.get_assignment(conn, student_id, session.assignment_id)
    assert assignment is not None  # a session's assignment can't vanish once created
    source = content.get_content_source(conn, student_id, assignment.source_id)
    assert source is not None  # an assignment's source can't vanish once created

    seen_json = page_identity_resolutions.get_seen_values_for_capture(conn, student_id, capture_id)
    if seen_json is not None:
        seen_values: dict[str, str] = json.loads(seen_json)
        photo_candidates: dict[str, tuple[str, ...]] = {
            name: (value,) for name, value in seen_values.items()
        }
        partial = page_identity.resolve_partial(
            conn, student_id, source.source_id, photo_candidates
        )
        valid_page_numbers = (
            {partial.auto_resolved_page_number}
            if partial.auto_resolved_page_number is not None
            else {m.page_number for m in partial.matches}
        )
        if page_number in valid_page_numbers:
            regrade_capture_for_resolved_identity(
                conn, student_id, session_id, capture_id, source.source_id, page_number
            )
            page_identity_resolutions.insert_resolution(
                conn,
                page_identity_resolutions.PageIdentityResolutionRow(
                    student_id=student_id,
                    source_id=source.source_id,
                    capture_id=capture_id,
                    outcome=page_identity.RESOLVED_BY_STUDENT_PICK,
                    resolved_page_number=page_number,
                    created_at=datetime.now(UTC).isoformat(),
                ),
            )
        # else: a stale or tampered submission -- silently ignored, nothing
        # resolved, the honest refusal keeps rendering.
    return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)


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


@app.get("/session/{student_id}/{session_id}", response_class=HTMLResponse)
def session_results(
    request: Request,
    student_id: str,
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")

    session = sessions.get_session(conn, student_id, session_id)
    if session is None:
        raise HTTPException(404, "no such session")

    assignment = content.get_assignment(conn, student_id, session.assignment_id)
    assert assignment is not None  # a session's assignment can't vanish once created
    source = content.get_content_source(conn, student_id, assignment.source_id)
    assert source is not None  # an assignment's source can't vanish once created
    mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
    )
    rules = rules_for(mode)

    graded = sessions.list_graded_problems_for_session(conn, student_id, session_id)
    had_partial = any(
        g.needs_human_cause == NeedsHumanCause.PARTIAL_PAGE_MARKERS.value for g in graded
    )
    identity_asks = _resolve_pending_identities(
        conn, student_id, session_id, source.source_id, graded
    )
    if had_partial:
        # _resolve_pending_identities may have just regraded a capture in
        # place (the opportunistic auto-resolve case) -- reload so the page
        # reflects that rather than the stale needs-human rows read above.
        # Harmless to reload even when nothing changed (still pending, or no
        # candidates at all): one cheap extra query, never a wrong render.
        graded = sessions.list_graded_problems_for_session(conn, student_id, session_id)

    problems_by_id = {}
    if graded:
        problems_by_id = {
            p.problem_id: p
            for p in captures.list_problems_for_capture(conn, student_id, graded[0].capture_id)
        }

    history = _group_by_problem(
        sessions.list_graded_attempts_for_source(conn, student_id, source.source_id)
    )

    items = []
    for g in graded:
        answer = (
            problems_by_id[g.problem_id].student_answer_raw
            if g.problem_id in problems_by_id
            else ""
        )
        identity_attempts = (
            history.get((g.page_number, g.problem_id), []) if g.page_number is not None else []
        )
        prior_attempts = tuple(
            PastAttempt(outcome=a.outcome, student_answer_raw=a.student_answer_raw)
            for a in identity_attempts
            if a.capture_id != g.capture_id
        )
        items.append(
            render_student_result(
                g,
                problems_by_id[g.problem_id].prompt_text if g.problem_id in problems_by_id else "",
                answer,
                rules=rules,
                prior_attempts=prior_attempts,
            )
        )

    return templates.TemplateResponse(
        request,
        "session_result.html",
        {
            "student": student,
            "session_id": session_id,
            "items": items,
            "no_problems_message": NO_PROBLEMS_FOUND_MESSAGE,
            "identity_asks": identity_asks,
        },
    )
