"""The capture surface: two taps, no login, one page per photo.

Tap 1 is choosing a student on `/`. Tap 2 is "Take Photo" on `/capture/{student_id}`,
which opens the device camera directly via a hidden `capture="environment"` file
input; the photo's arrival auto-submits the form (see capture.html), so no third tap
is needed. `k12ta.ingest` and `k12ta.pipeline` own the quality gate, assignment
resolution, transcription, and grading — this module is HTTP and templates only, per
docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.content.source import SourceKind
from k12ta.domain.attempts import PastAttempt
from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for
from k12ta.ingest import capture as ingest_capture
from k12ta.ingest import schedule as ingest_schedule
from k12ta.llm import build_vision_model
from k12ta.pipeline.process import PipelineStatus, process_capture
from k12ta.respond.render import render_student_result
from k12ta.store import captures, content, db, migrate, sessions, students
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


@app.post("/capture/{student_id}", response_model=None)
async def submit_capture(
    request: Request,
    student_id: str,
    assignment_id: str = Form(...),
    photo: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse | RedirectResponse:
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")

    def _reject(reason: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "status": "reject",
                "message": REJECT_MESSAGES[reason],
                "student": student,
                "assignment_id": assignment_id,
            },
        )

    try:
        image_bytes = ingest_capture.normalize_orientation(await photo.read())
    except Exception:
        # An unsupported or corrupt upload -- AGENTS.md rule 11: a failed call is
        # a plain-language state, not a crash. Not narrowed to a specific Pillow
        # exception type on purpose: the boundary here is "arbitrary bytes a
        # student picked from a file chooser," and any way Image.open can fail
        # on that gets the same honest reject, not a 500.
        return _reject("unreadable_file")

    assignment = content.get_assignment(conn, student_id, assignment_id)
    source = (
        content.get_content_source(conn, student_id, assignment.source_id)
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
        return _reject(verdict.reason or "too_small")

    outcome = process_capture(
        conn,
        settings,
        lambda: get_transcriber(settings),
        student_id,
        assignment_id,
        image_bytes,
    )

    if outcome.status is PipelineStatus.QUOTA_EXHAUSTED:
        return templates.TemplateResponse(
            request,
            "result.html",
            {"status": "quota_exhausted", "message": QUOTA_EXHAUSTED_MESSAGE, "student": student},
        )
    if outcome.status is PipelineStatus.TRANSCRIBE_FAILED:
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "status": "reject",
                "message": REJECT_MESSAGES["could_not_transcribe"],
                "student": student,
                "assignment_id": assignment_id,
            },
        )

    assert outcome.session_id is not None  # PipelineStatus.GRADED always sets this
    return RedirectResponse(f"/session/{student_id}/{outcome.session_id}", status_code=303)


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
            "items": items,
            "no_problems_message": NO_PROBLEMS_FOUND_MESSAGE,
        },
    )
