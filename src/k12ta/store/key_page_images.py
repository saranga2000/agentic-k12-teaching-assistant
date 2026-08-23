"""The key-scan image on file for one confirmed page, if any.

Persisted going forward only (2026-08-22): `k12ta.pipeline.key_ingestion` discarded
every upload before this migration, so a page confirmed under older code has no row
here, and this module has no way to recover one after the fact. A parent-facing scan
display treats "no row" as an honest "no key image available," never a guess.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyPageImageRow:
    student_id: str
    source_id: str
    page_number: int
    image_path: str
    confirmed_at: str


def upsert_image(conn: sqlite3.Connection, row: KeyPageImageRow) -> None:
    """A later re-scan of the same page replaces which image is "the" one for it --
    same reasoning as `k12ta.store.page_identities.upsert_identity`."""
    conn.execute(
        """
        INSERT INTO key_page_images
            (student_id, source_id, page_number, image_path, confirmed_at)
        VALUES
            (:student_id, :source_id, :page_number, :image_path, :confirmed_at)
        ON CONFLICT (student_id, source_id, page_number) DO UPDATE SET
            image_path = excluded.image_path,
            confirmed_at = excluded.confirmed_at
        """,
        vars(row),
    )
    conn.commit()


def get_image_path(
    conn: sqlite3.Connection, student_id: str, source_id: str, page_number: int
) -> str | None:
    cur = conn.execute(
        """
        SELECT image_path FROM key_page_images
        WHERE student_id = ? AND source_id = ? AND page_number = ?
        """,
        (student_id, source_id, page_number),
    )
    row = cur.fetchone()
    return None if row is None else str(row[0])
