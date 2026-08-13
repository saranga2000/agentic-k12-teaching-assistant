"""A persisted daily count of model-provider requests.

Not student-scoped, deliberately: see docs/ARCHITECTURE.md, "Multi-user". The
per-run request cap in k12ta.llm.gemini resets when the process restarts; this table
is what makes a daily ceiling survive that restart.
"""

from __future__ import annotations

import sqlite3
from datetime import date


def get_count(conn: sqlite3.Connection, on: date) -> int:
    cur = conn.execute(
        "SELECT request_count FROM daily_request_counts WHERE request_date = ?",
        (on.isoformat(),),
    )
    row = cur.fetchone()
    return 0 if row is None else int(row[0])


def record_request(conn: sqlite3.Connection, on: date) -> int:
    """Increment today's count and return the new total."""
    conn.execute(
        """
        INSERT INTO daily_request_counts (request_date, request_count)
        VALUES (?, 1)
        ON CONFLICT (request_date) DO UPDATE SET request_count = request_count + 1
        """,
        (on.isoformat(),),
    )
    conn.commit()
    return get_count(conn, on)
