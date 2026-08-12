"""Page captures and the problems transcribed from them.

`skill_ids` and `page_region` are stored as JSON text; the tuple shape is restored on
read so callers never see a raw JSON string.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageCaptureRow:
    student_id: str
    capture_id: str
    assignment_id: str
    captured_at: str
    image_path: str


def insert_page_capture(conn: sqlite3.Connection, row: PageCaptureRow) -> None:
    conn.execute(
        """
        INSERT INTO page_captures
            (student_id, capture_id, assignment_id, captured_at, image_path)
        VALUES
            (:student_id, :capture_id, :assignment_id, :captured_at, :image_path)
        """,
        vars(row),
    )
    conn.commit()


def get_page_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> PageCaptureRow | None:
    cur = conn.execute(
        "SELECT * FROM page_captures WHERE student_id = ? AND capture_id = ?",
        (student_id, capture_id),
    )
    row = cur.fetchone()
    return None if row is None else PageCaptureRow(**dict(row))


@dataclass(frozen=True)
class ProblemRow:
    student_id: str
    capture_id: str
    problem_id: str
    prompt_text: str
    student_answer_raw: str
    transcription_confidence: float
    skill_ids: tuple[str, ...] = ()
    page_region: tuple[int, int, int, int] | None = None


def insert_problem(conn: sqlite3.Connection, row: ProblemRow) -> None:
    conn.execute(
        """
        INSERT INTO problems
            (student_id, capture_id, problem_id, prompt_text, student_answer_raw,
             transcription_confidence, skill_ids, page_region)
        VALUES
            (:student_id, :capture_id, :problem_id, :prompt_text, :student_answer_raw,
             :transcription_confidence, :skill_ids, :page_region)
        """,
        {
            **vars(row),
            "skill_ids": json.dumps(list(row.skill_ids)),
            "page_region": json.dumps(row.page_region) if row.page_region is not None else None,
        },
    )
    conn.commit()


def list_problems_for_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> list[ProblemRow]:
    cur = conn.execute(
        "SELECT * FROM problems WHERE student_id = ? AND capture_id = ? ORDER BY problem_id",
        (student_id, capture_id),
    )
    return [_row_to_problem(row) for row in cur.fetchall()]


def _row_to_problem(row: sqlite3.Row) -> ProblemRow:
    data = dict(row)
    data["skill_ids"] = tuple(json.loads(data["skill_ids"]))
    region = data["page_region"]
    data["page_region"] = tuple(json.loads(region)) if region is not None else None
    return ProblemRow(**data)
