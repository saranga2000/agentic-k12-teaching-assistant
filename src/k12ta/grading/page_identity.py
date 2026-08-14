"""Resolving which workbook page a student photographed, or refusing honestly.

Same shape, same concern as `k12ta.grading.needs_human`: a confidence-gated honest
refusal, never a guess. A wrong page identity is worse than the current
UNKNOWN_PAGE behaviour, because it grades real work against the wrong key's
answers -- exactly the "confident wrong grade" this whole system exists to avoid.

Four outcomes, not a pass/fail, because the fix for a parent is different for each:
- RESOLVED: a real page_number, safe to grade against.
- BELOW_FLOOR: an identifier was read, but not confidently enough to trust.
- NOT_FOUND: no candidate for this source's configured kind, or the value read
  has no confirmed key page behind it yet.
- CONFLICTING: two different values for the same marker kind on one photo (a
  two-page spread showing two different "Day N" banners is the common case, not
  a rare one -- 7 of 9 real Summer Bridge fixtures are exactly this). Never a
  pick between them.

`page_identity_kind` is read from the content source, per source, never assumed
globally (docs/ROADMAP.md's page-identity discussion): which candidate field is
authoritative is a pipeline decision, not something the prompt decides by putting
one field first. `unique_problem_ids` (RSM's mechanism -- matching globally-unique
problem numbers directly, not a single page marker) is deliberately not
implemented: no RSM fixtures or real key data exist yet to validate it against,
and building it unvalidated would be exactly the kind of guess this module exists
to refuse.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from k12ta.grading.key_grader import CONFIDENCE_FLOOR
from k12ta.store import page_identities

_LOOKUP_KINDS = frozenset({"day_or_unit_banner", "printed_worksheet_code", "printed_page_number"})
"""Kinds resolved via a single confirmed marker -> page_number mapping
(k12ta.store.page_identities). unique_problem_ids is deliberately excluded --
see the module docstring."""


class PageIdentityOutcome(StrEnum):
    RESOLVED = "resolved"
    BELOW_FLOOR = "below_floor"
    NOT_FOUND = "not_found"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class PageIdentityResolution:
    outcome: PageIdentityOutcome
    page_number: int | None = None
    """Set only when outcome is RESOLVED."""


def resolve(
    conn: sqlite3.Connection,
    student_id: str,
    source_id: str,
    page_identity_kind: str | None,
    candidates: dict[str, tuple[str, ...]],
    confidence: float,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> PageIdentityResolution:
    """`candidates` maps identity kind to every distinct value seen for it on this
    one photo -- more than one value for the source's own configured kind is what
    CONFLICTING detects. `confidence` is the model's own confidence in its identity
    extraction, separate from any single answer's transcription confidence.

    Order matters and is tested: conflicting values are refused unconditionally,
    checked before the confidence floor -- a two-page spread with two banners is
    refused even if the model is very confident it read both of them correctly.
    """
    if page_identity_kind is None:
        # No source is assumed to have a page-identity mechanism until a parent's
        # setup flow records one (not built yet) -- honest refusal, not a guess
        # at which candidate field to trust.
        return PageIdentityResolution(outcome=PageIdentityOutcome.NOT_FOUND)

    values = candidates.get(page_identity_kind, ())
    if len(set(values)) > 1:
        return PageIdentityResolution(outcome=PageIdentityOutcome.CONFLICTING)
    if not values:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NOT_FOUND)
    if confidence < confidence_floor:
        return PageIdentityResolution(outcome=PageIdentityOutcome.BELOW_FLOOR)
    if page_identity_kind not in _LOOKUP_KINDS:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NOT_FOUND)

    page_number = page_identities.get_page_number(conn, student_id, source_id, values[0])
    if page_number is None:
        return PageIdentityResolution(outcome=PageIdentityOutcome.NOT_FOUND)
    return PageIdentityResolution(outcome=PageIdentityOutcome.RESOLVED, page_number=page_number)
