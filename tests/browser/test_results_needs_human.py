"""Regression coverage for the "no key" bug recorded in docs/PROGRESS.md, M2: before
the answer-key store existed, every graded problem was told "I don't have an answer
key for this one yet" unconditionally -- correct by construction back then, but
nothing in the pipeline actually checked, and the same code would have kept saying
it after a key was added. `k12ta.grading.needs_human` now decides and persists one
of six honest causes per problem; this proves the *rendering* layer actually reads
that cause (and, for PARTIAL_PAGE_MARKERS, its structured detail) and shows six
distinct messages, not two.

Seeded directly via the store, not driven through a photo upload: two of the six
causes -- NO_KEY_FOR_PAGE and NEEDS_PERSON, both of which require a page number --
are not reachable through a real upload without a confirmed key on file, and
CONFLICTING_PAGE_MARKERS/PARTIAL_PAGE_MARKERS are decided upstream of `decide()` by
`k12ta.grading.page_identity`, not persisted through this store call directly in
real use. This test is about the rendering contract in isolation from those
separate, already-tracked gaps.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from k12ta.grading.needs_human import NeedsHumanCause
from k12ta.store import captures, content, sessions, students
from tests.browser.conftest import LiveServer

pytestmark = pytest.mark.browser

_STUDENT_ID = "s-browser-needs-human"
_SESSION_ID = "sess-needs-human"
_CAPTURE_ID = "c-needs-human"

_ROWS_IN_ORDER: tuple[tuple[NeedsHumanCause, str | None], ...] = (
    (NeedsHumanCause.LOW_CONFIDENCE, None),
    (NeedsHumanCause.UNKNOWN_PAGE, None),
    (NeedsHumanCause.NO_KEY_FOR_PAGE, None),
    (NeedsHumanCause.NEEDS_PERSON, None),
    (NeedsHumanCause.CONFLICTING_PAGE_MARKERS, None),
    (
        NeedsHumanCause.PARTIAL_PAGE_MARKERS,
        json.dumps({"seen": ["Day"], "missing": ["Section"]}),
    ),
)


def _seed_session_with_all_six_causes(conn: object) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=_STUDENT_ID,
            display_name="Jahnvi",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=_STUDENT_ID,
            source_id="summer_bridge",
            label="Summer Bridge",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id=_STUDENT_ID,
            assignment_id="a-1",
            source_id="summer_bridge",
            created_at="2026-08-13T08:00:00+00:00",
        ),
    )
    captures.insert_page_capture(
        conn,
        captures.PageCaptureRow(
            student_id=_STUDENT_ID,
            capture_id=_CAPTURE_ID,
            assignment_id="a-1",
            captured_at="2026-08-13T08:05:00+00:00",
            image_path="/tmp/does-not-matter.jpg",
        ),
    )
    sessions.insert_session(
        conn,
        sessions.SessionRow(
            student_id=_STUDENT_ID,
            session_id=_SESSION_ID,
            assignment_id="a-1",
            started_at="2026-08-13T08:05:00+00:00",
        ),
    )
    for i, (cause, detail) in enumerate(_ROWS_IN_ORDER, start=1):
        problem_id = str(i)
        captures.insert_problem(
            conn,
            captures.ProblemRow(
                student_id=_STUDENT_ID,
                capture_id=_CAPTURE_ID,
                problem_id=problem_id,
                prompt_text=f"problem {problem_id}",
                student_answer_raw="x",
                transcription_confidence=0.99,
            ),
        )
        sessions.insert_graded_problem(
            conn,
            sessions.GradedProblemRow(
                student_id=_STUDENT_ID,
                session_id=_SESSION_ID,
                capture_id=_CAPTURE_ID,
                problem_id=problem_id,
                outcome="needs_human",
                grader_confidence=0.99,
                needs_human_cause=cause.value,
                needs_human_detail=detail,
            ),
        )


def test_all_six_needs_human_causes_render_distinct_messages(
    page: Page, web_server: LiveServer
) -> None:
    _seed_session_with_all_six_causes(web_server.connection())

    page.goto(f"{web_server.base_url}/session/{_STUDENT_ID}/{_SESSION_ID}")

    notes = page.locator(".outcome-note")
    expect(notes).to_have_count(6)
    texts = notes.all_text_contents()
    # Six causes, six genuinely different messages -- not the same string twice
    # or a generic fallback repeated across several of them.
    assert len(set(texts)) == 6, f"expected 6 distinct messages, got {texts}"
    assert any("Day" in t and "Section" in t for t in texts)
