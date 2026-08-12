"""Connection helper.

Neither foreign-key enforcement nor name-based row access is a sqlite3 default; both
matter here, so they are turned on in one place instead of at every call site.

`check_same_thread=False` (M2.2): FastAPI resolves a sync `Depends` generator like
`k12ta.web.app.get_conn` in a worker thread but runs an `async def` path function on
the event loop thread, so the connection `get_conn` yields is handed across threads
within a single request even though access within that request is strictly
sequential, never concurrent. sqlite3's same-thread check exists to catch genuinely
concurrent cross-thread use, which this isn't; disabling it here is what makes a
`Depends`-yielded connection usable at all.
"""

from __future__ import annotations

import sqlite3


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
