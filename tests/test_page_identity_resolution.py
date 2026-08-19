"""k12ta.grading.page_identity.resolve: turning extracted identity candidates plus
a source's current identity schema (k12ta.store.page_identity_schemas) into a real
page_number, or one of several honest refusals. No test hits the network --
candidates are always given directly.
"""

from __future__ import annotations

import sqlite3

from k12ta.grading.page_identity import (
    PageIdentityOutcome,
    build_composite_key,
    resolve,
    resolve_partial,
)
from k12ta.store import content, db, migrate, page_identities, page_identity_schemas, students


def _migrated_connection() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    migrate.apply_migrations(conn)
    return conn


def _seed_student_and_source(conn: sqlite3.Connection) -> None:
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
        ),
    )


def _seed_day_only_schema(conn: sqlite3.Connection) -> int:
    return page_identity_schemas.save_new_schema(
        conn, "s-marcus", "summer_bridge", [("day", "Day", "Day 5")]
    )


def _seed_section_and_day_schema(conn: sqlite3.Connection) -> int:
    return page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [("section", "Section", "Section 1"), ("day", "Day", "Day 5")],
    )


def _confirm_mapping(
    conn: sqlite3.Connection, composite_key: str, schema_version: int, page_number: int
) -> None:
    page_identities.upsert_identity(
        conn,
        page_identities.PageIdentityRow(
            student_id="s-marcus",
            source_id="summer_bridge",
            page_number=page_number,
            composite_key=composite_key,
            schema_version=schema_version,
            confirmed_at="2026-08-14T08:00:00+00:00",
        ),
    )


def test_source_with_no_schema_is_no_schema() -> None:
    """A source nobody has taught an identity schema to yet -- honest refusal,
    never a guess at which markers matter. This source may legitimately never
    auto-resolve, by design."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"day": ("Day 11",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NO_SCHEMA


def test_resolves_a_single_component_schema_to_its_confirmed_page() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_day_only_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Day 11"]), version, 33)

    result = resolve(
        conn, "s-marcus", "summer_bridge", candidates={"day": ("Day 11",)}, confidence=0.98
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 33


def test_resolves_a_two_component_composite_to_its_confirmed_page() -> None:
    """The whole point: Section alone or Day alone is not enough, but the two
    together are -- the composite is what's looked up, not either component."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 5"]), version, 21)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1",), "day": ("Day 5",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 21


def test_same_day_different_section_resolves_to_different_pages() -> None:
    """The exact bug this redesign exists to fix: "Day 1" alone is not globally
    unique if day numbering resets per section. The composite must disambiguate."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 1"]), version, 13)
    _confirm_mapping(conn, build_composite_key(["Section 2", "Day 1"]), version, 89)

    section1 = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1",), "day": ("Day 1",)},
        confidence=0.98,
    )
    section2 = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 2",), "day": ("Day 1",)},
        confidence=0.98,
    )

    assert section1.page_number == 13
    assert section2.page_number == 89


def test_below_confidence_floor_is_its_own_outcome_even_with_every_component_known() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_day_only_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Day 11"]), version, 33)

    result = resolve(
        conn, "s-marcus", "summer_bridge", candidates={"day": ("Day 11",)}, confidence=0.40
    )

    assert result.outcome is PageIdentityOutcome.BELOW_FLOOR
    assert result.page_number is None


def test_composite_not_in_confirmed_mappings_is_no_mapping() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_day_only_schema(conn)
    # No key page for "Day 99" has ever been scanned.

    result = resolve(
        conn, "s-marcus", "summer_bridge", candidates={"day": ("Day 99",)}, confidence=0.98
    )

    assert result.outcome is PageIdentityOutcome.NO_MAPPING


def test_zero_components_seen_at_all_is_no_markers_not_partial() -> None:
    """Nothing on the page to capture -- not recoverable by re-photographing,
    unlike PARTIAL below."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(conn, "s-marcus", "summer_bridge", candidates={}, confidence=0.98)

    assert result.outcome is PageIdentityOutcome.NO_MARKERS


def test_one_of_two_required_components_missing_is_partial_and_names_both_sides() -> None:
    """Recoverable -- re-photograph with the missing component in frame. The
    result names what was seen and what's missing so the message can be
    specific ("I can see the day but not the section"), not generic."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"day": ("Day 5",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.PARTIAL
    assert result.seen_labels == ("Day",)
    assert result.missing_labels == ("Section",)


def test_partial_beats_below_floor_confidence_is_irrelevant_when_a_component_is_missing() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"day": ("Day 5",)},
        confidence=0.10,
    )

    assert result.outcome is PageIdentityOutcome.PARTIAL


# --- composite conflict semantics: explicit, per the approved design ------------
#
# Rule: computed independently per schema component. If *any single component*
# has more than one distinct value on this photo, the whole resolution is
# CONFLICTING, checked first and unconditionally -- agreement on other
# components never rescues it, because the page these items belong to still
# can't be safely named.


def test_two_days_one_section_is_conflicting() -> None:
    """The common real case: a spread showing one section banner but two
    different day banners (7 of 9 real Summer Bridge fixtures)."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1",), "day": ("Day 2", "Day 3")},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_two_sections_one_day_is_also_conflicting() -> None:
    """The other direction -- a component disagreeing is enough regardless of
    which one it is."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1", "Section 2"), "day": ("Day 5",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_both_components_conflicting_at_once_is_still_just_conflicting() -> None:
    """A straight two-page spread showing two of everything -- same outcome as
    either component conflicting alone, not a different, worse case."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1", "Section 2"), "day": ("Day 5", "Day 6")},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_conflicting_wins_over_low_confidence() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_day_only_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"day": ("Day 2", "Day 3")},
        confidence=0.10,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_conflicting_wins_over_a_missing_component() -> None:
    """A component conflicting is refused before a different, missing component
    ever gets a chance to be reported as PARTIAL -- conflict is checked first,
    unconditionally, same ordering principle as the confidence floor."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    _seed_section_and_day_schema(conn)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"day": ("Day 2", "Day 3")},  # section not reported at all
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.CONFLICTING


def test_resolve_ignores_a_candidate_kind_outside_the_schema() -> None:
    """A high-confidence, unambiguous candidate for a component this source's
    schema doesn't use is irrelevant -- the schema decides which fields matter,
    not whatever the extraction happened to also report."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_day_only_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Day 11"]), version, 33)

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={
            "day": ("Day 11",),
            "printed_page_number": ("999",),  # would resolve to a wrong page if trusted
        },
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.RESOLVED
    assert result.page_number == 33


def test_a_mapping_confirmed_under_an_old_schema_version_is_no_mapping_not_resolved() -> None:
    """A schema edit must not silently reinterpret an old mapping under the new
    shape -- it just stops being eligible until re-confirmed."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    old_version = _seed_day_only_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Day 11"]), old_version, 33)
    _seed_section_and_day_schema(conn)  # bumps the current version

    result = resolve(
        conn,
        "s-marcus",
        "summer_bridge",
        candidates={"section": ("Section 1",), "day": ("Day 11",)},
        confidence=0.98,
    )

    assert result.outcome is PageIdentityOutcome.NO_MAPPING


def test_build_composite_key_joins_values_in_order_with_a_non_printable_separator() -> None:
    assert build_composite_key(["Section 1", "Day 5"]) == "Section 1\x1fDay 5"


# --- resolve_partial: asking when exactly one component is missing --------------
#
# Only ever meaningful after resolve() has already returned PARTIAL with
# exactly one missing component. Auto-resolves on a single candidate only when
# no other value has ever been confirmed for the missing component anywhere
# in the source -- a single match early in a term, before every section has
# been key-scanned, is not proof there is no other section.


def _seed_three_component_schema(conn: sqlite3.Connection) -> int:
    return page_identity_schemas.save_new_schema(
        conn,
        "s-marcus",
        "summer_bridge",
        [
            ("chapter", "Chapter", "Chapter 1"),
            ("section", "Section", "Section 1"),
            ("day", "Day", "Day 5"),
        ],
    )


def test_resolve_partial_auto_resolves_when_only_one_value_ever_confirmed() -> None:
    """Day 2 is known, Section is missing. Only Section 1 has ever been
    confirmed for anything in this source -- no evidence a second section
    exists yet, so this is deduction, not a guess."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 1"]), version, 13)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 2"]), version, 15)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number == 15
    assert result.matches == ()


def test_resolve_partial_does_not_auto_resolve_when_another_value_exists_elsewhere() -> None:
    """The exact case that motivated the limit: Day 2 has only ever been
    confirmed under Section 1, but Section 2 is known to exist (confirmed for
    a different day) -- an unscanned Section 2/Day 2 page might exist too, so
    one match here is not proof there's no other one."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 2"]), version, 15)
    _confirm_mapping(conn, build_composite_key(["Section 2", "Day 6"]), version, 71)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number is None
    assert len(result.matches) == 1
    assert result.matches[0].missing_value == "Section 1"
    assert result.matches[0].page_number == 15


def test_resolve_partial_offers_every_match_when_more_than_one_section_has_this_day() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 2"]), version, 15)
    _confirm_mapping(conn, build_composite_key(["Section 2", "Day 2"]), version, 63)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number is None
    values = {m.missing_value: m.page_number for m in result.matches}
    assert values == {"Section 1": 15, "Section 2": 63}


def test_resolve_partial_returns_nothing_when_this_day_was_never_confirmed_anywhere() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_section_and_day_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Section 1", "Day 1"]), version, 13)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 99",)})

    assert result.auto_resolved_page_number is None
    assert result.matches == ()


def test_resolve_partial_is_a_no_op_when_more_than_one_component_is_missing() -> None:
    """Explicit requirement: with two or more components missing, this must not
    attempt anything -- the caller keeps refusing honestly (PARTIAL, unchanged)."""
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    version = _seed_three_component_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Chapter 1", "Section 1", "Day 2"]), version, 15)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number is None
    assert result.matches == ()


def test_resolve_partial_is_a_no_op_with_no_schema() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number is None
    assert result.matches == ()


def test_resolve_partial_ignores_a_mapping_confirmed_under_an_older_schema_version() -> None:
    conn = _migrated_connection()
    _seed_student_and_source(conn)
    old_version = _seed_day_only_schema(conn)
    _confirm_mapping(conn, build_composite_key(["Day 2"]), old_version, 99)
    _seed_section_and_day_schema(conn)  # bumps the current version; nothing confirmed under it

    result = resolve_partial(conn, "s-marcus", "summer_bridge", {"day": ("Day 2",)})

    assert result.auto_resolved_page_number is None
    assert result.matches == ()
