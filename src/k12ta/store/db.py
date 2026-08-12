"""Connection helper.

Neither foreign-key enforcement nor name-based row access is a sqlite3 default; both
matter here, so they are turned on in one place instead of at every call site.
"""

from __future__ import annotations

import sqlite3


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
