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
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.content.source import SourceKind
from k12ta.domain.attempts import PastAttempt
from k12ta.domain.policy import FeedbackMode, resolve_mode, rules_for
from k12ta.domain.text import humanize_math_text
from k12ta.grading import page_identity
from k12ta.grading.needs_human import NeedsHumanCause
from k12ta.ingest import capture as ingest_capture
from k12ta.ingest import schedule as ingest_schedule
from k12ta.llm import build_text_model, build_vision_model
from k12ta.llm.base import TextModel, VisionModel
from k12ta.pipeline.process import (
    PipelineOutcome,
    PipelineStatus,
    process_capture,
    regrade_capture_for_resolved_identity,
)
from k12ta.respond.render import StudentResultView, render_student_result, summarize_results
from k12ta.store import (
    answer_keys,
    captures,
    content,
    db,
    disputes,
    identity_corrections,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    policy_overrides,
    program_requests,
    sessions,
    students,
    verdict_correction_audit,
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
    # Deliberately not "I couldn't read this one" -- that reads as a photo problem, and
    # a real provider rate limit is not one; the photo may be perfectly legible. See
    # PipelineStatus.RATE_LIMITED.
    "rate_limited": "The reading service is too busy right now — try again in a few minutes.",
    # The generic last-resort message, for an exception this route never anticipated --
    # see the worker() wrapper below. Deliberately not "I couldn't read this one": that
    # implies a photo problem, and by definition nothing is known about this one's cause.
    "internal_error": "Something went wrong on my end — ask a grown-up if it keeps happening.",
    # docs/ROADMAP.md's V1 "Archiving": a parent has closed this program to new
    # uploads. Everything already evaluated stays visible elsewhere; this route
    # only refuses the one new thing archiving actually blocks.
    "archived": "This program has been archived — ask a grown-up if you think it should be "
    "reopened.",
}
NO_ASSIGNMENT_MESSAGE = "No assignment is set for today yet."
NO_PROGRAMS_MESSAGE = "No programs are set up for you yet. Ask a grown-up to add one."
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
logger = logging.getLogger(__name__)

WORKER_TIMEOUT_SECONDS = 600
"""A backstop, not the real timeout -- the real one is k12ta.llm._gemini_http's own
per-attempt inactivity timeout and retry/backoff, already sized for a genuinely slow
dense page (docs/ROADMAP.md's M2 entry has the measured numbers). This is only what
protects the request from a *future* bug that escapes both process_capture's own
exception handling and the worker wrapper below without ever putting anything on the
queue -- generous on purpose, well above any real call's worst case, so it never
fires for a legitimately slow model response."""

app = FastAPI()
# M9a (docs/ROADMAP.md): src/k12ta/design/ is the one shared design-system
# directory both this app and k12ta.keys mount independently -- same physical
# file on disk, not two copies. StaticFiles ships with Starlette, itself a
# transitive dependency of the already-approved fastapi, so this needs no new
# line in docs/ARCHITECTURE.md's "Why these dependencies" (AGENTS.md rule 4).
app.mount(
    "/static", StaticFiles(directory=str(Path(__file__).parent.parent / "design")), name="static"
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["humanize_math"] = humanize_math_text

_transcriber: Transcriber | None = None
_text_model: TextModel | None = None
_vision_evaluator_model: VisionModel | None = None


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


def get_text_model(settings: Settings) -> TextModel:
    """M6, the agentic evaluator's tier 2 (docs/ROADMAP.md) -- same reused-
    singleton, same lazy-build reasoning as get_transcriber above. Only ever
    called from inside process_capture, and only when settings.evaluator_
    enabled is True, so a household that never sets K12TA_EVALUATOR_ENABLED
    never builds this at all."""
    global _text_model
    if _text_model is None:
        _text_model = build_text_model(settings)
    return _text_model


def get_vision_evaluator_model(settings: Settings) -> VisionModel:
    """M6, the agentic evaluator's tier 3 (docs/ROADMAP.md) -- same reused-
    singleton, same lazy-build reasoning as get_text_model above. A separate
    instance from get_transcriber's own vision model, not the same one
    reused: transcription and tier-3 evaluation are different call sites,
    each with their own independent request-cap bookkeeping, same
    separation get_text_model already keeps from get_transcriber. Only ever
    called from inside process_capture, and only when settings.evaluator_
    enabled is True, so a household that never sets K12TA_EVALUATOR_ENABLED
    never builds this at all."""
    global _vision_evaluator_model
    if _vision_evaluator_model is None:
        _vision_evaluator_model = build_vision_model(settings)
    return _vision_evaluator_model


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


@app.get("/student/{student_id}", response_class=HTMLResponse, response_model=None)
def program_picker(
    request: Request,
    student_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """Between "who are you" (student_picker) and "what are you doing"
    (source_home): a program picker, skipped straight through when there is
    exactly one program -- the common single-program case stays a near-
    zero-tap path, same reasoning capture.html's own inline source dropdown
    already applied when switching sources, not choosing among them from a
    dedicated screen, was the only thing here at all."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    sources = content.list_content_sources(conn, student_id)
    if len(sources) == 1:
        return RedirectResponse(f"/student/{student_id}/{sources[0].source_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "program_picker.html",
        {
            "student": student,
            "sources": sources,
            "no_programs_message": NO_PROGRAMS_MESSAGE,
            "program_requested_at": (
                program_requests.get_requested_at(conn, student_id) if not sources else None
            ),
        },
    )


@app.post("/student/{student_id}/request-program")
def submit_program_request(
    student_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Gap A (docs/USER_WORKFLOWS.md): a child's own "ask a grown-up to add a
    program" tap from the zero-sources empty state. In-app only, same honest
    limitation as submit_reminder -- no email/SMS infra exists to page anyone,
    this only sets a flag k12ta.keys' home() can show as a badge."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    program_requests.request_program(conn, student_id, datetime.now(UTC).isoformat())
    return RedirectResponse(f"/student/{student_id}", status_code=303)


@app.get("/student/{student_id}/{source_id}", response_class=HTMLResponse)
def source_home(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """ "Add a page" or "My pages" -- the second half of the choice
    program_picker starts. The pending count reuses the same query
    (sessions.list_pending_for_source) the parent-facing pending list is
    built from, purely so a child can see "3 waiting on a grown-up" before
    deciding whether My pages is worth a look."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    source = content.get_content_source(conn, student_id, source_id)
    if source is None:
        raise HTTPException(404, "no such source")
    pending_count = len(sessions.list_pending_for_source(conn, student_id, source_id))
    return templates.TemplateResponse(
        request,
        "source_home.html",
        {
            "student": student,
            "source": source,
            "pending_count": pending_count,
            # Gap O (docs/USER_WORKFLOWS.md): the one place a child sees "a
            # grown-up changed how pages are identified here" -- seen on
            # every visit to this source's hub, not buried in My pages.
            "identity_correction_at": identity_corrections.get_correction(
                conn, student_id, source_id
            ),
        },
    )


@app.post("/student/{student_id}/{source_id}/dismiss-correction")
def submit_dismiss_correction(
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Gap O: the child's own "Got it" tap acknowledging an identity
    correction notice. A no-op, not an error, when there is nothing to
    dismiss (identity_corrections.dismiss_correction's own contract)."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    identity_corrections.dismiss_correction(conn, student_id, source_id)
    return RedirectResponse(f"/student/{student_id}/{source_id}", status_code=303)


@dataclass(frozen=True)
class MyPageItem:
    """One graded_problems row rendered for a student's own "my pages"
    history, not a live session -- the same StudentResultView every result
    a student ever sees is built from (never a raw dict of graded_problems
    fields), so the multi-attempt-oracle suppression and the
    never-show-expected-answer-on-a-miss policy apply exactly as they do on
    session_result.html, not a second, easier-to-get-wrong copy of that
    logic. capture_id/session_id/reminder_requested_at ride alongside only
    because StudentResultView itself is deliberately silent on them -- a
    reminder button needs to know what to update, not what to display."""

    view: StudentResultView
    page_number: int | None
    capture_id: str
    session_id: str
    reminder_requested_at: str | None
    attempt_count: int = 1
    """Gap M (docs/USER_WORKFLOWS.md): how many captures produced a decisive
    (correct/incorrect) verdict for this exact (page_number, problem_id), so a
    child who retakes an already-graded page sees one item with a "tried N
    times" note instead of N separate-looking rows for the same question.
    Only computed for the graded bucket in my_pages() -- see that function's
    own comment for why waiting/to-look-at stay ungrouped."""


_WAITING_ON_GROWNUP_BUCKETS = frozenset({"waiting_on_key", "needs_a_person"})
_TO_LOOK_AT_BUCKETS = frozenset({"could_not_read", "repeat"})
# The grouped/attempt-counted bucket in my_pages() below -- named so a future
# fourth decisive verdict doesn't repeat the bug this fixed: partially_correct
# (docs/ROADMAP.md's V1 "Verdicts", M6's evaluator) is a real, decisive grade
# with page_number always set, exactly like correct/incorrect, and was
# silently excluded from grouping entirely (not merely mis-bucketed) until
# named here explicitly.
_GRADED_DISPLAY_BUCKETS = frozenset({"correct", "incorrect", "partially_correct"})


@app.get("/student/{student_id}/{source_id}/pages", response_class=HTMLResponse)
def my_pages(
    request: Request,
    student_id: str,
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    """Every page this student has ever photographed for this program, and
    where it stands -- the gap named in docs/ROADMAP.md's child-app
    restructure: before this, a student's only view of her own work was
    session_result.html immediately after one capture, a dead end with no
    history and no way back. "Waiting on a grown-up" is its own section
    (with a Remind button, see submit_reminder) since that's the one state a
    student can't resolve herself by retaking a photo."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    source = content.get_content_source(conn, student_id, source_id)
    if source is None:
        raise HTTPException(404, "no such source")

    override = policy_overrides.get_override(conn, student_id, source_id)
    mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
        parent_override=FeedbackMode(override.mode) if override is not None else None,
    )
    rules = rules_for(mode)

    all_graded = sessions.list_all_graded_for_source(conn, student_id, source_id)
    history = _group_by_problem(
        sessions.list_graded_attempts_for_source(conn, student_id, source_id)
    )

    problems_by_capture: dict[str, dict[str, captures.ProblemRow]] = {}
    items: list[MyPageItem] = []
    for g in all_graded:
        if g.capture_id not in problems_by_capture:
            problems_by_capture[g.capture_id] = {
                p.problem_id: p
                for p in captures.list_problems_for_capture(conn, student_id, g.capture_id)
            }
        problem = problems_by_capture[g.capture_id].get(g.problem_id)
        prompt_text = problem.prompt_text if problem is not None else ""
        answer = problem.student_answer_raw if problem is not None else ""
        identity_attempts = (
            history.get((g.page_number, g.problem_id), []) if g.page_number is not None else []
        )
        prior_attempts = tuple(
            PastAttempt(outcome=a.outcome, student_answer_raw=a.student_answer_raw)
            for a in identity_attempts
            if a.capture_id != g.capture_id
        )
        dispute = disputes.get(conn, student_id, g.session_id, g.capture_id, g.problem_id)
        correction = _latest_decided_correction(
            conn, student_id, g.session_id, g.capture_id, g.problem_id
        )
        view = render_student_result(
            g,
            prompt_text,
            answer,
            rules=rules,
            prior_attempts=prior_attempts,
            dispute=dispute,
            latest_decided_correction=correction,
        )
        items.append(
            MyPageItem(
                view=view,
                page_number=g.page_number,
                capture_id=g.capture_id,
                session_id=g.session_id,
                reminder_requested_at=g.reminder_requested_at,
            )
        )

    # Gap M (docs/USER_WORKFLOWS.md): `items` is still in the chronological
    # order list_all_graded_for_source returned it in, so the last item seen
    # for a given (page_number, problem_id) is the most recent capture's --
    # exactly the one worth showing. Scoped to the graded bucket only:
    # page_number is always set for correct/incorrect/partially_correct
    # (GradedProblemRow's own invariant), so the grouping key is unambiguous
    # there. waiting/to-look-at rows can have no page_number at all (a capture
    # still waiting on identity), and collapsing them by problem_id alone
    # risks conflating two genuinely different physical pages -- left
    # ungrouped, unchanged.
    attempt_counts: dict[tuple[int, str], int] = {}
    latest_graded_by_key: dict[tuple[int, str], MyPageItem] = {}
    for item in items:
        if item.view.display_bucket not in _GRADED_DISPLAY_BUCKETS:
            continue
        assert item.page_number is not None
        key = (item.page_number, item.view.problem_id)
        attempt_counts[key] = attempt_counts.get(key, 0) + 1
        latest_graded_by_key[key] = item

    graded_items = sorted(
        (
            replace(item, attempt_count=attempt_counts[key])
            for key, item in latest_graded_by_key.items()
        ),
        key=lambda item: _problem_sort_key(item.view.problem_id),
    )
    items.sort(key=lambda item: _problem_sort_key(item.view.problem_id))
    to_look_at_items = [i for i in items if i.view.display_bucket in _TO_LOOK_AT_BUCKETS]
    waiting_items = [i for i in items if i.view.display_bucket in _WAITING_ON_GROWNUP_BUCKETS]

    is_provisional = page_identity_schemas.get_current_schema_provenance(
        conn, student_id, source_id
    ) not in (None, "parent")
    return templates.TemplateResponse(
        request,
        "my_pages.html",
        {
            "student": student,
            "source": source,
            "graded_items": graded_items,
            "to_look_at_items": to_look_at_items,
            "waiting_items": waiting_items,
            "is_provisional": is_provisional,
        },
    )


@app.post("/student/{student_id}/{source_id}/remind")
def submit_reminder(
    student_id: str,
    source_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """A student's own "remind my grown-up about this" tap -- honest and
    local-only (see migration 0019): no email/SMS infra exists to page
    anyone, so this only sets a flag k12ta.keys shows as a badge on its
    pending list next time a parent opens the app."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    sessions.request_reminder(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        requested_at=datetime.now(UTC).isoformat(),
    )
    return RedirectResponse(f"/student/{student_id}/{source_id}/pages", status_code=303)


@app.post("/student/{student_id}/dispute")
def submit_dispute(
    student_id: str,
    session_id: str = Form(...),
    capture_id: str = Form(...),
    problem_id: str = Form(...),
    reason: str = Form(...),
    redirect_to: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Gap B (docs/USER_WORKFLOWS.md): a child's own "I think this is right"
    contest of an already-graded incorrect verdict -- distinct from
    submit_reminder above, which only ever appears on a row the grader
    itself refused to call. A short reason is required (household decision:
    not a one-tap action); a blank one is refused rather than silently
    filed as an empty dispute. `redirect_to` is supplied by the template
    that rendered the button (session_result.html or my_pages.html), so
    filing a dispute returns the student to wherever she was, not a fixed
    screen neither caller may actually want."""
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    if not reason.strip():
        raise HTTPException(400, "a reason is required")
    if not redirect_to.startswith("/") or redirect_to.startswith("//"):
        # A same-app relative path only -- refuses a tampered request rather
        # than ever redirecting this app's own response somewhere external.
        raise HTTPException(400, "invalid redirect")
    disputes.file_dispute(
        conn,
        student_id=student_id,
        session_id=session_id,
        capture_id=capture_id,
        problem_id=problem_id,
        reason=reason.strip(),
        disputed_at=datetime.now(UTC).isoformat(),
    )
    return RedirectResponse(redirect_to, status_code=303)


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
    if source is not None and source.archived:
        # docs/ROADMAP.md's V1 "Archiving": no new child uploads once a parent
        # has archived this program. Checked before spending anything on image
        # quality -- an archived source refuses regardless of the photo itself.
        yield step("checked", "failed", REJECT_MESSAGES["archived"])
        yield final_html(_reject_html(request, student, assignment_id, "archived"))
        return
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
        # This is the third real hang this shape has produced (see docs/ROADMAP.md,
        # 2026-08-20): an exception escaping process_capture inside this thread means
        # updates.put is never called, and the main thread's updates.get blocks
        # forever -- a stuck spinner, not a rendered failure. Catching everything here
        # is deliberately broader than "the exceptions we expect": the whole point is
        # a backstop for the one this codebase has not anticipated yet either.
        try:
            outcome = process_capture(
                conn,
                settings,
                lambda: get_transcriber(settings),
                student.student_id,
                assignment_id,
                image_bytes,
                # Always passed, same reasoning as get_transcriber above: process_
                # capture's own settings.evaluator_enabled check decides whether
                # this factory is ever actually called, exactly like its own
                # quota gate already decides whether get_transcriber is.
                get_text_model=lambda: get_text_model(settings),
                get_vision_model=lambda: get_vision_evaluator_model(settings),
            )
        except Exception as exc:
            logger.exception(
                "unhandled exception in capture worker student_id=%s assignment_id=%s",
                student.student_id,
                assignment_id,
            )
            outcome = PipelineOutcome.internal_error(f"{type(exc).__name__}: {exc}")
        updates.put(outcome)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        outcome = updates.get(timeout=WORKER_TIMEOUT_SECONDS)
    except queue.Empty:
        # The worker itself never reached its own except block above -- true last
        # resort, e.g. the thread died without unwinding through Python at all.
        logger.error(
            "capture worker produced nothing within %ss student_id=%s assignment_id=%s",
            WORKER_TIMEOUT_SECONDS,
            student.student_id,
            assignment_id,
        )
        outcome = PipelineOutcome.internal_error("worker timed out")
    thread.join(timeout=1)

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
    if outcome.status is PipelineStatus.RATE_LIMITED:
        yield step("read", "failed", REJECT_MESSAGES["rate_limited"])
        yield final_html(_reject_html(request, student, assignment_id, "rate_limited"))
        return
    if outcome.status is PipelineStatus.TRANSCRIBE_FAILED:
        yield step("read", "failed", REJECT_MESSAGES["could_not_transcribe"])
        yield final_html(_reject_html(request, student, assignment_id, "could_not_transcribe"))
        return
    if outcome.status is PipelineStatus.INTERNAL_ERROR:
        yield step("read", "failed", REJECT_MESSAGES["internal_error"])
        yield final_html(_reject_html(request, student, assignment_id, "internal_error"))
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


@dataclass(frozen=True)
class PageNumberAsk:
    """The "ask the human and proceed" fallback (docs/ROADMAP.md's M3.8):
    shown for a capture whose identity is missing outright -- UNKNOWN_PAGE
    (nothing legible at all), or PARTIAL_PAGE_MARKERS with nothing real to
    offer as a constrained pick (IdentityAsk still wins whenever it has real
    candidates; this is only the case where refusing was the only option
    before). Never CONFLICTING_PAGE_MARKERS -- two markers on one photo is
    contradictory data, not missing data, and the fix is re-photographing
    one page, not asking a question nobody photographing a two-page spread
    could answer correctly either. Carries nothing but the capture_id: the
    template builds the photo URL from it, and the two-step confirm flow
    (preview_page_entry / commit_page_entry below) re-derives everything
    else fresh at submit time, same "never trust anything computed at
    render time" discipline IdentityAsk already follows."""

    capture_id: str


@dataclass(frozen=True)
class SchemaGuessAsk:
    """Gap O (docs/USER_WORKFLOWS.md): the bootstrap-schema ask, shown
    instead of a bare PageNumberAsk exactly when this capture's identity
    resolution was NO_SCHEMA (a genuinely brand-new program) and its own
    photo extraction found something to propose. `guessed_components` is
    (component_name, guessed_value) pairs, in the order the model reported
    them -- the child edits or drops each value before confirming; nothing
    here is trusted as final until she submits it."""

    capture_id: str
    guessed_components: tuple[tuple[str, str], ...]


def _resolve_pending_identities(
    conn: sqlite3.Connection,
    student_id: str,
    session_id: str,
    source_id: str,
    graded: list[sessions.GradedProblemRow],
) -> tuple[list[IdentityAsk], list[PageNumberAsk], list[SchemaGuessAsk]]:
    """For every distinct capture in this session still needing a pick or an
    ask (PARTIAL_PAGE_MARKERS or UNKNOWN_PAGE), returns what to show --
    constrained picks with real candidates as IdentityAsk, a Gap O bootstrap
    guess as SchemaGuessAsk when this capture's own outcome was NO_SCHEMA and
    found something to propose, everything else missing (not contradictory)
    as PageNumberAsk -- after opportunistically applying anything that's
    become auto-resolvable since capture time (e.g. a parent has since
    scanned enough pages that only one candidate remains for this photo's
    known components). Always re-derives fresh from the current
    page_identities table on every call; nothing computed at capture time is
    trusted here except SchemaGuessAsk's own proposed values, which the
    child still has to confirm or correct before anything is saved."""
    identity_asks: list[IdentityAsk] = []
    page_number_asks: list[PageNumberAsk] = []
    schema_guess_asks: list[SchemaGuessAsk] = []
    seen_captures: set[str] = set()
    for row in graded:
        if row.needs_human_cause not in (
            NeedsHumanCause.PARTIAL_PAGE_MARKERS.value,
            NeedsHumanCause.UNKNOWN_PAGE.value,
        ):
            continue
        if row.capture_id in seen_captures:
            continue
        seen_captures.add(row.capture_id)

        if row.needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value:
            outcome = page_identity_resolutions.get_outcome_for_capture(
                conn, student_id, row.capture_id
            )
            guess_json = page_identity_resolutions.get_seen_values_for_capture(
                conn, student_id, row.capture_id
            )
            if outcome == page_identity.PageIdentityOutcome.NO_SCHEMA.value and guess_json:
                schema_guess_asks.append(
                    SchemaGuessAsk(
                        capture_id=row.capture_id,
                        guessed_components=tuple(json.loads(guess_json).items()),
                    )
                )
                continue
            # Nothing was extracted at all, or a schema exists but nothing
            # matched it -- there is no candidates concept to even attempt
            # here, ask directly.
            page_number_asks.append(PageNumberAsk(capture_id=row.capture_id))
            continue

        seen_json = page_identity_resolutions.get_seen_values_for_capture(
            conn, student_id, row.capture_id
        )
        if seen_json is None:
            page_number_asks.append(PageNumberAsk(capture_id=row.capture_id))
            continue
        seen_values: dict[str, str] = json.loads(seen_json)
        photo_candidates: dict[str, tuple[str, ...]] = {
            name: (value,) for name, value in seen_values.items()
        }
        # Not necessarily "current": this row's PARTIAL may have come from
        # resolve_with_schema_history's older-schema fallback (a page-number
        # schema can never itself produce PARTIAL -- see resolve()'s
        # NO_MARKERS-before-missing check), so resolve_partial has to be
        # asked at whichever version actually produced it.
        stored_version = page_identity_resolutions.get_schema_version_for_capture(
            conn, student_id, row.capture_id
        )
        resolved_version = page_identity.schema_version_for_seen_component_names(
            conn, student_id, source_id, tuple(seen_values), stored_version
        )
        partial = page_identity.resolve_partial(
            conn, student_id, source_id, photo_candidates, schema_version=resolved_version
        )
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
            # A real component was read, but nothing already confirmed
            # agrees with it -- there is genuinely nothing to constrain a
            # pick from. This is exactly the case the ask-and-proceed
            # principle replaces "refuse honestly" with.
            page_number_asks.append(PageNumberAsk(capture_id=row.capture_id))
            continue
        resolved_schema = page_identity_schemas.get_schema_at_version(
            conn, student_id, source_id, resolved_version
        )
        missing_component = next(
            (c for c in resolved_schema if c.component_name not in seen_values), None
        )
        if missing_component is None:
            continue
        identity_asks.append(
            IdentityAsk(
                capture_id=row.capture_id,
                missing_label=missing_component.label,
                options=tuple(
                    IdentityAskOption(value=m.missing_value, page_number=m.page_number)
                    for m in partial.matches
                ),
            )
        )
    return identity_asks, page_number_asks, schema_guess_asks


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
        # Same "not necessarily current" reasoning as _resolve_pending_identities.
        stored_version = page_identity_resolutions.get_schema_version_for_capture(
            conn, student_id, capture_id
        )
        resolved_version = page_identity.schema_version_for_seen_component_names(
            conn, student_id, source.source_id, tuple(seen_values), stored_version
        )
        partial = page_identity.resolve_partial(
            conn, student_id, source.source_id, photo_candidates, schema_version=resolved_version
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


def _get(data: dict[str, list[str]], key: str, default: str = "") -> str:
    return data.get(key, [default])[0]


def _normalize_component_name(raw: str) -> str:
    """Same shape as k12ta.keys.app's own helper of this name -- a stable
    internal key derived from whatever the model guessed, lowercase,
    non-alphanumeric runs collapsed to a single underscore."""
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


@app.post("/session/{student_id}/{session_id}/bootstrap-schema")
async def submit_schema_guess(
    request: Request,
    student_id: str,
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Gap O (docs/USER_WORKFLOWS.md): the child's confirm-or-correct answer
    to a SchemaGuessAsk -- the one place a schema is ever saved with
    provenance="unconfirmed" (page_identity_schemas.save_new_schema), a
    first mapping minted and saved alongside it
    (page_identities.upsert_identity, source="unconfirmed"), and this
    capture regraded against it. Only ever reachable when this source had
    NO_SCHEMA at all (see docs/USER_WORKFLOWS.md §3.2 for why that scope
    boundary is the entire safety argument): no answer key can exist yet for
    a page nothing has ever mapped to, so the worst this produces is an
    honest NO_KEY_FOR_PAGE, never a confident wrong grade.

    A submission with nothing kept (every row unchecked, or checked with a
    blank name or value) saves nothing -- the ask keeps showing next visit,
    same "not sure" honesty as the identity-pick and page-number-ask flows
    beside it."""
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

    data = parse_qs((await request.body()).decode())
    capture_id = _get(data, "capture_id")
    count = int(_get(data, "component_count", "0"))
    components: list[tuple[str, str, str]] = []
    for i in range(count):
        if _get(data, f"component_include_{i}") != "1":
            continue
        raw_name = _get(data, f"component_name_{i}").strip()
        value = _get(data, f"component_value_{i}").strip()
        name = _normalize_component_name(raw_name)
        if not name or not value:
            continue
        label = _get(data, f"component_label_{i}").strip() or raw_name
        components.append((name, label, value))

    if components:
        now = datetime.now(UTC).isoformat()
        version = page_identity_schemas.save_new_schema(
            conn,
            student_id,
            source.source_id,
            [(name, label, value) for name, label, value in components],
            provenance="unconfirmed",
        )
        composite_key = page_identity.build_composite_key([value for _, _, value in components])
        page_number, _ = page_identities.resolve_or_assign_page_number(
            conn, student_id, source.source_id, composite_key, version
        )
        page_identities.upsert_identity(
            conn,
            page_identities.PageIdentityRow(
                student_id=student_id,
                source_id=source.source_id,
                page_number=page_number,
                composite_key=composite_key,
                schema_version=version,
                confirmed_at=now,
                source="unconfirmed",
            ),
        )
        regrade_capture_for_resolved_identity(
            conn, student_id, session_id, capture_id, source.source_id, page_number
        )
    return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)


_PAGE_ENTRY_PREVIEW_COUNT = 3
"""How many of the typed page's own confirmed answers the confirm step shows
-- "the first two or three answers the system is about to grade against,"
per the ask-and-proceed principle's confirmation requirement. Enough to
recognise the page by its actual content, not so many the confirm screen
stops being a quick check."""


@app.post(
    "/session/{student_id}/{session_id}/preview-page-entry",
    response_class=HTMLResponse,
    response_model=None,
)
async def preview_page_entry(
    request: Request,
    student_id: str,
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse | RedirectResponse:
    """Step one of two for the free-text page-ask (PageNumberAsk): she typed
    an identity, this renders what she's about to confirm -- her own photo
    again, and this page's own first few confirmed answers, if any exist yet
    -- and commits nothing. "A second tap alone is not enough; she must be
    shown what she is confirming" is the whole reason this is a separate
    step from commit_page_entry rather than one route that both previews and
    commits.

    Body-parsed rather than typed Form(...) params, like k12ta.keys.app's
    submit_confirm, because the field shape varies with the source's current
    schema: a bare `page_number` for 0/1 components (exactly as before this
    changed), one `component_{name}` per component for 2+ (a single raw
    value, e.g. a chapter's page-footer digit, is not safe to trust as this
    source's page_number once a second component exists to disambiguate it
    -- see k12ta.store.page_identities.resolve_or_assign_page_number's
    docstring for the collision this prevents). Unlike submit_confirm, this
    path never mints a new mapping for an unrecognised 2+-component identity
    -- it only looks one up; an unknown composite redirects back unchanged,
    same honest "nothing happened" as a mistyped bare page number always
    has here. A parent who wants to teach a durable mapping uses the
    existing manual-mapping screen instead."""
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

    data = parse_qs((await request.body()).decode())
    capture_id = _get(data, "capture_id")
    schema = page_identity_schemas.get_current_schema(conn, student_id, source.source_id)
    if len(schema) >= 2:
        values = [_get(data, f"component_{c.component_name}").strip() for c in schema]
        if not all(values):
            return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)
        version = page_identity_schemas.get_current_version(conn, student_id, source.source_id)
        found = page_identities.get_page_number(
            conn, student_id, source.source_id, page_identity.build_composite_key(values), version
        )
        if found is None:
            return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)
        parsed_page_number = found
    else:
        page_number_raw = _get(data, "page_number").strip()
        if not page_number_raw.isdigit() or int(page_number_raw) <= 0:
            return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)
        parsed_page_number = int(page_number_raw)

    entries = answer_keys.get_entries_for_page(
        conn, student_id, source.source_id, parsed_page_number
    )
    preview = sorted(entries, key=lambda e: _problem_sort_key(e.problem_number))[
        :_PAGE_ENTRY_PREVIEW_COUNT
    ]

    return templates.TemplateResponse(
        request,
        "confirm_page_entry.html",
        {
            "student": student,
            "session_id": session_id,
            "capture_id": capture_id,
            "page_number": parsed_page_number,
            "preview": preview,
        },
    )


@app.post("/session/{student_id}/{session_id}/commit-page-entry")
def commit_page_entry(
    student_id: str,
    session_id: str,
    capture_id: str = Form(...),
    page_number: int = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """Step two: her real, informed confirmation, after seeing her photo and
    this page's own answers on the preview step. Nothing here is re-validated
    against a candidate list the way submit_identity_pick's pick is -- there
    is no list, that is the whole point of this path -- so the safety this
    route relies on is entirely the preview step actually having been shown,
    never a server-side check of her claim's correctness. Logged as
    RESOLVED_BY_STUDENT_ENTRY, never RESOLVED_BY_STUDENT_PICK: a typed,
    self-confirmed number is a different, weaker claim than a pick among
    real candidates, and an accuracy count must never conflate the two."""
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

    regrade_capture_for_resolved_identity(
        conn, student_id, session_id, capture_id, source.source_id, page_number
    )
    page_identity_resolutions.insert_resolution(
        conn,
        page_identity_resolutions.PageIdentityResolutionRow(
            student_id=student_id,
            source_id=source.source_id,
            capture_id=capture_id,
            outcome=page_identity.RESOLVED_BY_STUDENT_ENTRY,
            resolved_page_number=page_number,
            created_at=datetime.now(UTC).isoformat(),
        ),
    )
    return RedirectResponse(f"/session/{student_id}/{session_id}", status_code=303)


def _problem_sort_key(problem_id: str) -> str:
    """Numeric problem_ids ("1", "2", ..., "10") sort in real numeric order, not
    lexicographic (which would put "10" before "2") -- the whole point of
    ordering the results table by question number is that it matches the
    physical page. Zero-padding turns that into a plain string comparison (so
    the key stays a single, fully-orderable `str` rather than a mixed tuple).
    Anything non-numeric (a label like "table-x3", or an
    AMBIGUOUS_PROBLEM_ID_PREFIX placeholder with no real number at all) sorts
    after every real numbered problem, in plain string order among themselves."""
    if problem_id.isdigit():
        return f"0{int(problem_id):09d}"
    return f"1{problem_id}"


def _latest_decided_correction(
    conn: sqlite3.Connection, student_id: str, session_id: str, capture_id: str, problem_id: str
) -> verdict_correction_audit.VerdictCorrectionAuditRow | None:
    """The most recent parent correction of this row's already-decided
    verdict, if any -- docs/ROADMAP.md's M5 child notice. Ignores rows from
    a first-time needs_human resolution or a dispute's own overturn (the
    latter already has its own notice, see k12ta.store.disputes); only
    DECIDED_VERDICT_CORRECTION rows are k12ta.respond.render's concern here.
    list_for_problem returns oldest-first, so the last matching entry is the
    most recent."""
    rows = [
        r
        for r in verdict_correction_audit.list_for_problem(
            conn, student_id, session_id, capture_id, problem_id
        )
        if r.source == verdict_correction_audit.VerdictCorrectionSource.DECIDED_VERDICT_CORRECTION
    ]
    return rows[-1] if rows else None


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


@app.get("/captures/{student_id}/{capture_id}/image")
def capture_image(
    student_id: str, capture_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> FileResponse:
    """The photo behind one capture, for the free-text page-ask/confirm flow
    (session_result.html) to show alongside the question -- a student reading
    her own page number off her own photo, not off a description of it. Path
    comes from `page_captures.image_path`, never from the request, so nothing
    here lets a caller point at an arbitrary file on disk."""
    capture = captures.get_page_capture(conn, student_id, capture_id)
    if capture is None:
        raise HTTPException(404, "no such capture")
    return FileResponse(capture.image_path, media_type="image/jpeg")


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
    override = policy_overrides.get_override(conn, student_id, source.source_id)
    mode = resolve_mode(
        source_default_mode=FeedbackMode(source.default_mode),
        work_will_be_graded_by_someone_else=source.graded_by_someone_else,
        parent_override=FeedbackMode(override.mode) if override is not None else None,
    )
    rules = rules_for(mode)

    graded = sessions.list_graded_problems_for_session(conn, student_id, session_id)
    had_partial = any(
        g.needs_human_cause == NeedsHumanCause.PARTIAL_PAGE_MARKERS.value for g in graded
    )
    identity_asks, page_number_asks, schema_guess_asks = _resolve_pending_identities(
        conn, student_id, session_id, source.source_id, graded
    )
    identity_schema = page_identity_schemas.get_current_schema(conn, student_id, source.source_id)
    # Gap O (docs/USER_WORKFLOWS.md): safe to compute from the current
    # version alone -- see page_identity_schemas.get_current_schema_
    # provenance's own docstring for why a source can never have an
    # already-parent-confirmed later version sitting on top of an
    # unconfirmed one.
    is_provisional = page_identity_schemas.get_current_schema_provenance(
        conn, student_id, source.source_id
    ) not in (None, "parent")
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
        dispute = disputes.get(conn, student_id, g.session_id, g.capture_id, g.problem_id)
        correction = _latest_decided_correction(
            conn, student_id, g.session_id, g.capture_id, g.problem_id
        )
        items.append(
            render_student_result(
                g,
                problems_by_id[g.problem_id].prompt_text if g.problem_id in problems_by_id else "",
                answer,
                rules=rules,
                prior_attempts=prior_attempts,
                dispute=dispute,
                latest_decided_correction=correction,
            )
        )

    # Ordered by question number, not database/grading order, so the table lines
    # up with the physical page a parent or student is holding -- see
    # _problem_sort_key.
    items.sort(key=lambda item: _problem_sort_key(item.problem_id))
    summary = summarize_results(items)

    return templates.TemplateResponse(
        request,
        "session_result.html",
        {
            "student": student,
            "session_id": session_id,
            "source_id": source.source_id,
            "items": items,
            "summary": summary,
            "no_problems_message": NO_PROBLEMS_FOUND_MESSAGE,
            "identity_asks": identity_asks,
            "page_number_asks": page_number_asks,
            "schema_guess_asks": schema_guess_asks,
            "identity_schema": identity_schema,
            "is_provisional": is_provisional,
        },
    )
