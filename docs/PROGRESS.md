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

A capture page rendered as a blank screen when the database had no students, because
no screen had an empty state and the existing test only asserted a 200 status code —
it passed against a page with nothing a person could act on. The framing-guide overlay
meant to show one-page-vs-two-page examples before the shutter turned out to be
impossible on iOS: a `capture="environment"` file input hands the whole screen to the
native camera, which has no room for an overlay. Guidance moved before capture (a
static guide, shown before the camera opens) and validation moved after (a reject
gate on the uploaded photo), rather than attempting a live overlay iOS cannot host.

Before the answer-key store existed, every graded problem was told "I don't have an
answer key for this one yet" unconditionally — correct by construction, since no key
existed anywhere yet, but nothing in the pipeline actually checked; the same code
would have kept saying it after a key was added, silently wrong. Fixed once
`answer_key_entries` existed, by making cause determination an explicit decision
(`k12ta.grading.needs_human`) that looks up a real key entry instead of asserting its
absence.

A file-editing tool silently truncated a template file; a stale cached read of that
same file, taken instead of a fresh terminal read, showed the file as intact and let
the truncation stand uncaught through a further edit. Fixed by treating an edit
tool's own success message as unverified until a terminal command re-reads the file.
