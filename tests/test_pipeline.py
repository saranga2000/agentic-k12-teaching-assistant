"""Pipeline orchestration: capture -> ingest -> transcribe -> grade -> persist.

No test here hits the network — every transcribe call goes through FakeTranscriber.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from k12ta.config import Settings
from k12ta.grading.needs_human import NeedsHumanCause
from k12ta.grading.page_identity import build_composite_key
from k12ta.llm.base import DataRetention
from k12ta.pipeline.process import (
    PipelineStatus,
    process_capture,
    regrade_capture_for_resolved_identity,
    replay_source,
)
from k12ta.store import (
    answer_keys,
    captures,
    content,
    db,
    key_page_images,
    migrate,
    page_identities,
    page_identity_resolutions,
    page_identity_schemas,
    quota,
    sessions,
    students,
)
from k12ta.transcribe.base import (
    FailureKind,
    PageIdentityExtraction,
    TranscribedItem,
    TranscriptionResult,
)
from tests.fakes import FakeTextModel, FakeTranscriber, FakeVisionModel

TODAY = date.today()
"""`process_capture` calls `date.today()` internally (it has no injectable clock), so
this must track the real date rather than a fixed one -- a hardcoded past date only
matches by coincidence on the day it was written and silently breaks the next day."""


def _migrated_connection(path: str = ":memory:") -> sqlite3.Connection:
    conn = db.connect(path)
    migrate.apply_migrations(conn)
    return conn


def _settings(
    tmp_path: Path,
    daily_request_limit: int = 20,
    evaluator_enabled: bool = False,
    evaluator_mark_wrong_enabled: bool = False,
) -> Settings:
    return Settings(
        llm_provider="anthropic",
        llm_api_key="",
        llm_model="",
        llm_max_requests_per_run=40,
        data_dir=tmp_path,
        coach_name="Coach",
        daily_token_budget_usd=Decimal("1.50"),
        daily_request_limit=daily_request_limit,
        log_level="INFO",
        evaluator_enabled=evaluator_enabled,
        evaluator_mark_wrong_enabled=evaluator_mark_wrong_enabled,
    )


def _seed_student_with_source(conn: sqlite3.Connection, student_id: str) -> str:
    """Seed a student, a content source (deliberately without any key content, since
    none exists anywhere yet), and today's assignment. Returns the assignment_id."""
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Jahnvi",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    assignment_id = "summer_bridge:2026-08-12"
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id=student_id,
            assignment_id=assignment_id,
            source_id="summer_bridge",
            created_at=TODAY.isoformat(),
        ),
    )
    return assignment_id


def _seed_student_with_day_schema_source(conn: sqlite3.Connection, student_id: str) -> str:
    """Same as `_seed_student_with_source`, but the content source has a
    single-component identity schema ("day") -- the shape auto-resolution needs
    to have anything to resolve against at all."""
    assignment_id = _seed_student_with_source(conn, student_id)
    page_identity_schemas.save_new_schema(
        conn, student_id, "summer_bridge", [("day", "Day", "Day 5")]
    )
    return assignment_id


def _seed_student_with_section_and_day_schema_source(
    conn: sqlite3.Connection, student_id: str
) -> str:
    """Same as above, but with the two-component composite schema -- Summer
    Bridge's actual real shape (section + day), the whole reason a single-value
    schema isn't safe to assume."""
    assignment_id = _seed_student_with_source(conn, student_id)
    page_identity_schemas.save_new_schema(
        conn,
        student_id,
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )
    return assignment_id


def _success_result(*confidences: float) -> TranscriptionResult:
    items = tuple(
        TranscribedItem(
            problem_id=str(i + 1),
            prompt_text=f"problem {i + 1}",
            student_answer_raw="42",
            confidence=confidence,
        )
        for i, confidence in enumerate(confidences)
    )
    return TranscriptionResult(
        items=items,
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def _failure_result(kind: FailureKind) -> TranscriptionResult:
    return TranscriptionResult(
        items=(),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=200,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        failure=f"simulated {kind.value}",
        failure_kind=kind,
    )


def test_successful_transcribe_persists_problems_and_needs_human_graded_rows(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_success_result(0.99, 0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    assert outcome.session_id is not None
    assert transcriber.request_count == 1

    session = sessions.get_session(conn, student_id, outcome.session_id)
    assert session is not None
    assert session.assignment_id == assignment_id

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert len(graded) == 2
    assert all(g.outcome == "needs_human" for g in graded)
    assert all(g.expected_answer is None for g in graded)
    # No page number was supplied, so the honest cause is "could not tell which page
    # this is", never a guessed page or an invented key reason.
    assert all(g.needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value for g in graded)

    quota_count = quota.get_count(conn, TODAY)
    assert quota_count == 1

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", (student_id,))
    capture_row = captures.get_page_capture(conn, student_id, cur.fetchone()[0])
    assert capture_row is not None
    assert capture_row.transcribe_failure_reason is None


def test_blank_or_duplicate_problem_id_escalates_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """Found 2026-08-20 on real data: two items sharing a blank problem_id
    crashed process_capture outright (a UNIQUE constraint violation on
    problems, raised from inside the capture worker thread). Every ambiguous
    item -- blank, or a repeat of another item's problem_id -- must instead
    escalate to NEEDS_HUMAN/AMBIGUOUS_PROBLEM_ID, storable without collision,
    losing nothing. A normal, uniquely-identified item on the same photo is
    unaffected."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="19", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="", prompt_text="q2", student_answer_raw="a1", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="", prompt_text="q3", student_answer_raw="a2", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="3", prompt_text="q4", student_answer_raw="a3", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="3", prompt_text="q5", student_answer_raw="a4", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    assert outcome.session_id is not None

    capture_id = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)[
        0
    ].capture_id
    stored_problems = captures.list_problems_for_capture(conn, student_id, capture_id)
    assert len(stored_problems) == 5  # nothing dropped

    graded_by_problem_id = {
        g.problem_id: g
        for g in sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    }
    assert len(graded_by_problem_id) == 5  # each stored problem_id is unique, nothing collided

    # The one unambiguous item is unaffected -- unknown_page, same as any other
    # item on a photo with no resolvable identity, never ambiguous_problem_id.
    assert graded_by_problem_id["1"].needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value
    ambiguous = [g for pid, g in graded_by_problem_id.items() if pid != "1"]
    assert len(ambiguous) == 4
    assert all(g.needs_human_cause == NeedsHumanCause.AMBIGUOUS_PROBLEM_ID.value for g in ambiguous)


def test_quota_already_exhausted_never_calls_the_transcriber(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path, daily_request_limit=1)
    quota.record_request(conn, TODAY)  # already at the limit
    transcriber = FakeTranscriber(result=_success_result(0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.QUOTA_EXHAUSTED
    assert transcriber.calls == []
    assert quota.get_count(conn, TODAY) == 1  # unchanged, not incremented further

    cur = conn.execute("SELECT COUNT(*) FROM page_captures WHERE student_id = ?", (student_id,))
    assert cur.fetchone()[0] == 0


def test_transcriber_construction_failure_degrades_gracefully(tmp_path: Path) -> None:
    """get_transcriber is a factory precisely so building a live adapter can wait
    until after the quota gate passes -- and a broken provider config (a bad
    K12TA_LLM_PROVIDER, a missing key) must never surface as an unhandled 500 to a
    student who was just trying to submit a photo."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)

    def broken_factory() -> FakeTranscriber:
        raise ValueError("unsupported LLM provider: 'anthropic'")

    outcome = process_capture(
        conn, settings, broken_factory, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.TRANSCRIBE_FAILED
    assert outcome.failure_reason is not None
    assert "unsupported LLM provider" in outcome.failure_reason
    # The photo was still preserved -- construction failing is a transcribe failure,
    # not a quota-exhausted one, and follows the same "preserve the photo" rule.
    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", (student_id,))
    row = cur.fetchone()
    assert row is not None
    capture_row = captures.get_page_capture(conn, student_id, row[0])
    assert capture_row is not None
    # Diagnosable after the fact, not only in a log line that doesn't survive a restart.
    assert capture_row.transcribe_failure_reason == outcome.failure_reason


def test_transcribe_failure_preserves_the_photo_but_persists_nothing_else(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_failure_result(FailureKind.UNREADABLE))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.TRANSCRIBE_FAILED
    assert outcome.session_id is None
    assert quota.get_count(conn, TODAY) == 1  # the attempt still counted

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", (student_id,))
    row = cur.fetchone()
    assert row is not None  # the photo itself was preserved
    capture_row = captures.get_page_capture(conn, student_id, row[0])
    assert capture_row is not None
    assert Path(capture_row.image_path).exists()
    # Diagnosable after the fact, not only in a log line that doesn't survive a restart.
    assert capture_row.transcribe_failure_reason == "simulated unreadable"

    assert captures.list_problems_for_capture(conn, student_id, row[0]) == []
    cur = conn.execute("SELECT COUNT(*) FROM sessions WHERE student_id = ?", (student_id,))
    assert cur.fetchone()[0] == 0


def test_provider_rate_limit_is_a_distinct_outcome_from_an_ordinary_transcribe_failure(
    tmp_path: Path,
) -> None:
    """A real 429 exhausting VisionLLMTranscriber's own retry budget (surfaced
    as FailureKind.RATE_LIMITED, never a raised exception -- see k12ta.
    transcribe.base.Transcriber's "must not raise" contract) is not a
    transcription problem: the photo may be perfectly legible, the provider
    is just out of capacity. Conflating it with "I couldn't read this one"
    hid a whole week's worth of real rate-limiting behind a message that
    sounded like a photo-quality issue."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_failure_result(FailureKind.RATE_LIMITED))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.RATE_LIMITED
    assert outcome.status is not PipelineStatus.TRANSCRIBE_FAILED
    assert outcome.session_id is None
    assert quota.get_count(conn, TODAY) == 1  # the attempt still counted

    cur = conn.execute("SELECT capture_id FROM page_captures WHERE student_id = ?", (student_id,))
    row = cur.fetchone()
    assert row is not None
    capture_row = captures.get_page_capture(conn, student_id, row[0])
    assert capture_row is not None
    # Its own persisted reason, not the ordinary transcribe-failure column --
    # a query can tell the two apart without parsing free text.
    assert capture_row.rate_limited_reason == "simulated rate_limited"
    assert capture_row.transcribe_failure_reason is None


def test_daily_counter_survives_a_simulated_server_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "pipeline-test.db")
    settings = _settings(tmp_path, daily_request_limit=1)

    first_conn = _migrated_connection(db_path)
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(first_conn, student_id)
    first_transcriber = FakeTranscriber(result=_success_result(0.99))
    first_outcome = process_capture(
        first_conn, settings, lambda: first_transcriber, student_id, assignment_id, b"photo-one"
    )
    assert first_outcome.status is PipelineStatus.GRADED
    first_conn.close()

    # A fresh connection to the same file, simulating a server restart.
    second_conn = _migrated_connection(db_path)
    second_transcriber = FakeTranscriber(result=_success_result(0.99))
    second_outcome = process_capture(
        second_conn, settings, lambda: second_transcriber, student_id, assignment_id, b"photo-two"
    )

    assert second_outcome.status is PipelineStatus.QUOTA_EXHAUSTED
    assert second_transcriber.calls == []


def _seed_key_entries(
    conn: sqlite3.Connection, student_id: str, source_id: str, page_number: int
) -> None:
    """Persist key entries through the same store the parent confirm gate writes
    (k12ta.store.answer_keys.upsert_entry), not by raw INSERT -- the pipeline under
    test here is the capture/grade path, and its key lookup must read the real store."""
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id=source_id,
            page_number=page_number,
            problem_number="1",
            answer_text="42",
            ungradeable_reason=None,
            confirmed_at="2026-08-12T00:00:00+00:00",
        ),
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id=source_id,
            page_number=page_number,
            problem_number="2",
            answer_text=None,
            ungradeable_reason="answers_vary",
            confirmed_at="2026-08-12T00:00:00+00:00",
        ),
    )


def test_keyed_page_grades_correct_incorrect_and_honest_causes(
    tmp_path: Path,
) -> None:
    """With a page number and a real key page persisted, the pipeline grades against
    the key: a match is CORRECT, an ungradeable-key item is NEEDS_PERSON, and an item
    whose number has no key entry on that page is NO_KEY_FOR_PAGE."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=5)
    settings = _settings(tmp_path)
    # problem 1 -> "42" (matches key -> CORRECT)
    # problem 2 -> ungradeable key entry ("answers_vary" -> NEEDS_PERSON)
    # problem 3 -> no key entry on page 5 (-> NO_KEY_FOR_PAGE)
    items = (
        TranscribedItem(problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99),
        TranscribedItem(problem_id="2", prompt_text="q2", student_answer_raw="x", confidence=0.99),
        TranscribedItem(problem_id="3", prompt_text="q3", student_answer_raw="y", confidence=0.99),
    )
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=items,
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        )
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    by_problem = {g.problem_id: g for g in graded}

    assert by_problem["1"].outcome == "correct"
    assert by_problem["1"].expected_answer == "42"
    assert by_problem["1"].needs_human_cause is None

    assert by_problem["2"].outcome == "needs_human"
    assert by_problem["2"].needs_human_cause == NeedsHumanCause.NEEDS_PERSON.value

    assert by_problem["3"].outcome == "needs_human"
    assert by_problem["3"].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value


def test_low_confidence_is_its_own_cause_even_when_a_key_exists(
    tmp_path: Path,
) -> None:
    """A readable-but-low-confidence transcription must never become a confident
    grade just because a key exists -- it is its own honest cause."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=5)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.4
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        )
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.LOW_CONFIDENCE.value


def test_capture_with_no_manual_page_number_resolves_via_page_identity(
    tmp_path: Path,
) -> None:
    """Scope B, the whole point: a student capture with no manual page_number now
    resolves against a confirmed key-scan composite automatically, using
    whatever the model read off this same photo -- closing the gap Scope A's
    proof left open."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_day_schema_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=5)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            composite_key=build_composite_key(["Day 3"]),
            schema_version=1,
            confirmed_at="2026-08-12T00:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 3",)}, confidence=0.97),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].expected_answer == "42"

    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {"resolved": 1}
    # The pipeline loaded the source's real schema and passed it through, so the
    # model was told exactly which component to look for -- not left guessing.
    assert transcriber.identity_schemas_seen == [(("day", "Day 5"),)]


def test_capture_resolves_a_composite_schema_where_a_single_component_would_be_ambiguous(
    tmp_path: Path,
) -> None:
    """The exact bug that started this redesign: "Day 1" alone is not globally
    unique when day numbering resets per section. The composite disambiguates."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=89)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=89,
            composite_key=build_composite_key(["Section 2", "Day 1"]),
            schema_version=1,
            confirmed_at="2026-08-12T00:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(
                candidates={"section": ("Section 2",), "day": ("Day 1",)}, confidence=0.97
            ),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"


def test_capture_with_conflicting_page_markers_refuses_every_item(
    tmp_path: Path,
) -> None:
    """Two different day banners on one photo (a two-page spread) must refuse
    outright -- never pick one -- and every item on that photo gets the distinct
    CONFLICTING_PAGE_MARKERS cause, not the generic UNKNOWN_PAGE."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_day_schema_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="2", prompt_text="q2", student_answer_raw="7", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(
                candidates={"day": ("Day 2", "Day 3")}, confidence=0.95
            ),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert len(graded) == 2
    assert all(g.outcome == "needs_human" for g in graded)
    assert all(
        g.needs_human_cause == NeedsHumanCause.CONFLICTING_PAGE_MARKERS.value for g in graded
    )

    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {"conflicting": 1}


def test_capture_with_one_of_two_components_missing_is_partial_page_markers(
    tmp_path: Path,
) -> None:
    """Recoverable by re-photographing with the missing part in frame -- its own
    cause, with the specific components seen/missing recorded so the message can
    say so ("I can see the day but not the section"), not the generic
    UNKNOWN_PAGE."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.PARTIAL_PAGE_MARKERS.value
    assert graded[0].needs_human_detail is not None
    detail = json.loads(graded[0].needs_human_detail)
    assert detail == {"seen": ["Day"], "missing": ["Section"]}

    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {"partial": 1}


def test_partial_auto_resolves_when_only_one_section_has_ever_been_confirmed(
    tmp_path: Path,
) -> None:
    """The deduction case: Day 5 is read, Section isn't, and Section 1 is the
    only section this source has ever had a confirmed mapping for -- grades
    normally, as if RESOLVED, no prompt needed."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=21)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=21,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].page_number == 21
    assert graded[0].needs_human_cause is None  # graded normally, not needs-human at all


def test_partial_asks_and_stores_seen_values_when_another_section_is_known(
    tmp_path: Path,
) -> None:
    """The limit that matters: Day 5 has only ever been confirmed under
    Section 1, but Section 2 is known to exist (confirmed for a different
    day) -- must not auto-resolve. Stays PARTIAL_PAGE_MARKERS (no code to
    grade with yet), but seen_values_json is stored so the pick screen and
    a later pick submission can re-derive fresh candidates without asking
    the model again."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=21,
            composite_key=build_composite_key(["Section 1", "Day 5"]),
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=71,
            composite_key=build_composite_key(["Section 2", "Day 6"]),
            schema_version=1,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].needs_human_cause == NeedsHumanCause.PARTIAL_PAGE_MARKERS.value
    assert graded[0].page_number is None

    capture_id = graded[0].capture_id
    seen_json = page_identity_resolutions.get_seen_values_for_capture(conn, student_id, capture_id)
    assert seen_json is not None
    assert json.loads(seen_json) == {"day": "Day 5"}


def test_keyed_page_marks_an_unreduced_fraction_correct_with_a_note(tmp_path: Path) -> None:
    """ "2/6" against a key of "1/3" is numerically right but not in lowest
    terms -- CORRECT (not INCORRECT, not escalated to a parent), flagged
    unsimplified so the render layer can say so instead of looking identical
    to a fully-reduced answer. See k12ta.grading.needs_human.decide."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=21,
            problem_number="1",
            answer_text="1/3",
            ungradeable_reason=None,
            confirmed_at="2026-08-19T00:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    items = (
        TranscribedItem(
            problem_id="1", prompt_text="probability?", student_answer_raw="2/6", confidence=0.99
        ),
    )
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=items,
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        )
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=21,
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].needs_human_cause is None
    assert graded[0].unsimplified is True


def test_regrade_capture_for_resolved_identity_grades_every_problem_from_the_capture(
    tmp_path: Path,
) -> None:
    """The shared regrade path: once a capture's page identity is known --
    whether from a student's pick or a parent's later key entry -- every
    problem transcribed from that capture is re-decided using the already-
    stored transcription, never re-sent to the model."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="19", confidence=0.99
                ),
                TranscribedItem(
                    problem_id="2", prompt_text="q2", student_answer_raw="wrong", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )
    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )
    graded_before = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert {g.outcome for g in graded_before} == {"needs_human"}
    capture_id = graded_before[0].capture_id

    # The key for the page this capture turns out to be arrives afterward.
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=21,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=21,
            problem_number="2",
            answer_text="42",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )

    regrade_capture_for_resolved_identity(
        conn, student_id, outcome.session_id, capture_id, "summer_bridge", page_number=21
    )

    graded_after = {
        g.problem_id: g
        for g in sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    }
    assert graded_after["1"].outcome == "correct"
    assert graded_after["1"].page_number == 21
    assert graded_after["1"].needs_human_cause is None
    assert graded_after["2"].outcome == "incorrect"
    assert graded_after["2"].expected_answer == "42"
    assert transcriber.request_count == 1  # never called again


def test_regrade_capture_for_resolved_identity_auto_confirms_a_pages_first_attempt(
    tmp_path: Path,
) -> None:
    """docs/ROADMAP.md's V1 "Attempts": a capture resolving to a page for
    the first time -- via a pick or a parent's later key entry, same as
    above -- has nothing to confirm redoing, so it must not get stuck
    withholding its own grade forever waiting for a confirmation that will
    never come."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="19", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )
    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )
    graded_before = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    capture_id = graded_before[0].capture_id
    before = captures.get_page_capture(conn, student_id, capture_id)
    assert before is not None
    assert before.resubmit_confirmed_at is None  # unresolved identity, nothing to confirm yet

    regrade_capture_for_resolved_identity(
        conn, student_id, outcome.session_id, capture_id, "summer_bridge", page_number=21
    )

    after = captures.get_page_capture(conn, student_id, capture_id)
    assert after is not None
    assert after.resubmit_confirmed_at is not None


def test_regrade_capture_for_resolved_identity_can_still_land_on_needs_human(
    tmp_path: Path,
) -> None:
    """Resolving identity does not guarantee a key exists for the resolved
    page -- the honest NO_KEY_FOR_PAGE cause, not a guess, when it doesn't."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_section_and_day_schema_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="19", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(candidates={"day": ("Day 5",)}, confidence=0.97),
        )
    )
    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )
    capture_id = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)[
        0
    ].capture_id

    regrade_capture_for_resolved_identity(
        conn, student_id, outcome.session_id, capture_id, "summer_bridge", page_number=21
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value
    assert graded[0].page_number == 21


def test_replay_source_regrades_every_resolved_capture_from_stored_transcription(
    tmp_path: Path,
) -> None:
    """The regression-corpus driver: once a real photo's page identity is
    known, replay_source re-decides it against whatever the answer key says
    *right now* -- no re-transcription, no model call -- so a key correction
    or a decide() change can be checked against every real capture on file in
    seconds instead of re-ingesting photos and spending quota again."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="19", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
        )
    )

    # Two real captures, resolved to two different pages, both before any key exists.
    outcome_a = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"a", page_number=13
    )
    outcome_b = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"b", page_number=15
    )
    for outcome in (outcome_a, outcome_b):
        graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
        assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value

    # The key arrives afterward, same as a parent scanning it days later.
    for page_number in (13, 15):
        answer_keys.upsert_entry(
            conn,
            answer_keys.AnswerKeyEntryRow(
                student_id=student_id,
                source_id="summer_bridge",
                page_number=page_number,
                problem_number="1",
                answer_text="19",
                ungradeable_reason=None,
                confirmed_at="2026-08-14T08:00:00+00:00",
            ),
        )

    summary = replay_source(conn, student_id, "summer_bridge")

    assert summary.captures_replayed == 2
    for outcome in (outcome_a, outcome_b):
        graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
        assert graded[0].outcome == "correct"
    assert transcriber.request_count == 2  # never called again during replay


def test_manual_page_number_override_skips_auto_resolution_entirely(
    tmp_path: Path,
) -> None:
    """A caller-supplied page_number (tests, the Scope A demo path) must win
    outright and never be second-guessed by this photo's own identity
    extraction -- and, since resolution never runs, nothing is logged to
    page_identity_resolutions for it."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_day_schema_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=5)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            # Conflicting candidates the auto-resolve path would refuse on --
            # irrelevant here because the manual override bypasses resolution.
            page_identity=PageIdentityExtraction(
                candidates={"day": ("Day 2", "Day 3")}, confidence=0.95
            ),
        )
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"

    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {}


def test_capture_of_a_page_with_no_markers_at_all_refuses_honestly_as_unknown_page(
    tmp_path: Path,
) -> None:
    """The source has a schema and confirmed mappings exist for other days -- but
    this one photo (front matter, like the real "SECTION 1" pages that precede
    "Day 1" in the actual workbook) shows no identity markers at all. That must
    resolve as NO_MARKERS and fall through to the same honest UNKNOWN_PAGE cause
    as "no page number supplied," never something else -- a schema being
    configured must not change what an absent marker means."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_day_schema_source(conn, student_id)
    _seed_key_entries(conn, student_id, "summer_bridge", page_number=5)
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            composite_key=build_composite_key(["Day 3"]),
            schema_version=1,
            confirmed_at="2026-08-12T00:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            # No day key at all -- the model saw nothing to report, not even an
            # empty list for it. Front matter has no banner to read.
            page_identity=PageIdentityExtraction(candidates={}, confidence=0.0),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    assert outcome.status is PipelineStatus.GRADED
    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value


def test_capture_for_a_source_with_no_schema_at_all_refuses_as_unknown_page(
    tmp_path: Path,
) -> None:
    """A source nobody has taught an identity schema to yet -- NO_SCHEMA, same
    honest UNKNOWN_PAGE fallthrough. This source may legitimately never
    auto-resolve, by design."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # no schema
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_success_result(0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value
    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {"no_schema": 1}


def test_no_schema_with_nothing_extracted_persists_no_guess(tmp_path: Path) -> None:
    """The ordinary case above, checked explicitly against Gap O's own new
    field: a plain success result with no identity_values has nothing for
    k12ta.web.app's bootstrap-schema ask to offer, and must not pretend
    otherwise."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # no schema
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_success_result(0.99))

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    seen_json = page_identity_resolutions.get_seen_values_for_capture(
        conn, student_id, graded[0].capture_id
    )
    assert seen_json is None


def test_no_schema_with_something_extracted_persists_the_guess_for_the_bootstrap_ask(
    tmp_path: Path,
) -> None:
    """Gap O (docs/USER_WORKFLOWS.md): a brand-new program's first photo
    still resolves to UNKNOWN_PAGE (nothing to grade against yet -- schema
    bootstrapping never guesses at a grade), but whatever the model found is
    now kept for k12ta.web.app's SchemaGuessAsk to build from, the same
    persisted field PARTIAL_PAGE_MARKERS already uses for its own ask."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # no schema
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(
        result=TranscriptionResult(
            items=(
                TranscribedItem(
                    problem_id="1", prompt_text="q1", student_answer_raw="42", confidence=0.99
                ),
            ),
            provider="google",
            model="gemini-3.7-flash",
            cost_usd=0.0,
            latency_ms=500,
            data_retention=DataRetention.PROVIDER_MAY_TRAIN,
            page_identity=PageIdentityExtraction(
                candidates={"chapter": ("CH.4",), "printed_page": ("13", "13")}, confidence=0.9
            ),
        )
    )

    outcome = process_capture(
        conn, settings, lambda: transcriber, student_id, assignment_id, b"fake-jpeg-bytes"
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].needs_human_cause == NeedsHumanCause.UNKNOWN_PAGE.value
    counts = page_identity_resolutions.count_outcomes_for_source(conn, student_id, "summer_bridge")
    assert counts == {"no_schema": 1}
    seen_json = page_identity_resolutions.get_seen_values_for_capture(
        conn, student_id, graded[0].capture_id
    )
    assert seen_json is not None
    assert json.loads(seen_json) == {"chapter": "CH.4", "printed_page": "13"}


# --- M6: the agentic evaluator, wired behind a flag (docs/ROADMAP.md) -------


def _seed_keyless_source(conn: sqlite3.Connection, student_id: str) -> str:
    """A source explicitly configured keyless (has_answer_key=False) -- V1's
    core capability, not a bridge until a key arrives."""
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name="Jahnvi",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id=student_id,
            source_id="rsm",
            label="RSM",
            kind="worksheet_packet",
            subject="math",
            has_answer_key=False,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
        ),
    )
    assignment_id = "rsm:2026-08-12"
    content.insert_assignment(
        conn,
        content.AssignmentRow(
            student_id=student_id,
            assignment_id=assignment_id,
            source_id="rsm",
            created_at=TODAY.isoformat(),
        ),
    )
    return assignment_id


def _one_item_transcription(answer: str = "19") -> TranscriptionResult:
    return TranscriptionResult(
        items=(
            TranscribedItem(
                problem_id="1",
                prompt_text="Solve for x: 2x + 5 = 43",
                student_answer_raw=answer,
                confidence=0.99,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def test_evaluator_disabled_by_default_leaves_a_keyless_page_as_needs_human(
    tmp_path: Path,
) -> None:
    """The literal safety requirement: shipping this code changes nothing
    about what a real deployment does today. Default settings, a text model
    is available but must never be reached."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path)  # evaluator_enabled=False, the default
    transcriber = FakeTranscriber(result=_one_item_transcription())
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.9}'])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value
    assert text_model.request_count == 0  # never called at all


def test_evaluator_enabled_grades_a_keyless_page_correct(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "correct", "confidence": 0.9, "generated_answer": "19"}',
            '{"verdict": "correct", "confidence": 0.85, "generated_answer": "19"}',
        ]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert graded[0].needs_human_cause is None
    assert text_model.request_count == 2  # two independent solves, agreement-gated


def test_evaluator_incorrect_verdict_stays_needs_human_until_mark_wrong_is_enabled(
    tmp_path: Path,
) -> None:
    """The other, more important half of M6's flag requirement: every keyless
    INCORRECT reaches a parent before the child, regardless of the
    evaluator's own confidence, until a real precision number exists."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)  # mark_wrong stays False
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="wrong guess"))
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "incorrect", "confidence": 0.95, "generated_answer": "19"}',
            '{"verdict": "incorrect", "confidence": 0.95, "generated_answer": "19"}',
        ]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"


def test_evaluator_incorrect_verdict_reaches_the_child_once_mark_wrong_is_enabled(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="wrong guess"))
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "incorrect", "confidence": 0.95, "generated_answer": "19"}',
            '{"verdict": "incorrect", "confidence": 0.95, "generated_answer": "19"}',
        ]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "incorrect"


def test_evaluator_never_fires_for_a_keyed_source_still_waiting_on_its_key(
    tmp_path: Path,
) -> None:
    """The critical safety boundary: NO_KEY_FOR_PAGE on a KEYED source means
    "a parent hasn't scanned the key yet," not "no key exists" -- the system
    must never invent an answer here, per V1's own "keyed" definition
    (docs/ROADMAP.md). Only a genuinely keyless source may reach the
    evaluator for this cause."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # has_answer_key=True
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.9}'])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value
    assert text_model.request_count == 0


def test_evaluator_resolves_a_keyed_mismatch_that_is_actually_the_same_answer(
    tmp_path: Path,
) -> None:
    """The permanent fix for this system's first four grades at 50% unjust:
    "rhombus" against a key of "quadrilateral"."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # has_answer_key=True
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="quadrilateral",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="rhombus"))
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.9}'])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert text_model.request_count == 1  # one call, no cross-check needed -- a key exists


def test_evaluator_enabled_but_no_text_model_factory_stays_needs_human(tmp_path: Path) -> None:
    """get_text_model is optional (every existing caller passes nothing) --
    the flag alone must never crash a caller that hasn't wired a model in."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription())

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value


# --- M6 tier 3 (vision), wired into the pipeline -----------------------------


def _vision_reply(
    verdict: str,
    confidence: float,
    read_answer: str | None = None,
    read_confidence: float = 0.9,
    generated_answer: str | None = None,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "confidence": confidence,
            "read_answer": read_answer,
            "read_confidence": read_confidence,
            "generated_answer": generated_answer,
        }
    )


def test_low_tier2_confidence_escalates_to_vision_and_uses_its_verdict(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # has_answer_key=True
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="quadrilateral",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="rhombus"))
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.4}'])
    vision_model = FakeVisionModel(replies=[_vision_reply("correct", 0.9, read_answer="rhombus")])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert vision_model.request_count == 1


def test_no_vision_model_factory_means_no_escalation_even_at_low_confidence(
    tmp_path: Path,
) -> None:
    """get_vision_model is optional (every existing caller passes nothing) --
    tier 2's own result stands, exactly as it did before tier 3 existed."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="quadrilateral",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="rhombus"))
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.2}'])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"  # tier 2's own low-confidence verdict stands


def test_vision_read_answer_replaces_the_stored_transcription(tmp_path: Path) -> None:
    """docs/ARCHITECTURE.md: tier 3 fuses transcription and evaluation into
    one call rather than skipping the record a parent reviews."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="l9"))  # misread "19"
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "needs_human", "confidence": 0.0}',
            '{"verdict": "needs_human", "confidence": 0.0}',
        ]
    )
    vision_model = FakeVisionModel(
        replies=[_vision_reply("correct", 0.95, read_answer="19", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    capture_id = graded[0].capture_id
    problems = captures.list_problems_for_capture(conn, student_id, capture_id)
    assert problems[0].student_answer_raw == "19"


def test_vision_incorrect_verdict_stays_needs_human_until_mark_wrong_is_enabled(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)  # mark_wrong stays False
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="21"))
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "needs_human", "confidence": 0.0}',
            '{"verdict": "needs_human", "confidence": 0.0}',
        ]
    )
    vision_model = FakeVisionModel(
        replies=[_vision_reply("incorrect", 0.95, read_answer="21", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"


def test_vision_sends_the_key_image_when_one_is_on_file(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="quadrilateral",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    key_image_path = tmp_path / "key-page-5.jpg"
    key_image_path.write_bytes(b"a real key page photo")
    key_page_images.upsert_image(
        conn,
        key_page_images.KeyPageImageRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            image_path=str(key_image_path),
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="rhombus"))
    text_model = FakeTextModel(replies=['{"verdict": "correct", "confidence": 0.4}'])
    vision_model = FakeVisionModel(replies=[_vision_reply("correct", 0.9, read_answer="rhombus")])

    process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    assert vision_model.seen_image_counts == [2]


def test_vision_sends_only_the_page_image_when_no_key_image_is_on_file(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
    text_model = FakeTextModel(
        replies=[
            '{"verdict": "needs_human", "confidence": 0.0}',
            '{"verdict": "needs_human", "confidence": 0.0}',
        ]
    )
    vision_model = FakeVisionModel(replies=[_vision_reply("correct", 0.9, read_answer="19")])

    process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    assert vision_model.seen_image_counts == [1]


# --- M6 tier 3's LOW_CONFIDENCE rescue path ----------------------------------


def _low_confidence_transcription(
    answer: str = "19", confidence: float = 0.5
) -> TranscriptionResult:
    return TranscriptionResult(
        items=(
            TranscribedItem(
                problem_id="1",
                prompt_text="Solve for x: 2x + 5 = 43",
                student_answer_raw=answer,
                confidence=confidence,
            ),
        ),
        provider="google",
        model="gemini-3.7-flash",
        cost_usd=0.0,
        latency_ms=500,
        data_retention=DataRetention.PROVIDER_MAY_TRAIN,
    )


def test_low_confidence_keyless_source_rescued_by_vision(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="l9"))
    text_model = FakeTextModel(replies=[])  # never called for LOW_CONFIDENCE
    vision_model = FakeVisionModel(
        replies=[_vision_reply("correct", 0.95, read_answer="19", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert text_model.request_count == 0
    assert vision_model.request_count == 1
    capture_id = graded[0].capture_id
    problems = captures.list_problems_for_capture(conn, student_id, capture_id)
    assert problems[0].student_answer_raw == "19"


def test_low_confidence_no_vision_model_stays_low_confidence(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="l9"))
    text_model = FakeTextModel(replies=[])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.LOW_CONFIDENCE.value


def test_low_confidence_keyed_source_with_no_key_only_corrects_transcription(
    tmp_path: Path,
) -> None:
    """The keyed-source safety boundary, entered from LOW_CONFIDENCE instead
    of NO_KEY_FOR_PAGE -- vision may improve the reading, but must never
    invent a verdict for a source the parent said they'd supply answers
    for."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # has_answer_key=True, no key yet
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="l9"))
    text_model = FakeTextModel(replies=[])
    vision_model = FakeVisionModel(
        replies=[_vision_reply("correct", 0.95, read_answer="19", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NO_KEY_FOR_PAGE.value
    capture_id = graded[0].capture_id
    problems = captures.list_problems_for_capture(conn, student_id, capture_id)
    assert problems[0].student_answer_raw == "19"  # transcription still improved


def test_low_confidence_keyed_source_with_ungradeable_key_only_corrects_transcription(
    tmp_path: Path,
) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text=None,
            ungradeable_reason="answers_vary",
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="l9"))
    text_model = FakeTextModel(replies=[])
    vision_model = FakeVisionModel(
        replies=[_vision_reply("correct", 0.95, read_answer="19", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.NEEDS_PERSON.value


def test_low_confidence_keyed_source_with_a_real_key_judges_against_it(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="quadrilateral",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path, evaluator_enabled=True)
    transcriber = FakeTranscriber(
        result=_low_confidence_transcription(answer="rombus")  # misread "rhombus"
    )
    text_model = FakeTextModel(replies=[])
    vision_model = FakeVisionModel(replies=[_vision_reply("correct", 0.95, read_answer="rhombus")])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"
    assert vision_model.seen_prompts[0].count("quadrilateral") >= 1
    assert "do not solve the problem yourself" in vision_model.seen_prompts[0].lower()


def test_low_confidence_vision_still_cannot_tell_stays_low_confidence(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True, evaluator_mark_wrong_enabled=True)
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="???"))
    text_model = FakeTextModel(replies=[])
    vision_model = FakeVisionModel(replies=[_vision_reply("needs_human", 0.0)])

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.LOW_CONFIDENCE.value


def test_low_confidence_incorrect_gated_behind_mark_wrong(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path, evaluator_enabled=True)  # mark_wrong stays False
    transcriber = FakeTranscriber(result=_low_confidence_transcription(answer="21"))
    text_model = FakeTextModel(replies=[])
    vision_model = FakeVisionModel(
        replies=[_vision_reply("incorrect", 0.95, read_answer="21", generated_answer="19")]
    )

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
        get_text_model=lambda: text_model,
        get_vision_model=lambda: vision_model,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.LOW_CONFIDENCE.value
    capture_id = graded[0].capture_id
    problems = captures.list_problems_for_capture(conn, student_id, capture_id)
    assert problems[0].student_answer_raw == "21"  # transcription still corrected


# --- M7's attempt cap: a fourth photograph of the same page is refused -----


def test_first_three_captures_of_a_page_grade_normally(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)  # has_answer_key=True
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)

    for _ in range(3):
        transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
        outcome = process_capture(
            conn,
            settings,
            lambda t=transcriber: t,
            student_id,
            assignment_id,
            b"fake-jpeg-bytes",
            page_number=5,
        )
        graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
        assert graded[0].outcome == "correct"
        assert graded[0].needs_human_cause is None


def test_a_fourth_capture_of_the_same_page_is_refused(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)

    for _ in range(3):
        transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
        process_capture(
            conn,
            settings,
            lambda t=transcriber: t,
            student_id,
            assignment_id,
            b"fake-jpeg-bytes",
            page_number=5,
        )

    fourth_transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
    outcome = process_capture(
        conn,
        settings,
        lambda: fourth_transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "needs_human"
    assert graded[0].needs_human_cause == NeedsHumanCause.ATTEMPT_CAP_REACHED.value


def test_the_capture_that_reaches_the_cap_still_counts_and_is_not_capped_itself(
    tmp_path: Path,
) -> None:
    """The third capture must grade normally -- it must never be counted
    against itself while its own count_page_attempts query runs."""
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    answer_keys.upsert_entry(
        conn,
        answer_keys.AnswerKeyEntryRow(
            student_id=student_id,
            source_id="summer_bridge",
            page_number=5,
            problem_number="1",
            answer_text="19",
            ungradeable_reason=None,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )
    settings = _settings(tmp_path)

    outcomes = []
    for _ in range(3):
        transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
        outcomes.append(
            process_capture(
                conn,
                settings,
                lambda t=transcriber: t,
                student_id,
                assignment_id,
                b"fake-jpeg-bytes",
                page_number=5,
            )
        )

    third_graded = sessions.list_graded_problems_for_session(
        conn, student_id, outcomes[2].session_id
    )
    assert third_graded[0].outcome == "correct"


def test_a_different_page_is_unaffected_by_another_pages_cap(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_student_with_source(conn, student_id)
    for page_number, answer_text in [(5, "19"), (6, "20")]:
        answer_keys.upsert_entry(
            conn,
            answer_keys.AnswerKeyEntryRow(
                student_id=student_id,
                source_id="summer_bridge",
                page_number=page_number,
                problem_number="1",
                answer_text=answer_text,
                ungradeable_reason=None,
                confirmed_at="2026-08-14T08:00:00+00:00",
            ),
        )
    settings = _settings(tmp_path)

    for _ in range(3):
        transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
        process_capture(
            conn,
            settings,
            lambda t=transcriber: t,
            student_id,
            assignment_id,
            b"fake-jpeg-bytes",
            page_number=5,
        )

    other_page_transcriber = FakeTranscriber(result=_one_item_transcription(answer="20"))
    outcome = process_capture(
        conn,
        settings,
        lambda: other_page_transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=6,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    assert graded[0].outcome == "correct"


# --- M7's deliberate-resubmit confirmation -----------------------------------


def test_a_pages_first_capture_is_auto_confirmed(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path)
    transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))

    outcome = process_capture(
        conn,
        settings,
        lambda: transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    capture = captures.get_page_capture(conn, student_id, graded[0].capture_id)
    assert capture is not None
    assert capture.resubmit_confirmed_at is not None


def test_a_pages_second_capture_starts_unconfirmed(tmp_path: Path) -> None:
    conn = _migrated_connection()
    student_id = "s-jahnvi"
    assignment_id = _seed_keyless_source(conn, student_id)
    settings = _settings(tmp_path)

    first_transcriber = FakeTranscriber(result=_one_item_transcription(answer="18"))
    process_capture(
        conn,
        settings,
        lambda: first_transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    second_transcriber = FakeTranscriber(result=_one_item_transcription(answer="19"))
    outcome = process_capture(
        conn,
        settings,
        lambda: second_transcriber,
        student_id,
        assignment_id,
        b"fake-jpeg-bytes",
        page_number=5,
    )

    graded = sessions.list_graded_problems_for_session(conn, student_id, outcome.session_id)
    capture = captures.get_page_capture(conn, student_id, graded[0].capture_id)
    assert capture is not None
    assert capture.resubmit_confirmed_at is None
