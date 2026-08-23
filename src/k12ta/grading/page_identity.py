"""Resolving which workbook page a student photographed, or refusing honestly.

Same shape, same concern as `k12ta.grading.needs_human`: a confidence-gated honest
refusal, never a guess. A wrong page identity is worse than the current
UNKNOWN_PAGE behaviour, because it grades real work against the wrong key's
answers -- exactly the "confident wrong grade" this whole system exists to avoid.

Identity is a *composite* of a source's own components (Summer Bridge:
section + day; Kumon: worksheet_code alone; RSM: chapter + problem_range),
discovered from a parent's first key scan rather than declared at enrollment --
see `k12ta.store.page_identity_schemas`. A single component is not assumed
globally unique: Summer Bridge's own day numbering plausibly resets per section,
which is exactly the bug the earlier single-`page_identity_kind` design broke on.

Seven outcomes, not a pass/fail, because the fix for a parent is different for
each:
- NO_SCHEMA: this source has no identity schema yet (first scan hasn't happened,
  or never will -- a source may legitimately never auto-resolve).
- CONFLICTING: any single schema component has more than one distinct value on
  this one photo (a two-page spread is the common case, not the rare one -- 7 of
  9 real Summer Bridge fixtures are exactly this). Checked first, unconditionally,
  before anything else -- agreement on other components never rescues it, because
  the page these items belong to still can't be safely named. Two sections + one
  day, one section + two days, and both at once are all this same outcome; which
  component disagreed doesn't change the fix (photograph one page at a time).
- NO_MARKERS: the schema is non-empty, but zero of its components have any value
  on this photo at all. Not recoverable by re-photographing -- there is nothing
  on the page to capture.
- PARTIAL: at least one component was read, but at least one required component
  was not. Recoverable -- re-photograph with the missing part in frame. Reports
  which components were seen and which are missing (by label) so the cause layer
  can say so specifically.
- BELOW_FLOOR: every component was read, but the extraction's confidence in that
  reading is below the floor.
- NO_MAPPING: every component confidently read and a real composite built, but no
  confirmed page_identities row matches it at the source's *current* schema
  version -- either truly never confirmed, or confirmed only under an older
  schema version (see `k12ta.store.page_identities`'s staleness rule). Both need
  the same fix: confirm (or re-confirm) this page's key.
- RESOLVED: the composite matched a confirmed mapping; a real page_number.

`unique_problem_ids` (RSM's mechanism) is no longer a special-cased, unimplemented
kind -- any schema component a parent confirms is resolved the same uniform way,
since resolve() only ever cares about component *names*, never their semantics.
Whether a component is actually a *good* identifier (globally unique, legible) is
a modelling question for that source's schema, not something this module decides.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from k12ta.grading.key_grader import CONFIDENCE_FLOOR
from k12ta.store import page_identities, page_identity_schemas

_SEPARATOR = "\x1f"
"""A non-printable control character, chosen so it can never collide with a real
printed marker value -- component values are joined with this to build the
composite lookup key."""


def build_composite_key(values: Sequence[str]) -> str:
    """The one place a composite key is built, shared by `resolve()` and by
    `k12ta.keys.app`'s confirm/manual-entry routes, so the two sides can never
    drift apart on how a key is constructed. `values` must already be in the
    source's current schema position order."""
    return _SEPARATOR.join(values)


class PageIdentityOutcome(StrEnum):
    NO_SCHEMA = "no_schema"
    CONFLICTING = "conflicting"
    NO_MARKERS = "no_markers"
    PARTIAL = "partial"
    BELOW_FLOOR = "below_floor"
    NO_MAPPING = "no_mapping"
    RESOLVED = "resolved"


RESOLVED_BY_STUDENT_PICK = "resolved_by_student_pick"
"""Not a resolve() outcome -- resolve() never produces this, and
PageIdentityOutcome deliberately stays exactly the seven values it can
produce, unchanged. This is logged separately, as its own
page_identity_resolutions row, only by the web route that accepts a
student's constrained pick after resolve_partial found candidates to offer.
Kept distinct from "resolved" specifically so a per-source accuracy count
(k12ta.store.page_identity_resolutions.count_outcomes_for_source) can never
conflate a student's pick with the model's own composite lookup succeeding
-- see docs/ARCHITECTURE.md's "asking when exactly one component is
missing" section for the full reasoning."""

RESOLVED_BY_STUDENT_ENTRY = "resolved_by_student_entry"
"""Also not a resolve() outcome, and also its own page_identity_resolutions
row -- but distinct from RESOLVED_BY_STUDENT_PICK, not folded into it: a
pick chooses among a small set of candidates resolve_partial already
verified against confirmed data (docs/ARCHITECTURE.md's "asking when
exactly one component is missing"); this is a student typing a page number
free-text when there was nothing to constrain a pick from at all (the
"ask the human and proceed" principle, 2026-08-22 -- see docs/ROADMAP.md's
M3.8), confirmed only against a preview of that page's own key, never
against a list of real candidates. Kept as its own value so an accuracy
count can tell "the model resolved it," "she picked among real options,"
and "she typed a number and confirmed it herself" apart -- three different
claims about how confident the result actually is, none of them each
other."""

RESOLVED_BY_PARENT_ENTRY = "resolved_by_parent_entry"
"""The same free-text, preview-then-confirm shape as RESOLVED_BY_STUDENT_ENTRY,
offered on the same still-unresolved captures, but from k12ta.keys.app rather
than k12ta.web.app -- a parent reading the pending list's photo, not the
student who took it. Kept distinct from RESOLVED_BY_STUDENT_ENTRY, not
folded into it, for the same reason that one is kept distinct from
RESOLVED_BY_STUDENT_PICK: an accuracy count should be able to tell who
supplied a claim apart, not just that it wasn't the model. See
docs/ROADMAP.md's M3.9."""


@dataclass(frozen=True)
class PageIdentityResolution:
    outcome: PageIdentityOutcome
    page_number: int | None = None
    """Set only when outcome is RESOLVED."""
    seen_labels: tuple[str, ...] = ()
    """Set only when outcome is PARTIAL: the schema components (by parent-facing
    label) that were read on this photo."""
    missing_labels: tuple[str, ...] = ()
    """Set only when outcome is PARTIAL: the schema components (by parent-facing
    label) that were not."""


def resolve(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    candidates: dict[str, tuple[str, ...]],
    confidence: float,
    confidence_floor: float = CONFIDENCE_FLOOR,
    *,
    schema_version: int | None = None,
) -> PageIdentityResolution:
    """`candidates` maps component name to every distinct value seen for it on
    this one photo -- more than one value for any of *this source's own schema
    components* is what CONFLICTING detects; a candidate for a component outside
    the schema is ignored entirely. `confidence` is the model's own confidence in
    its identity extraction, separate from any single answer's transcription
    confidence.

    `schema_version`, when given, resolves against that specific version
    instead of the source's current one -- for a caller deliberately checking
    an older schema as a fallback (see `resolve_with_schema_history`). Every
    ordinary caller omits it and gets today's "current schema" behaviour
    unchanged.

    Order matters and is tested: conflicting values are refused unconditionally,
    checked before anything else -- a two-page spread is refused even if the
    model is very confident it read every value correctly. A missing component
    (PARTIAL) is checked before the confidence floor too: there is nothing to be
    confident *about* for a component that was never read at all.
    """
    version = (
        schema_version
        if schema_version is not None
        else page_identity_schemas.get_current_version(conn, student_id, source_id)
    )
    schema = (
        page_identity_schemas.get_schema_at_version(conn, student_id, source_id, version)
        if schema_version is not None
        else page_identity_schemas.get_current_schema(conn, student_id, source_id)
    )
    if not schema:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NO_SCHEMA)

    if any(len(set(candidates.get(c.component_name, ()))) > 1 for c in schema):
        return PageIdentityResolution(outcome=PageIdentityOutcome.CONFLICTING)

    seen = [c for c in schema if len(candidates.get(c.component_name, ())) == 1]
    missing = [c for c in schema if c not in seen]

    if not seen:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NO_MARKERS)
    if missing:
        return PageIdentityResolution(
            outcome=PageIdentityOutcome.PARTIAL,
            seen_labels=tuple(c.label for c in seen),
            missing_labels=tuple(c.label for c in missing),
        )
    if confidence < confidence_floor:
        return PageIdentityResolution(outcome=PageIdentityOutcome.BELOW_FLOOR)

    composite_key = build_composite_key([candidates[c.component_name][0] for c in schema])
    page_number = page_identities.get_page_number(
        conn, student_id, source_id, composite_key, version
    )
    if page_number is None:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NO_MAPPING)
    return PageIdentityResolution(outcome=PageIdentityOutcome.RESOLVED, page_number=page_number)


@dataclass(frozen=True)
class PartialMatch:
    missing_value: str
    """A real, already-confirmed value for the one component this photo
    didn't show -- never a guess or a free-text possibility, always something
    a parent already verified against the physical book."""
    page_number: int


@dataclass(frozen=True)
class PartialResolution:
    """The result of checking a PARTIAL identity (exactly one missing schema
    component) against what this source already has confirmed, per
    docs/ARCHITECTURE.md's "asking when exactly one component is missing"
    section -- a bounded, deliberate exception to refusing outright, distinct
    from the ordinary composite lookup RESOLVED already performs. Both fields
    default empty/None: the no-op case (more than one component missing, no
    schema, or nothing confirmed that agrees with what was read) leaves the
    caller's existing PARTIAL refusal exactly as it was."""

    auto_resolved_page_number: int | None = None
    """Set only when exactly one match exists AND no other value has ever
    been confirmed for the missing component anywhere in this source -- see
    resolve_partial's docstring for why that second condition is required."""
    matches: tuple[PartialMatch, ...] = ()
    """Set when there's something to ask about instead: 2+ matches, or
    exactly 1 that doesn't meet the auto-resolve bar above. The caller offers
    these as a constrained pick, never free text -- every option here is a
    real, already-confirmed page."""
    seen_values: dict[str, str] = field(default_factory=dict)
    """{component_name: value} for every component this photo DID read --
    empty only on the fully-no-op path (no schema, or more than one
    component missing). Present so a caller that's about to ask (matches
    non-empty) can persist exactly this, and nothing more, for the pick
    screen and the pick submission to later re-derive fresh candidates from
    -- see k12ta.store.page_identity_resolutions.PageIdentityResolutionRow.
    seen_values_json. Ignore it when matches is empty; there's nothing to
    ask about either way."""


def resolve_partial(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    photo_candidates: dict[str, tuple[str, ...]],
    *,
    schema_version: int | None = None,
) -> PartialResolution:
    """Only meaningful to call after `resolve()` has already returned PARTIAL
    for `photo_candidates` (at the same `schema_version`, if one was passed)
    -- this repeats none of resolve()'s own checks (CONFLICTING, NO_SCHEMA,
    NO_MARKERS, confidence) and assumes the caller already made them. Looks
    among this source's already-confirmed page_identities mappings,
    decomposed by the current schema's component ordering, for ones that
    agree with every component `photo_candidates` DID read, to see whether
    the one missing component is safely inferable rather than a genuine
    ambiguity.

    A no-op (PartialResolution() -- caller's existing refusal stands
    unchanged) whenever: there's no schema; more than one component is
    missing (explicit requirement -- keep refusing honestly rather than
    guess which of several gaps to fill); or nothing already confirmed
    agrees with what this photo read.

    Auto-resolves (sets auto_resolved_page_number, no matches offered) only
    when exactly one agreeing mapping exists AND no other value has EVER
    been confirmed for the missing component anywhere in this source. That
    second condition is the whole point: a single match early in a term,
    before every section has been key-scanned, is not proof a second section
    doesn't exist -- it only means no page from it has been taught to the
    system yet. Without this check, the very first Section 2 page a student
    ever photographs, before a parent has scanned anything from Section 2,
    would auto-resolve to a Section 1 page sharing the same day number and
    grade real work against the wrong answers -- exactly the "confident
    wrong grade" this whole system exists to avoid. This is deduction from
    what a parent has already confirmed against the physical book, not
    inference from how much of the book happens to be confirmed so far.

    Otherwise returns whatever real matches exist for the caller to offer as
    a constrained pick (never free text, never a guess).

    `schema_version`, when given, matches `resolve`'s own override: resolve
    against that specific version instead of the source's current one."""
    version = (
        schema_version
        if schema_version is not None
        else page_identity_schemas.get_current_version(conn, student_id, source_id)
    )
    schema = (
        page_identity_schemas.get_schema_at_version(conn, student_id, source_id, version)
        if schema_version is not None
        else page_identity_schemas.get_current_schema(conn, student_id, source_id)
    )
    if not schema:
        return PartialResolution()

    seen = [c for c in schema if len(photo_candidates.get(c.component_name, ())) == 1]
    missing = [c for c in schema if c not in seen]
    if len(missing) != 1:
        return PartialResolution()
    missing_component = missing[0]
    seen_values = {c.component_name: photo_candidates[c.component_name][0] for c in seen}

    rows = page_identities.list_for_source_at_version(conn, student_id, source_id, version)

    universe: set[str] = set()
    matches: list[PartialMatch] = []
    for row in rows:
        parts = row.composite_key.split(_SEPARATOR)
        if len(parts) != len(schema):
            continue  # a composite from a differently-shaped schema position count
        values = {c.component_name: parts[i] for i, c in enumerate(schema)}
        missing_value = values[missing_component.component_name]
        universe.add(missing_value)
        if all(values[name] == value for name, value in seen_values.items()):
            matches.append(PartialMatch(missing_value=missing_value, page_number=row.page_number))

    if len(matches) == 1 and universe == {matches[0].missing_value}:
        return PartialResolution(auto_resolved_page_number=matches[0].page_number)
    return PartialResolution(matches=tuple(matches), seen_values=seen_values)


def resolve_with_schema_history(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    candidates: dict[str, tuple[str, ...]],
    confidence: float,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> tuple[PageIdentityResolution, int]:
    """Tries the source's current schema first; if that doesn't resolve and an
    older schema version exists, falls back to trying the *immediately
    preceding* version too, at the same photo's candidates -- built for
    Summer Bridge's 2026-08-22 schema change (see docs/ROADMAP.md's M3.7):
    version 2 is the printed page number alone, version 1 is the Day+Section
    pair it replaced as the primary signal because Section is not printed on
    any exercise page. A photo can carry markers for both the current and the
    previous schema in one photograph (`k12ta.pipeline.process` asks for the
    union of both components' names), so this lets the day banner keep
    resolving pages the page number extraction alone doesn't, exactly as it
    always could, without a source's older confirmed mappings needing to be
    re-entered under the new schema.

    Returns the resolution AND the schema version it came from -- a caller
    that gets PARTIAL back needs that version to call `resolve_partial` at
    the same one, not "current," which would query the wrong schema (and,
    for a single-component current schema, could never even produce PARTIAL
    in the first place -- see `resolve`'s NO_MARKERS-before-missing check).

    Only ever looks one version back, not further, and only when the current
    schema fails to produce something better. This is a deliberate, narrow
    fallback for one migration, not a general N-version history walk --
    widening it further should be a decision made when a second such
    migration actually needs it, not spun up preemptively here (same
    "keep the scope narrow" spirit docs/ARCHITECTURE.md's page-identity
    section already asks of `resolve_partial`'s own bounded exception).

    CONFLICTING short-circuits immediately, without trying the previous
    version at all: two different values for one schema component (a
    two-page spread, most commonly) is a refusal about *this photo*, not
    about which schema happened to be current -- rescuing it via an older
    schema that happens to look less conflicting would be exactly the
    confident-wrong-grade this whole module exists to refuse."""
    version = page_identity_schemas.get_current_version(conn, student_id, source_id)
    resolution = resolve(conn, student_id, source_id, candidates, confidence, confidence_floor)
    if resolution.outcome in (PageIdentityOutcome.RESOLVED, PageIdentityOutcome.CONFLICTING):
        return resolution, version
    if version <= 1:
        return resolution, version

    previous_version = version - 1
    previous_resolution = resolve(
        conn,
        student_id,
        source_id,
        candidates,
        confidence,
        confidence_floor,
        schema_version=previous_version,
    )
    if previous_resolution.outcome in (PageIdentityOutcome.RESOLVED, PageIdentityOutcome.PARTIAL):
        return previous_resolution, previous_version
    return resolution, version


def schema_version_for_seen_component_names(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    seen_component_names: Sequence[str],
    stored_schema_version: int | None,
) -> int:
    """Which schema version a stored PARTIAL resolution's `seen_values` (its
    component *names*, not values) actually belongs to -- for a caller
    reconstructing candidates from `page_identity_resolutions.seen_values_json`
    well after the fact (the ask-flow in `k12ta.web.app`), which must call
    `resolve_partial` at that same version, never blindly at "current."

    `stored_schema_version` (migration 0016) is trusted outright when present
    -- every resolution logged going forward carries it. It is None only for
    a row logged before that column existed, which this function handles by
    inference instead: find the version, current or the one immediately
    before it, whose own component names are a superset of what was actually
    seen. Same one-version-back bound as `resolve_with_schema_history`, for
    the same reason -- a deliberate, narrow fallback, not a general history
    walk. Falls back to the current version if inference finds no match,
    which reproduces today's (imperfect, pre-fix) behaviour rather than
    raising -- an old row that predates any of this is not this function's
    problem to solve perfectly."""
    if stored_schema_version is not None:
        return stored_schema_version

    current_version = page_identity_schemas.get_current_version(conn, student_id, source_id)
    seen = set(seen_component_names)

    current_names = {
        c.component_name
        for c in page_identity_schemas.get_current_schema(conn, student_id, source_id)
    }
    if seen <= current_names:
        return current_version

    if current_version > 1:
        previous_names = {
            c.component_name
            for c in page_identity_schemas.get_schema_at_version(
                conn, student_id, source_id, current_version - 1
            )
        }
        if seen <= previous_names:
            return current_version - 1

    return current_version
