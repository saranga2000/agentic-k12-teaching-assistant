"""k12ta.grading.page_identity.resolve: turning extracted identity candidates plus
a source's configured page_identity_kind into a real page_number, or an honest
refusal. No test hits the network -- candidates are always given directly.
"""

from __future__ import annotations

import sqlite3

from k12ta.grading.page_identity import PageIdentityOutcome, resolve
from k12ta.store import content, db, migrate, page_identities, students

CONFIDENCE_FLOOR = 0.95  # mirrors k12ta.grading.key_grader.CONFIDENCE_FLOOR


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed(conn: sqlite3.Connection, page_identity_kind: str | None) -> None:
    students.insert_student(
        conn,
        students.StudentRow(
            student_id="s-marcus",
            display_name="Marcus",
            grade_level=7,
            state_code="CA",
            coach_name="Coach",
        ),
    )
    content.insert_content_source(
        conn,
        content.ContentSourceRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            label="Summer bridge workbook",
            kind="workbook",
            subject="math",
            has_answer_key=True,
            graded_by_someone_else=False,
            default_mode="full",
            typical_session_minutes=30,
            page_identity_kind=page_identity_kind,
        ),
    )


def _confirm_mapping(conn: sqlite3.Connection, identifier_value: str, page_number: int) -> None:
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=page_number,
            identifier_value=identifier_value,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )


def test_resolves_a_known_day_banner_to_its_confirmed_page() -> None:
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")
    _confirm_mapping(conn, "Day 11", 33)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={"day_or_unit_banner": ("Day 11",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 33


def test_below_confidence_floor_is_its_own_outcome_even_with_a_known_marker() -> None:
    """Constraint 3: a guessed identity that grades against the wrong key is worse
    than the current behaviour. Below the floor, refuse -- even when the marker
    itself is one we've seen confirmed before."""
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")
    _confirm_mapping(conn, "Day 11", 33)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={"day_or_unit_banner": ("Day 11",)},
        confidence=0.40,
    )

    assert result.outcome is PageIdentityOutcome.BELOW_FLOOR
    assert result.page_number is None


def test_marker_not_in_confirmed_mappings_is_not_found() -> None:
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")
    # No key page for "Day 99" has ever been scanned.

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={"day_or_unit_banner": ("Day 99",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NOT_FOUND


def test_no_candidate_for_the_sources_configured_kind_is_not_found() -> None:
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NOT_FOUND


def test_source_with_no_configured_kind_is_not_found() -> None:
    """A source nobody has set up page-identity for yet (page_identity_kind is
    still NULL) -- honest refusal, never a guess at which field to trust."""
    conn = _migrated_connection()
    _seed(conn, page_identity_kind=None)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind=None,
        candidates={"day_or_unit_banner": ("Day 11",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NOT_FOUND


def test_two_different_values_for_the_same_kind_is_conflicting_not_a_pick() -> None:
    """Constraint 4, and the actual, common case: 7 of 9 real Summer Bridge
    fixtures are two-page spreads showing two different Day banners at once.
    Never guess between them."""
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")
    _confirm_mapping(conn, "Day 2", 15)
    _confirm_mapping(conn, "Day 3", 17)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={"day_or_unit_banner": ("Day 2", "Day 3")},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING
    assert result.page_number is None


def test_conflicting_wins_over_low_confidence() -> None:
    """Constraint 4 is unconditional -- checked before the confidence gate, not
    just when confidence happens to be high."""
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={"day_or_unit_banner": ("Day 2", "Day 3")},
        confidence=0.10,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_pipeline_only_looks_at_the_sources_configured_kind() -> None:
    """A high-confidence, unambiguous printed_page_number candidate is irrelevant
    if this source is configured for day_or_unit_banner -- the pipeline decides
    which field to trust, per source, not the prompt."""
    conn = _migrated_connection()
    _seed(conn, "day_or_unit_banner")
    _confirm_mapping(conn, "Day 11", 33)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="day_or_unit_banner",
        candidates={
            "day_or_unit_banner": ("Day 11",),
            "printed_page_number": ("999",),  # would resolve to a wrong page if trusted
        },
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 33


def test_printed_page_number_kind_uses_the_same_confirmed_mapping_lookup() -> None:
    conn = _migrated_connection()
    _seed(conn, "printed_page_number")
    _confirm_mapping(conn, "13", 13)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="printed_page_number",
        candidates={"printed_page_number": ("13",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 13


def test_unique_problem_ids_is_not_yet_implemented_and_says_so_honestly() -> None:
    """RSM's mechanism (matching globally-unique problem numbers directly, not a
    single page marker) is a genuinely different resolution path -- deliberately
    not built, since no RSM fixtures or real key data exist yet to validate it
    against (docs/ROADMAP.md's Kumon/RSM measurement gap). Falls to NOT_FOUND, an
    honest refusal, never a guess."""
    conn = _migrated_connection()
    _seed(conn, "unique_problem_ids")

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        page_identity_kind="unique_problem_ids",
        candidates={"unique_problem_ids": ("4019",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NOT_FOUND
