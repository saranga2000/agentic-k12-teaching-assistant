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
failure. A `two-page-spread` fixture labels only one of the two visible pages, so any
detection the model made on the unlabelled page had no fixture item to match against and
was scored as a spurious hallucination it never committed. The `layout` and
`spread_side` fields, added to the fixture schema to record which side was labelled,
made the gap visible enough to fix: unmatched detections on a spread page are now
reported as `unattributed_items`, excluded from precision and recall, since the fixture
cannot say whether they were invented or correctly read from the unlabelled side.

Two other findings from M1.4 are worth recording because both were caught, not shipped.
A WebFetch summary of Gemini's API documentation fabricated detail twice — the
`generateContent` request/response shape, then the free-tier rate limit — caught only by
pulling the raw HTML instead of trusting the summary. Separately, reading the free
tier's terms of service directly surfaced that the tier permits Google to use submitted
content, including human review, to improve its products — not a detail training data
would reliably know. That changed `docs/DATA_POLICY.md`: M1's eval corpus is stated
plainly as running on a tier that is not data-safe, and a paid or zero-retention tier is
now a recorded prerequisite before M2 sends real schoolwork through the same adapter.
