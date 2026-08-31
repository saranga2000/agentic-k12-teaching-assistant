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
from k12ta.pipeline.process import AMBIGUOUS_PROBLEM_ID_PREFIX
from k12ta.store.disputes import DisputeRow
from k12ta.store.sessions import GradedProblemRow

# One message and one glyph per k12ta.grading.needs_human.NeedsHumanCause, so the
# six read differently at a glance -- reinforcement alongside the message text,
# never the only signal (rule 11's spirit extended to "meaning is never colour- or
# glyph-alone"). All six are reachable from student capture: Scope B wired page-
# identity resolution into k12ta.pipeline.process, so NO_KEY_FOR_PAGE and NEEDS_
# PERSON are live outcomes here now, not only reachable by seeding a session
# directly the way the key-upload confirm flow always could.
COULD_NOT_READ_MESSAGE = "I could not read this one clearly."
COULD_NOT_READ_GLYPH = "?"
UNKNOWN_PAGE_MESSAGE = "I'm not sure which page this is — ask a grown-up to check it."
UNKNOWN_PAGE_GLYPH = "…"
# "I will grade it" is a real promise, not a platitude: k12ta.pipeline.process.
# regrade_capture_for_resolved_identity is what makes it true -- once a parent adds
# the missing key, k12ta.keys's "waiting on an answer key" list finds this problem
# gradable again and lets the parent trigger it, from the already-stored
# transcription, no retake and no re-reading needed.
NO_ANSWER_KEY_MESSAGE = (
    "I don't have the answers for this page yet. Ask a grown-up to add them and I'll grade it."
)
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
# The one needs-human cause whose message deliberately surfaces expected_answer
# -- not to reveal a verdict (none exists yet) but because the whole point is
# to show both sides so a grown-up can judge. Built dynamically in
# _needs_human_copy; this is only the fallback for a row with the cause but no
# expected_answer (shouldn't happen -- k12ta.grading.needs_human.decide always
# sets one for this cause -- but honest is better than crashing).
ANSWER_DIFFERS_FROM_KEY_MESSAGE = "Your answer is different from the key. A grown-up will check."
ANSWER_DIFFERS_FROM_KEY_GLYPH = "≠"
# A row graded before the needs_human_cause column existed (migration 0006) has no
# claimed reason -- genuinely unknown, not a guess dressed up as one.
UNKNOWN_CAUSE_MESSAGE = "I need a grown-up to look at this one."
UNKNOWN_CAUSE_GLYPH = "?"
# Actionable, not only descriptive -- "which question" points at the real gap (no
# printed problem number to key this answer to), distinct from LOW_CONFIDENCE's
# "I could not read your writing" (this isn't about legibility of the answer, it's
# about identity of the question). See k12ta.grading.needs_human.NeedsHumanCause.
# AMBIGUOUS_PROBLEM_ID's docstring for how this is found (blank/duplicate
# problem_id on one photo).
AMBIGUOUS_PROBLEM_ID_MESSAGE = "I could not tell which question this answer belongs to."
AMBIGUOUS_PROBLEM_ID_GLYPH = "#"

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
    NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY: (
        ANSWER_DIFFERS_FROM_KEY_GLYPH,
        ANSWER_DIFFERS_FROM_KEY_MESSAGE,
    ),
    NeedsHumanCause.AMBIGUOUS_PROBLEM_ID: (
        AMBIGUOUS_PROBLEM_ID_GLYPH,
        AMBIGUOUS_PROBLEM_ID_MESSAGE,
    ),
}

CORRECT_GLYPH = "✓"
CORRECT_MESSAGE = "Correct!"
# GradedProblemRow.unsimplified only ("2/6" matched a key of "1/3" by value, not
# by string) -- numerically right, so not INCORRECT, but many workbooks ask to
# simplify, so not silently indistinguishable from a fully-reduced CORRECT
# either. Same glyph as a plain correct mark; the note is carried in the message
# text alone, since outcome/glyph also drive the multi-attempt-oracle CSS class
# and must not grow a fourth state for this.
CORRECT_UNSIMPLIFIED_MESSAGE = "Correct! It can still be simplified further."
# Matches the framing guide's own good/bad vocabulary (k12ta.web's capture screen),
# so "✗" already means "not this" elsewhere in the app before a student ever reaches
# the results table.
INCORRECT_GLYPH = "✗"
# reveal_final_answer=False (DIAGNOSTIC_ONLY, FLUENCY): no location or concept
# naming yet -- that requires k12ta.diagnose output, which does not exist on any
# row today (see GradedProblemRow.diagnosis_* -- always unset until that
# milestone). This message is deliberately generic so that adding location/concept
# text later narrows this same branch rather than opening a new one.
INCORRECT_RESTRICTED_MESSAGE = "This one needs another look."

# GradeOutcome.PARTIALLY_CORRECT (docs/ROADMAP.md's V1 "Verdicts", M6's evaluator):
# genuinely unsplittable partial work, e.g. a half-right prose answer. Gated by
# the same feedback policy as incorrect -- not a special case that always tells
# the child everything -- and given its own glyph/bucket rather than folded into
# "incorrect", since a parent reviewing the results table needs to tell "wrong"
# apart from "partly right" at a glance, same reasoning as CORRECT_UNSIMPLIFIED_
# MESSAGE getting its own message text without a fourth CSS-driving outcome.
PARTIALLY_CORRECT_GLYPH = "±"
PARTIALLY_CORRECT_RESTRICTED_MESSAGE = "Part of this one is right — a grown-up will check the rest."

REPEAT_GLYPH = "↺"
# Identical regardless of whether this attempt is actually right or wrong -- a
# message that varies with correctness is itself the oracle the multi-attempt
# suppression exists to close. See k12ta.domain.attempts.already_disclosed.
REPEAT_MESSAGE = "I already told you what I can on this one — check it yourself."

# The results table's display grouping: eight NeedsHumanCause values collapse to
# three buckets so a student is learning three shapes, not eight, at a glance --
# the per-cause message (above) stays exactly as specific as it always was, only
# the glyph and row-tint are bucket-uniform. Every StudentResultView also carries
# "correct", "incorrect", or "repeat" as its bucket, for the same summary tally.
# Unmapped (None, or a future cause added to the enum but not here) falls to
# "needs_a_person" -- the same honest default UNKNOWN_CAUSE_MESSAGE already uses,
# never a guess about which of the other two it might be.
_COULD_NOT_READ_CAUSES = frozenset(
    {
        NeedsHumanCause.LOW_CONFIDENCE,
        NeedsHumanCause.UNKNOWN_PAGE,
        NeedsHumanCause.CONFLICTING_PAGE_MARKERS,
        NeedsHumanCause.PARTIAL_PAGE_MARKERS,
        NeedsHumanCause.AMBIGUOUS_PROBLEM_ID,
    }
)
_NEEDS_A_PERSON_CAUSES = frozenset(
    {NeedsHumanCause.NEEDS_PERSON, NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY}
)


def _needs_human_bucket(cause_value: str | None) -> str:
    if cause_value is not None:
        cause = NeedsHumanCause(cause_value)
        if cause in _COULD_NOT_READ_CAUSES:
            return "could_not_read"
        if cause is NeedsHumanCause.NO_KEY_FOR_PAGE:
            return "waiting_on_key"
        if cause in _NEEDS_A_PERSON_CAUSES:
            return "needs_a_person"
    return "needs_a_person"


_BUCKET_GLYPH: dict[str, str] = {
    "correct": CORRECT_GLYPH,
    "partially_correct": PARTIALLY_CORRECT_GLYPH,
    "incorrect": INCORRECT_GLYPH,
    "could_not_read": COULD_NOT_READ_GLYPH,
    "waiting_on_key": NO_ANSWER_KEY_GLYPH,
    "needs_a_person": NEEDS_PERSON_GLYPH,
    "repeat": REPEAT_GLYPH,
}

# Buckets that count toward the results-table summary's "to look at" tally --
# things a student herself can act on (retry, or retake a clearer photo).
# partially_correct belongs here, not in "right": genuinely worth another look,
# same as incorrect. Everything else is either "correct" or "waiting_on_grownup"
# ("waiting_on_key", "needs_a_person" -- neither is hers to resolve).
_TO_LOOK_AT_BUCKETS = frozenset({"incorrect", "partially_correct", "could_not_read", "repeat"})
_WAITING_ON_GROWNUP_BUCKETS = frozenset({"waiting_on_key", "needs_a_person"})


def _join_labels(labels: list[str]) -> str:
    if len(labels) <= 1:
        return "".join(labels)
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _needs_human_copy(
    cause_value: str | None,
    detail_json: str | None = None,
    expected_answer: str | None = None,
) -> tuple[str, str]:
    """Glyph and message for a graded_problems row's stored `needs_human_cause`
    (and, for PARTIAL_PAGE_MARKERS, its `needs_human_detail`; for
    ANSWER_DIFFERS_FROM_KEY, its `expected_answer`). The one place this
    decision is rendered from -- never re-derived from confidence or any other
    proxy, see docs/PROGRESS.md's M2 entry for why that was wrong before. The
    facts (which components were seen/missing, or the key's own answer) come
    from `k12ta.pipeline.process` / `k12ta.grading.needs_human.decide` -- this
    function only interpolates them into a sentence, it never infers a
    source's schema or a key's answer itself. Deliberately ignores `rules`: it
    doesn't take one -- these causes are honest in every feedback mode."""
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
    if cause is NeedsHumanCause.ANSWER_DIFFERS_FROM_KEY and expected_answer:
        return (
            ANSWER_DIFFERS_FROM_KEY_GLYPH,
            f'Your answer and the key\'s answer ("{expected_answer}") are different. '
            "A grown-up will check if yours is still right.",
        )
    return _NEEDS_HUMAN_COPY[cause]


@dataclass(frozen=True)
class StudentResultView:
    """Everything a student results screen is allowed to render for one graded
    problem. No `expected_answer` field -- when the policy permits showing it,
    it is already folded into `message`. `outcome` drives styling only, and is
    deliberately overridden to `"repeat"` on a suppressed multi-attempt response
    -- the true correctness must not leak through the CSS class either.

    `display_bucket` is the coarser, six-way grouping ("correct", "incorrect",
    "could_not_read", "waiting_on_key", "needs_a_person", "repeat") the results
    table's glyph and summary counts are keyed to -- see _needs_human_bucket
    above. `display_number` is `problem_id` unless it is a synthetic
    AMBIGUOUS_PROBLEM_ID_PREFIX placeholder (k12ta.pipeline.process), in which
    case there is no real printed number to show and this is "?" instead."""

    problem_id: str
    prompt_text: str
    student_answer_raw: str
    outcome: str
    display_bucket: str
    display_number: str
    glyph: str
    message: str
    capture_id: str
    """Which photo this row came from -- safe to expose (it names a photograph,
    not a grade or an answer), so a results screen can show the student her own
    page alongside the verdict, the same reasoning `k12ta.web.app.capture_image`
    already applies (scoped by `student_id`, nothing to leak by exposing the id
    itself)."""
    dispute: DisputeRow | None = None
    """Gap B/L (docs/USER_WORKFLOWS.md): the child's own dispute of this row,
    if any -- None on every row that was never disputed (the overwhelming
    majority). Safe to expose in full: the child's own reason, and, once
    resolved, the parent's own comment addressed to her. A caller looks this
    up (k12ta.store.disputes.get) and passes it through; this function never
    reaches into that table itself, same "caller supplies context, this
    function only interprets it" split as `prior_attempts`."""


def render_student_result(
    row: GradedProblemRow,
    prompt_text: str,
    student_answer_raw: str,
    *,
    rules: FeedbackRules,
    prior_attempts: Sequence[PastAttempt],
    dispute: DisputeRow | None = None,
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
        _, message = _needs_human_copy(
            row.needs_human_cause, row.needs_human_detail, row.expected_answer
        )
        outcome = row.outcome
        bucket = _needs_human_bucket(row.needs_human_cause)
    elif (
        not rules.reveal_final_answer
        and row.outcome in ("correct", "partially_correct", "incorrect")
        and already_disclosed(prior_attempts, student_answer_raw)
    ):
        message, outcome, bucket = REPEAT_MESSAGE, "repeat", "repeat"
    elif row.outcome == "correct":
        outcome, bucket = row.outcome, "correct"
        message = CORRECT_UNSIMPLIFIED_MESSAGE if row.unsimplified else CORRECT_MESSAGE
    elif row.outcome == "partially_correct":
        outcome, bucket = row.outcome, "partially_correct"
        if rules.reveal_final_answer:
            # expected_answer can be None here (M6's keyless path has no
            # reference key to show at all, only the agent's own judgement) --
            # fall back to the same generic wording restricted mode uses rather
            # than render a literal "None".
            message = (
                f"Part of this one is right. The full answer is {row.expected_answer}."
                if row.expected_answer is not None
                else PARTIALLY_CORRECT_RESTRICTED_MESSAGE
            )
        else:
            message = PARTIALLY_CORRECT_RESTRICTED_MESSAGE
    else:
        outcome, bucket = row.outcome, "incorrect"
        if rules.reveal_final_answer:
            message = f"Not quite. The answer is {row.expected_answer}."
        else:
            message = INCORRECT_RESTRICTED_MESSAGE

    display_number = (
        "?" if row.problem_id.startswith(AMBIGUOUS_PROBLEM_ID_PREFIX) else row.problem_id
    )

    return StudentResultView(
        problem_id=row.problem_id,
        prompt_text=prompt_text,
        student_answer_raw=student_answer_raw,
        outcome=outcome,
        display_bucket=bucket,
        display_number=display_number,
        glyph=_BUCKET_GLYPH[bucket],
        message=message,
        capture_id=row.capture_id,
        dispute=dispute,
    )


@dataclass(frozen=True)
class ResultsSummary:
    """The shape of a results page before a student reads a single row: how many
    right, how many worth another look, how many out of her hands entirely.
    `encouragement` is generated here, deterministically, from this session's own
    counts and problem numbers -- no model call, and deliberately no cross-session
    "same as last time" comparison (that needs a "previous session for this
    source" query that does not exist yet in k12ta.store.sessions; see
    docs/ROADMAP.md's parent-surface note). Still written to prompts/coach_voice.
    md's voice rules where they apply to what this layer actually has: specific
    (names real problem numbers, not "some questions"), brief, and never
    generic praise dressed up as personal -- see _encouragement below."""

    right: int
    to_look_at: int
    waiting_on_grownup: int
    encouragement: str


def _look_at_numbers(items: Sequence[StudentResultView]) -> list[str]:
    """Real, printed problem numbers among the to-look-at items, in results
    order -- an AMBIGUOUS_PROBLEM_ID placeholder ("?") is never named here,
    since there is nothing concrete on the page to point her at."""
    return [
        item.display_number
        for item in items
        if item.display_bucket in _TO_LOOK_AT_BUCKETS and item.display_number != "?"
    ]


def _encouragement(
    items: Sequence[StudentResultView], right: int, to_look_at: int, waiting: int, total: int
) -> str:
    if total == 0:
        return ""
    if to_look_at == 0 and waiting == 0:
        return "All correct." if right == 1 else f"All {right} correct."

    if to_look_at == 0:
        sentences = [f"{right} of {total} correct."]
    else:
        numbers = _look_at_numbers(items)
        if numbers:
            noun = "Problem" if len(numbers) == 1 else "Problems"
            verb = "is" if len(numbers) == 1 else "are"
            sentences = [
                f"{right} of {total} correct. {noun} {_join_labels(numbers)} "
                f"{verb} worth another look."
            ]
        else:
            sentences = [f"{right} of {total} correct."]

    if waiting == 1:
        sentences.append("One needs a grown-up to check.")
    elif waiting > 1:
        sentences.append(f"{waiting} need a grown-up to check.")

    return " ".join(sentences)


def summarize_results(items: Sequence[StudentResultView]) -> ResultsSummary:
    """Tally a session's already-rendered items into the three counts a parent-
    or-student-facing header shows before any individual row. Reads only
    `display_bucket`/`display_number`, both already policy-filtered by
    render_student_result -- a repeat's true correctness cannot leak into this
    tally any more than it can into its own row, since REPEAT is its own bucket,
    never folded into "correct" or "incorrect" here."""
    right = sum(1 for item in items if item.display_bucket == "correct")
    to_look_at = sum(1 for item in items if item.display_bucket in _TO_LOOK_AT_BUCKETS)
    waiting_on_grownup = sum(
        1 for item in items if item.display_bucket in _WAITING_ON_GROWNUP_BUCKETS
    )
    return ResultsSummary(
        right=right,
        to_look_at=to_look_at,
        waiting_on_grownup=waiting_on_grownup,
        encouragement=_encouragement(items, right, to_look_at, waiting_on_grownup, len(items)),
    )
