"""Routes for the parent-only answer-key ingestion app.

Upload -> transcribe -> confirm is one stateless request/response cycle: a key photo
is never written to disk, and nothing enters `answer_key_entries` before the parent's
confirm POST. HTTP and templates only, per docs/ARCHITECTURE.md -- the quota gate,
orientation fix, and transcription live in `k12ta.pipeline.key_ingestion`.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from k12ta.config import Settings, load_dotenv
from k12ta.llm import build_vision_model
from k12ta.pipeline.key_ingestion import KeyIngestionStatus, transcribe_key_page
from k12ta.store import answer_key_audit, answer_keys, content, db, migrate, students
from k12ta.transcribe.key_page import KeyTranscriber, VisionLLMKeyTranscriber

QUOTA_EXHAUSTED_MESSAGE = (
    "Today's reading budget is used up. Try again tomorrow, or raise "
    "K12TA_DAILY_REQUEST_LIMIT."
)
NO_STUDENTS_MESSAGE = (
    "No students yet. Run `python scripts/seed_dev_data.py` against this server's "
    "K12TA_DATA_DIR."
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
    return templates.TemplateResponse(
        request, "enrollment.html", {"student": student, "source": source}
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


@app.post("/keys/{student_id}/{source_id}/upload", response_class=HTMLResponse)
async def submit_upload(
    request: Request,
    student_id: str,
    source_id: str,
    photo: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    student, source = _require_student_and_source(conn, student_id, source_id)
    image_bytes = await photo.read()

    outcome = transcribe_key_page(conn, settings, lambda: get_transcriber(settings), image_bytes)

    if outcome.status is KeyIngestionStatus.QUOTA_EXHAUSTED:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"message": QUOTA_EXHAUSTED_MESSAGE, "student": student, "source": source},
        )
    if outcome.status is KeyIngestionStatus.TRANSCRIBE_FAILED:
        return templates.TemplateResponse(
            request,
            "message.html",
            {
                "message": f"Could not read that page: {outcome.failure_reason}",
                "student": student,
                "source": source,
            },
        )

    assert outcome.normalized_image_bytes is not None  # TRANSCRIBED always sets this
    photo_data_uri = "data:image/jpeg;base64," + base64.b64encode(
        outcome.normalized_image_bytes
    ).decode("ascii")
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "student": student,
            "source": source,
            "entries": outcome.entries,
            "photo_data_uri": photo_data_uri,
            "ungradeable_reasons": UNGRADEABLE_REASONS,
        },
    )


def _confirm_row(
    data: dict[str, list[str]], i: int
) -> tuple[str, int | None, str | None, str | None]:
    """Row i's (problem_number, page_number, answer_text, ungradeable_reason) from
    `confirm.html`'s submitted form, or ("", None, None, None) when the row is an
    unused slot or has nothing valid to store (neither an answer nor "ungradeable" --
    storing it would violate answer_key_entries' CHECK constraint anyway)."""
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
        problem_number, page_number, answer_text, ungradeable_reason = _confirm_row(data, i)
        if not problem_number or page_number is None:
            continue

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
