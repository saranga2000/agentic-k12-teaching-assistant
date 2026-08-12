"""Resolving a student's default assignment for a given day.

Never a code constant: which content source a student works from on a given weekday
is a database row (`weekly_default_sources`), set up once M3.1 builds the parent-facing
setup flow. Until then it's seeded by `scripts/seed_dev_data.py` for local development.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from k12ta.store import content
from k12ta.store import schedule as store_schedule


def resolve_default_source(
    conn: sqlite3.Connection, student_id: str, on: date
) -> content.ContentSourceRow | None:
    scheduled = store_schedule.get_default_source(conn, student_id, weekday=on.weekday())
    if scheduled is None:
        return None
    return content.get_content_source(conn, student_id, scheduled.source_id)


def get_or_create_todays_assignment(
    conn: sqlite3.Connection, student_id: str, source_id: str, on: date
) -> content.AssignmentRow:
    """One assignment row per (student, source, day); re-capturing the same day
    reuses it rather than creating a duplicate."""
    assignment_id = f"{source_id}:{on.isoformat()}"
    existing = content.get_assignment(conn, student_id, assignment_id)
    if existing is not None:
        return existing
    row = content.AssignmentRow(
        student_id=student_id,
        assignment_id=assignment_id,
        source_id=source_id,
        created_at=on.isoformat(),
    )
    content.insert_assignment(conn, row)
    return row
