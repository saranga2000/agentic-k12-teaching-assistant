"""Tiny migration runner.

Applies every `.sql` file in `migrations/` that is not yet recorded, in filename
order, and records its filename stem in `schema_migrations` so a second run is a
no-op. One file today; the mechanism is what has to survive a second one arriving.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(_BOOTSTRAP)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every not-yet-applied migration in order. Returns newly applied versions."""
    already = applied_versions(conn)
    newly_applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = path.stem
        if version in already:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        newly_applied.append(version)
    return newly_applied
