# Progress

What a reviewer reads to understand how the project actually went. Ordered by milestone,
written after the milestone closes, and biased toward what went wrong — a list of things
that worked is not useful to the next person hitting the same wall.

## M0. Skeleton, domain model, green CI

The initial commit bundled the repo scaffolding, the domain dataclasses, the feedback
policy engine, the mastery model, the key grader, and the GitHub Actions CI workflow in
one pass, written and self-reviewed before CI ever ran against it. Two latent failures
were sitting in that commit undetected: a line in `tests/test_policy.py` over the
configured 100-character limit, and a `mypy --strict` violation in the mastery model,
where `retention_on()` returned `Any` instead of `float`. The cause of the second one was
specific enough to be worth recording: `float.__pow__` is typed `Any` in typeshed,
because a negative base with a fractional exponent can return a `complex`, so
`0.5 ** (...)` silently propagated `Any` through the surrounding `min`/`max`/`round`
calls. Neither failure was visible by reading the code or running it locally with a
casual `pytest`; both surfaced the first time CI actually ran, in the very next commit.
That is the argument for wiring CI on day one rather than after the first milestone:
lint and strict-typing violations are cheap to fix in isolation and expensive to
untangle once three more commits sit on top of them.

## M1. Fixture corpus and transcription eval harness

The detection-precision number was confounded by an instrument flaw, not a model
failure. A `two-page-spread` fixture labels only one of its two pages, so a detection
on the unlabelled page had no fixture item to match and was scored as a spurious
hallucination it never committed. The `layout` and `spread_side` fields, added to the
fixture schema days earlier for an unrelated reason, made the gap visible enough to
fix: unmatched detections on a spread page are now reported as `unattributed_items`,
excluded from precision and recall.

A 429-retry loop with no circuit breaker, plus a throttle applied only between pages
and not between retry attempts, burned 108 requests against a 9-page corpus. Nothing
in any log or test surfaced this; it was visible only on the provider's usage
dashboard. Fixed with provider-agnostic failure classification, a per-run request
cap, and a throttle applied to every attempt, including retries.

Two other findings were caught, not shipped. A WebFetch summary of Gemini's API
documentation fabricated detail twice — request/response shape, then the free-tier
rate limit — caught only by pulling raw HTML instead of trusting the summary. Reading
the free tier's terms of service directly surfaced that it permits Google to use
submitted content, including human review, to improve its products. That changed
`docs/DATA_POLICY.md`: the M1 corpus is stated as running on a tier that is not
data-safe, and a zero-retention tier is now a recorded prerequisite before M2 sends
real schoolwork through the same adapter.

## M2. Vertical slice: photo in, graded page out

A capture page rendered as a blank black screen when the database had no students:
no screen had an empty state, and the only test asserted a 200 status code, not
whether a person could act on anything.

The framing-guide overlay specified in M2.2 was impossible on iOS: a
`capture="environment"` file input hands the whole screen to the native camera, with
no room for an overlay. Guidance moved before capture; validation after.

Every graded problem was told "I don't have an answer key for this one" for items no
key had ever been looked up for — an assertion never checked, in exactly the place
the design forbids it. Fixed by making cause determination explicit.

Seven of nine fixture photographs were two-page spreads despite careful photography.
Spreads are the common case, not an edge case; the eval harness and grading pipeline
were both rebuilt around that fact.

The page-identity design broke on the first real curriculum: Summer Bridge has three
sections, each with Days 1-20, so the day banner alone is not unique. About 70% of
that work was reworked a day after shipping — cheap only because it was a day old.

Discovery mode did not spontaneously report the section banner on a real photo. The
manual parent fallback is load-bearing, not a backstop.

The first four real grades this system ever produced were 50% unjust: two of four
marked INCORRECT on a live capture were a student writing a more specific correct
name than the key's general one ("rhombus" vs. "quadrilateral", "Square" vs.
"rectangle"). Exact-string matching, correct for numeric answers, was silently wrong
for free-text ones. Fixed by routing a non-numeric key mismatch to its own
NEEDS_HUMAN cause (`ANSWER_DIFFERS_FROM_KEY`) instead of asserting INCORRECT --
deliberately not a synonym or taxonomy system, and deliberately not a model judging
equivalence: both are unmeasured confidence in exactly the place a wrong mark costs
the most. See `docs/EVALS.md`'s M1 section for the related calibration gap this
surfaced alongside.

Extending exact-match to tolerate a unit label ("496" vs. key "496 ft²") introduced
two near-misses, both caught by sweeping the real production key data before wiring
anything up, not by the tests written first: a thousands-comma key ("2,122.64 m²")
truncated to "2" under a naive "digit-run then anything" grammar, and a real algebra
key ("28y") had its variable stripped as if it were a unit, so a bare "28" would have
been credited as correct. Neither was hypothetical -- both are live key entries on
this project's own pages 33 and 61. Fixed by requiring a real boundary: the unit must
be whitespace-separated from the number and contain no digit anywhere, otherwise
`numeric_part` returns None rather than a guessed value. Same lesson as the mark
above: tests against invented examples pass while missing failure modes the real data
already contains.
