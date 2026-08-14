"""The one place a `GradedProblemRow` becomes text a student sees.

`render_student_result` is the only function in this codebase permitted to read
`GradedProblemRow.expected_answer` or turn a grading outcome into a sentence. It
takes `rules: FeedbackRules` as a required keyword-only argument with no default,
so it cannot be called without one -- a caller with no `FeedbackRules` in scope
gets a `TypeError` (a `mypy --strict` error at the call site), not a chance to
forget. Callers (`k12ta.web`) pass through `StudentResultView`, which has no raw
`expected_answer` field of its own -- the answer, when permitted, is already
folded into `message` here, so there is nothing left for a template to leak by
accident.

The needs-human branch never reads `rules`: those six causes ("I could not read
your writing" and its siblings) are honest in every feedback mode, so the
function that produces their copy takes no policy input at all.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from k12ta.domain.attempts import PastAttempt, already_disclosed
from k12ta.domain.policy import FeedbackRules
from k12ta.grading.needs_human import NeedsHumanCause
from k12ta.store.sessions import GradedProblemRow

# One message and one glyph per k12ta.grading.needs_human.NeedsHumanCause, so the
# six read differently at a glance -- reinforcement alongside the message text,
# never the only signal (rule 11's spirit extended to "meaning is never colour- or
# glyph-alone"). UNKNOWN_PAGE is the one every student capture hits today: the
# capture flow has no page-number field yet (see docs/ROADMAP.md's page-identity
# discussion), so NO_KEY_FOR_PAGE and NEEDS_PERSON are not reachable from this route
# at all until that's built -- still given real, distinct copy here rather than
# left unhandled, since seeding a session directly (as the key-upload confirm flow
# already can) reaches them today, and student capture will once page numbers land.
COULD_NOT_READ_MESSAGE = "I could not read this one clearly."
COULD_NOT_READ_GLYPH = "?"
UNKNOWN_PAGE_MESSAGE = "I'm not sure which page this is — ask a grown-up to check it."
UNKNOWN_PAGE_GLYPH = "…"
NO_ANSWER_KEY_MESSAGE = "I don't have an answer key for this one yet — ask a grown-up to check it."
NO_ANSWER_KEY_GLYPH = "—"
NEEDS_PERSON_MESSAGE = "This one needs a grown-up to take a look."
NEEDS_PERSON_GLYPH = "~"
# Its own distinct message, not UNKNOWN_PAGE's copy with different words -- and an
# instruction, not just a diagnosis: this is the de facto two-page-spread handler
# (docs/ROADMAP.md's M2.2 note), so "I can see two page markers" alone leaves a
# child stuck on a photo that will never resolve no matter how many times she
# retries it. "Take a photo of just one page" tells her the one thing that fixes
# it. See k12ta.grading.needs_human.NeedsHumanCause.CONFLICTING_PAGE_MARKERS's
# docstring.
CONFLICTING_PAGE_MARKERS_MESSAGE = "I can see two page markers. Take a photo of just one page."
CONFLICTING_PAGE_MARKERS_GLYPH = "⇄"
# Recoverable, unlike CONFLICTING_PAGE_MARKERS above -- re-photographing with the
# missing part in frame fixes it. The real message is built dynamically from
# graded_problems.needs_human_detail (which components were seen/missing, see
# k12ta.pipeline.process); this static text is only the fallback for a row with
# the cause but no usable detail (malformed, or predates this column).
PARTIAL_PAGE_MARKERS_MESSAGE = "I can see part of the page marker, but not all of it."
PARTIAL_PAGE_MARKERS_GLYPH = "◐"
# A row graded before the needs_human_cause column existed (migration 0006) has no
# claimed reason -- genuinely unknown, not a guess dressed up as one.
UNKNOWN_CAUSE_MESSAGE = "I need a grown-up to look at this one."
UNKNOWN_CAUSE_GLYPH = "?"

_NEEDS_HUMAN_COPY: dict[NeedsHumanCause, tuple[str, str]] = {
    NeedsHumanCause.LOW_CONFIDENCE: (COULD_NOT_READ_GLYPH, COULD_NOT_READ_MESSAGE),
    NeedsHumanCause.UNKNOWN_PAGE: (UNKNOWN_PAGE_GLYPH, UNKNOWN_PAGE_MESSAGE),
    NeedsHumanCause.NO_KEY_FOR_PAGE: (NO_ANSWER_KEY_GLYPH, NO_ANSWER_KEY_MESSAGE),
    NeedsHumanCause.NEEDS_PERSON: (NEEDS_PERSON_GLYPH, NEEDS_PERSON_MESSAGE),
    NeedsHumanCause.CONFLICTING_PAGE_MARKERS: (
        CONFLICTING_PAGE_MARKERS_GLYPH,
        CONFLICTING_PAGE_MARKERS_MESSAGE,
    ),
    NeedsHumanCause.PARTIAL_PAGE_MARKERS: (
        PARTIAL_PAGE_MARKERS_GLYPH,
        PARTIAL_PAGE_MARKERS_MESSAGE,
    ),
}

CORRECT_GLYPH = "✓"
CORRECT_MESSAGE = "Correct!"
INCORRECT_GLYPH = "✎"
# reveal_final_answer=False (DIAGNOSTIC_ONLY, FLUENCY): no location or concept
# naming yet -- that requires k12ta.diagnose output, which does not exist on any
# row today (see GradedProblemRow.diagnosis_* -- always unset until that
# milestone). This message is deliberately generic so that adding location/concept
# text later narrows this same branch rather than opening a new one.
INCORRECT_RESTRICTED_MESSAGE = "This one needs another look."

REPEAT_GLYPH = "↺"
# Identical regardless of whether this attempt is actually right or wrong -- a
# message that varies with correctness is itself the oracle the multi-attempt
# suppression exists to close. See k12ta.domain.attempts.already_disclosed.
REPEAT_MESSAGE = "I already told you what I can on this one — check it yourself."


def _join_labels(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _needs_human_copy(cause_value: str | None, detail_json: str | None = None) -> tuple[str, str]:
    """Glyph and message for a graded_problems row's stored `needs_human_cause`
    (and, for PARTIAL_PAGE_MARKERS, its `needs_human_detail`). The one place this
    decision is rendered from -- never re-derived from confidence or any other
    proxy, see docs/PROGRESS.md's M2 entry for why that was wrong before. The
    facts (which components were seen/missing) come from `k12ta.pipeline.process`
    -- this function only interpolates them into a sentence, it never infers a
    source's schema itself. Deliberately ignores `rules`: it doesn't take one --
    these causes are honest in every feedback mode."""
    if cause_value is None:
        return UNKNOWN_CAUSE_GLYPH, UNKNOWN_CAUSE_MESSAGE
    cause = NeedsHumanCause(cause_value)
    if cause is NeedsHumanCause.PARTIAL_PAGE_MARKERS and detail_json:
        try:
            detail = json.loads(detail_json)
            seen = _join_labels(list(detail.get("seen", [])))
            missing = _join_labels(list(detail.get("missing", [])))
        except (json.JSONDecodeError, TypeError, AttributeError):
            seen, missing = "", ""
        if seen and missing:
            return PARTIAL_PAGE_MARKERS_GLYPH, f"I can see the {seen} but not the {missing}."
    return _NEEDS_HUMAN_COPY[cause]


@dataclass(frozen=True)
class StudentResultView:
    """Everything a student results screen is allowed to render for one graded
    problem. No `expected_answer` field -- when the policy permits showing it,
    it is already folded into `message`. `outcome` drives styling only, and is
    deliberately overridden to `"repeat"` on a suppressed multi-attempt response
    -- the true correctness must not leak through the CSS class either."""

    problem_id: str
    prompt_text: str
    student_answer_raw: str
    outcome: str
    glyph: str
    message: str


def render_student_result(
    row: GradedProblemRow,
    prompt_text: str,
    student_answer_raw: str,
    *,
    rules: FeedbackRules,
    prior_attempts: Sequence[PastAttempt],
) -> StudentResultView:
    """Turn one graded problem into the only thing a student sees for it.
    `rules` and `prior_attempts` are both required and keyword-only -- there is
    no default for either, so a caller with no `FeedbackRules` in scope, or that
    forgot to fetch this problem's attempt history, cannot call this at all. A
    caller that forgot `prior_attempts` would otherwise silently behave as if
    every attempt were the first, which is exactly the multi-attempt oracle this
    parameter exists to close.

    `prior_attempts` must be every earlier graded_problems row for this exact
    problem identity (student, source, page, problem_id), across every session
    and capture, EXCLUDING this row itself."""
    if row.outcome == "needs_human":
        glyph, message = _needs_human_copy(row.needs_human_cause, row.needs_human_detail)
        outcome = row.outcome
    elif (
        not rules.reveal_final_answer
        and row.outcome in ("correct", "incorrect")
        and already_disclosed(prior_attempts, student_answer_raw)
    ):
        glyph, message, outcome = REPEAT_GLYPH, REPEAT_MESSAGE, "repeat"
    elif row.outcome == "correct":
        glyph, message, outcome = CORRECT_GLYPH, CORRECT_MESSAGE, row.outcome
    else:
        outcome = row.outcome
        if rules.reveal_final_answer:
            glyph = INCORRECT_GLYPH
            message = f"Not quite. The answer is {row.expected_answer}."
        else:
            glyph, message = INCORRECT_GLYPH, INCORRECT_RESTRICTED_MESSAGE

    return StudentResultView(
        problem_id=row.problem_id,
        prompt_text=prompt_text,
        student_answer_raw=student_answer_raw,
        outcome=outcome,
        glyph=glyph,
        message=message,
    )
