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
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.grading.key_grader import CONFIDENCE_FLOOR
from k12ta.llm import build_vision_model
from k12ta.pipeline.key_ingestion import (
    KeyIngestionOutcome,
    KeyIngestionStatus,
    transcribe_key_page,
)
from k12ta.store import (
    answer_key_audit,
    answer_keys,
    content,
    db,
    migrate,
    page_identities,
    page_identity_resolutions,
    students,
)
from k12ta.transcribe.key_page import KeyPageEntry, KeyTranscriber, VisionLLMKeyTranscriber

QUOTA_EXHAUSTED_MESSAGE = (
    "Today's reading budget is used up. Try again tomorrow, or raise K12TA_DAILY_REQUEST_LIMIT."
)
# Plain-language options for the enrollment screen's page-identity picker, not the
# internal enum names a parent has no reason to know -- ordered for the select,
# generic "printed page number" last since the other three are more legible/reliable
# per-source options where they apply (docs/ROADMAP.md's page-identity discussion).
# The empty string is "not sure yet": a real, re-selectable choice that leaves
# page_identity_kind NULL and produces k12ta.grading.page_identity's honest
# NOT_FOUND refusal, never a guessed kind.
PAGE_IDENTITY_KIND_LABELS: dict[str, str] = {
    "": "Not sure yet",
    "day_or_unit_banner": "Day or unit number shown on the page",
    "printed_worksheet_code": "Worksheet code in the corner",
    "unique_problem_ids": "Chapter and problem numbers",
    "printed_page_number": "Printed page number",
}
NO_STUDENTS_MESSAGE = (
    "No students yet. Run `python scripts/seed_dev_data.py` against this server's K12TA_DATA_DIR."
)
UNGRADEABLE_REASONS = ("answers_vary", "graph_or_table")

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


def _require_student_and_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> tuple[students.StudentRow, content.ContentSourceRow]:
    student = students.get_student(conn, student_id)
    if student is None:
        raise HTTPException(404, "no such student")
    source = content.get_content_source(conn, student_id, source_id)
    if source is None:
        raise HTTPException(404, "no such content source")
    return student, source


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
        "not_found": counts.get("not_found", 0),
        "conflicting": counts.get("conflicting", 0),
    }
    return templates.TemplateResponse(
        request,
        "enrollment.html",
        {
            "student": student,
            "source": source,
            "identity_counts": identity_counts,
            "page_identity_kind_labels": PAGE_IDENTITY_KIND_LABELS,
        },
    )


@app.post("/keys/{student_id}/{source_id}/identity-kind")
def submit_identity_kind(
    student_id: str,
    source_id: str,
    page_identity_kind: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    """The only intended way `content_sources.page_identity_kind` is ever set after
    a source is first configured -- never by hand-editing the database. Rejects an
    unrecognised value outright rather than silently storing junk `k12ta.grading
    .page_identity.resolve` would then be handed as a source's configured kind."""
    _require_student_and_source(conn, student_id, source_id)
    if page_identity_kind not in PAGE_IDENTITY_KIND_LABELS:
        raise HTTPException(400, "unrecognised page_identity_kind")
    content.set_page_identity_kind(conn, student_id, source_id, page_identity_kind or None)
    return RedirectResponse(f"/keys/{student_id}/{source_id}", status_code=303)


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


def _render_upload_result(
    request: Request,
    student: students.StudentRow,
    source: content.ContentSourceRow,
    outcome: KeyIngestionOutcome,
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
    return templates.get_template("confirm.html").render(
        request=request,
        student=student,
        source=source,
        entries=_sorted_for_confirm(outcome.entries),
        photo_data_uri=photo_data_uri,
        ungradeable_reasons=UNGRADEABLE_REASONS,
        identifier_confidence_floor=CONFIDENCE_FLOOR,
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

    def on_progress(chars: int) -> None:
        updates.put(("progress", chars))

    def worker() -> None:
        outcome = transcribe_key_page(
            conn, settings, lambda: get_transcriber(settings), image_bytes, on_progress=on_progress
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
        html = _render_upload_result(request, student, source, outcome)
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


def _identifier_source(data: dict[str, list[str]], i: int, identifier_value: str) -> str:
    """ "model" when the parent left the transcriber's own extraction unchanged,
    "manual" when they typed or corrected it -- see `page_identities.PageIdentityRow
    .source`'s docstring for why this distinction is kept. `identifier_value_original_i`
    is a hidden field carrying whatever the model originally extracted (empty string
    if it extracted nothing), round-tripped through `confirm.html` unedited."""
    original = _get(data, f"identifier_value_original_{i}").strip()
    return "model" if identifier_value == original else "manual"


def _confirm_row(
    data: dict[str, list[str]], i: int
) -> tuple[str, int | None, str | None, str | None, str, str]:
    """Row i's (problem_number, page_number, answer_text, ungradeable_reason,
    identifier_value, identifier_source) from `confirm.html`'s submitted form, or
    ("", None, None, None, "", "model") when the row is an unused slot or has
    nothing valid to store (neither an answer nor "ungradeable" -- storing it
    would violate answer_key_entries' CHECK constraint anyway)."""
    problem_number = _get(data, f"problem_number_{i}").strip()
    if not problem_number:
        return "", None, None, None, "", "model"
    page_number_raw = _get(data, f"page_number_{i}").strip()
    if not page_number_raw.isdigit():
        return "", None, None, None, "", "model"
    page_number = int(page_number_raw)
    identifier_value = _get(data, f"identifier_value_{i}").strip()
    identifier_source = _identifier_source(data, i, identifier_value)
    if _get(data, f"ungradeable_{i}") == "1":
        reason = _get(data, f"ungradeable_reason_{i}").strip() or UNGRADEABLE_REASONS[0]
        return problem_number, page_number, None, reason, identifier_value, identifier_source
    answer_text = _get(data, f"answer_text_{i}").strip()
    if answer_text:
        return problem_number, page_number, answer_text, None, identifier_value, identifier_source
    return "", None, None, None, "", "model"


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

    saved = 0
    conflicts = []
    for i in range(row_count):
        (
            problem_number,
            page_number,
            answer_text,
            ungradeable_reason,
            identifier_value,
            identifier_source,
        ) = _confirm_row(data, i)
        if not problem_number or page_number is None:
            continue

        if identifier_value:
            # The day/marker -> page_number mapping a student capture later
            # resolves against. Populated here, not at upload time: this is the
            # parent's *confirmed* page_number, which may differ from whatever
            # the model originally guessed (same reasoning as answer_key_entries
            # itself -- the confirmed value is what gets stored).
            page_identities.upsert_identity(
                conn,
                page_identities.PageIdentityRow(
                    student_id=student_id,
                    source_id=source_id,
                    page_number=page_number,
                    identifier_value=identifier_value,
                    confirmed_at=now,
                    source=identifier_source,
                ),
            )

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
            saved += 1
        elif (
            existing.answer_text == answer_text
            and existing.ungradeable_reason == ungradeable_reason
        ):
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
            saved += 1
        else:
            conflicts.append(
                {
                    "page_number": page_number,
                    "problem_number": problem_number,
                    "old_answer_text": existing.answer_text,
                    "old_ungradeable_reason": existing.ungradeable_reason,
                    "new_answer_text": answer_text,
                    "new_ungradeable_reason": ungradeable_reason,
                }
            )

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
