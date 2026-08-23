"""An append-only log of every automatic page-identity resolution attempt during a
student capture. Never written for a caller-supplied page_number (a test, a manual
override) -- the point is measuring what real extraction actually does, not
padding the count with cases that never went through it.

Four honest outcomes (`k12ta.grading.page_identity.PageIdentityOutcome`), not a
single pass/fail: "resolved", "below_floor", "not_found", and "conflicting" each
call for a different fix, and a parent needs to be able to tell them apart -- see
the counts surfaced on `k12ta.keys`'s enrollment page.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PageIdentityResolutionRow:
    student_id: str
    source_id: str
    capture_id: str
    outcome: str
    """One of PageIdentityOutcome's values: "resolved", "below_floor",
    "not_found", "conflicting"."""
    resolved_page_number: int | None
    """Set only when outcome is "resolved"."""
    created_at: str
    seen_values_json: str | None = None
    """Set only for the "ask" case: a PARTIAL identity with exactly one
    missing schema component and at least one real candidate to offer (see
    k12ta.grading.page_identity.resolve_partial). {component_name: value} for
    every component this capture's photo DID read -- never the candidates
    themselves, which are always re-derived fresh from the current
    page_identities table both when the pick screen renders and when a pick
    is submitted, so nothing here can go stale."""
    schema_version: int | None = None
    """Which schema version `outcome` actually came from (migration 0016) --
    the source's current version for almost every row, but a caller using
    k12ta.grading.page_identity.resolve_with_schema_history's one-version-
    back fallback logs whichever version actually produced this outcome, so
    a later query can tell "resolved via the current schema" apart from
    "resolved via the previous one" instead of only inferring it from the
    shape of a stored composite key. None for every row written before this
    column existed, and for any resolve() call outside the fallback path
    that doesn't bother passing it."""


def insert_resolution(conn: sqlite3.Connection, row: PageIdentityResolutionRow) -> None:
    conn.execute(
        """
        INSERT INTO page_identity_resolutions
            (student_id, source_id, capture_id, outcome, resolved_page_number, created_at,
             seen_values_json, schema_version)
        VALUES
            (:student_id, :source_id, :capture_id, :outcome, :resolved_page_number, :created_at,
             :seen_values_json, :schema_version)
        """,
        vars(row),
    )
    conn.commit()


def get_seen_values_for_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> str | None:
    """The raw seen_values_json for one capture's resolution attempt, or None
    if there isn't one (no resolution row at all, or it was never set for
    this outcome) -- the caller parses it, this is a plain lookup."""
    cur = conn.execute(
        """
        SELECT seen_values_json FROM page_identity_resolutions
        WHERE student_id = ? AND capture_id = ?
        """,
        (student_id, capture_id),
    )
    row = cur.fetchone()
    return None if row is None else row[0]


def get_schema_version_for_capture(
    conn: sqlite3.Connection, student_id: str, capture_id: str
) -> int | None:
    """The schema_version recorded for one capture's resolution attempt
    (migration 0016), or None if there isn't one -- no resolution row at
    all, or it predates this column. A caller reconstructing candidates from
    `get_seen_values_for_capture` needs this to call `k12ta.grading.page_
    identity.resolve_partial` at the same version the row was actually
    produced at; see `k12ta.grading.page_identity.
    schema_version_for_seen_component_names` for what to do when this comes
    back None."""
    cur = conn.execute(
        """
        SELECT schema_version FROM page_identity_resolutions
        WHERE student_id = ? AND capture_id = ?
        """,
        (student_id, capture_id),
    )
    row = cur.fetchone()
    return None if row is None else row[0]


def count_outcomes_for_source(
    conn: sqlite3.Connection, student_id: str, source_id: str
) -> dict[str, int]:
    """Every outcome that has fired at least once, keyed by outcome value. An
    outcome with zero rows is simply absent from the dict -- the caller decides
    how to render "never happened," not this function."""
    cur = conn.execute(
        """
        SELECT outcome, COUNT(*) FROM page_identity_resolutions
        WHERE student_id = ? AND source_id = ?
        GROUP BY outcome
        """,
        (student_id, source_id),
    )
    return {row[0]: row[1] for row in cur.fetchall()}
