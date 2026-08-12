"""Local dev/demo convenience only — not a feature, not run in production.

Parents will configure students, content sources, and the weekly schedule through the
M3.1 setup flow. Until that exists, the M2.2 capture screen has nothing to show
without *something* in the database. This inserts two fabricated students (never a
real child's name, per docs/DATA_POLICY.md), seeds `k12ta.content.registry`'s example
content sources for each, and points Monday-Friday at the workbook source so the
capture screen has a default assignment to demo. Safe to rerun: existing rows are left
alone.

    python scripts/seed_dev_data.py
"""

from __future__ import annotations

import sqlite3

from k12ta.config import Settings
from k12ta.content.registry import example_sources
from k12ta.store import content, db, migrate, schedule, students

DEV_STUDENT_IDS = ("dev-alex", "dev-sam")
WEEKDAY_SOURCE_ID = "summer_bridge"


def _seed_student(
    conn: sqlite3.Connection, student_id: str, display_name: str, grade_level: int
) -> None:
    if students.get_student(conn, student_id) is not None:
        return
    students.insert_student(
        conn,
        students.StudentRow(
            student_id=student_id,
            display_name=display_name,
            grade_level=grade_level,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    for source in example_sources():
        content.insert_content_source(
            conn,
            content.ContentSourceRow(
                student_id=student_id,
                source_id=source.source_id,
                label=source.label,
                kind=source.kind.value,
                subject=source.subject,
                has_answer_key=source.has_answer_key,
                graded_by_someone_else=source.graded_by_someone_else,
                default_mode=source.default_mode.value,
                typical_session_minutes=source.typical_session_minutes,
                standards_frame=source.standards_frame,
            ),
        )
    for weekday in range(5):  # Monday-Friday
        schedule.set_default_source(
            conn,
            schedule.WeeklyDefaultSourceRow(
                student_id=student_id, weekday=weekday, source_id=WEEKDAY_SOURCE_ID
            ),
        )


def main() -> None:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(settings.data_dir / "k12ta.db"))
    migrate.apply_migrations(conn)
    _seed_student(conn, "dev-alex", "Alex", grade_level=7)
    _seed_student(conn, "dev-sam", "Sam", grade_level=4)
    conn.close()
    print(f"Seeded dev students {DEV_STUDENT_IDS} into {settings.data_dir / 'k12ta.db'}")


if __name__ == "__main__":
    main()
