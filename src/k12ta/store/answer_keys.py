"""Confirmed answer-key entries: one row per (student, source, page, problem).

Nothing here writes an entry that hasn't been through a parent's confirmation --
that gate lives in the caller (k12ta.keys), not here. This module only persists what
it's given.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerKeyEntryRow:
    student_id: str
    source_id: str
    page_number: int
    problem_number: str
    answer_text: str | None
    ungradeable_reason: str | None
    """One of "answers_vary" or "graph_or_table" when `answer_text` is None."""
    confirmed_at: str
    source: str = "model"
    """"model" when transcribed from a scanned key page, "manual" when a parent
    typed it directly (M3.4, no photo, no model call) -- mirrors
    k12ta.store.page_identities.PageIdentityRow.source exactly, same reasoning:
    who supplied the value, not how sure anyone was, so an eval can measure
    accuracy against only what the model actually produced."""


def upsert_entry(conn: sqlite3.Connection, row: AnswerKeyEntryRow) -> None:
    """Insert or correct one entry. Re-confirming an already-stored page (photo
    boundaries in a scanning sitting can overlap) updates it rather than erroring."""
    conn.execute(
        """
        INSERT INTO answer_key_entries
            (student_id, source_id, page_number, problem_number, answer_text,
             ungradeable_reason, confirmed_at, source)
        VALUES
            (:student_id, :source_id, :page_number, :problem_number, :answer_text,
             :ungradeable_reason, :confirmed_at, :source)
        ON CONFLICT (student_id, source_id, page_number, problem_number) DO UPDATE SET
            answer_text = excluded.answer_text,
            ungradeable_reason = excluded.ungradeable_reason,
            confirmed_at = excluded.confirmed_at,
            source = excluded.source
        """,
        vars(row),
    )
    conn.commit()


def get_entry(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    page_number: int,
    problem_number: str,
) -> AnswerKeyEntryRow | None:
    cur = conn.execute(
        """
        SELECT * FROM answer_key_entries
        WHERE student_id = ? AND source_id = ? AND page_number = ? AND problem_number = ?
        """,
        (student_id, source_id, page_number, problem_number),
    )
    row = cur.fetchone()
    return None if row is None else AnswerKeyEntryRow(**dict(row))


def get_entries_for_page(
    conn: sqlite3.Connection, student_id: str, source_id: str, page_number: int
) -> list[AnswerKeyEntryRow]:
    cur = conn.execute(
        """
        SELECT * FROM answer_key_entries
        WHERE student_id = ? AND source_id = ? AND page_number = ?
        ORDER BY problem_number
        """,
        (student_id, source_id, page_number),
    )
    return [AnswerKeyEntryRow(**dict(row)) for row in cur.fetchall()]


def list_entries_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> list[AnswerKeyEntryRow]:
    cur = conn.execute(
        """
        SELECT * FROM answer_key_entries WHERE student_id = ? AND source_id = ?
        ORDER BY page_number, problem_number
        """,
        (student_id, source_id),
    )
    return [AnswerKeyEntryRow(**dict(row)) for row in cur.fetchall()]
