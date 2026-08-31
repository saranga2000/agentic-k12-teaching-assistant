# Progress tonight — 2026-08-30/31 session

Working the 6-item order from the post-clarification work order. Committing after each
numbered item. Live model call budget: 5 pages max, for item 6's smoke test only —
running count logged below when that item starts.

## Status

- [x] 1. Unwire evals/integrity/ from blocking pytest; keep multi-attempt oracle blocking
      — commit 7330f29. Only `tests/test_eval_integrity.py` marked `integrity` and
      excluded from default `pytest -q`; scorer/runner/judge/prompt unit tests untouched
      and still block. `make check-integrity` runs it by hand; still fails honestly on
      salami_3/reverse_3, as expected. Also fixed 10 files' pre-existing ruff-format
      drift so `make check` is actually green. tests/browser/test_multi_attempt_oracle.py
      needed no change -- already lives in tests/browser/, already runs via the separate
      always-blocking `browser-tests` CI job, untouched by this item.
- [x] 2. Unicode NFC normalisation in grading path + Tamil tests -- committed.
      `key_grader.normalise()` and `grade_against_key()` both run `unicodedata.
      normalize("NFC", ...)` before any comparison. Real test: "கொடி" built two
      byte-different ways (U+0BCA precomposed vs. U+0BC6+U+0BBE decomposed vowel
      sign), via explicit \uXXXX escapes so the test doesn't depend on any tool
      preserving raw non-ASCII bytes -- confirmed the escapes actually round-trip
      correctly on disk before trusting them. Test failed before the fix (both new
      tests), passes after. Scope note: page_identity.py's composite-key string
      equality (chapter/section names, which could also be non-English) was NOT
      touched -- that's page identity matching, a different comparison from
      grading an answer against a key, and wasn't part of this item's ask.
- [x] 3. Verdict model: `answered: bool`, `partially_correct` verdict, migration --
      committed. Migration 0024 (`answered`, backfills existing rows to 1, verified
      by hand against a simulated pre-existing DB). `GradeOutcome.PARTIALLY_CORRECT`
      replaces the unused `PARTIAL`. Wired into: `k12ta.domain.attempts` (oracle
      suppression), `k12ta.respond.render` (glyph/bucket/message, gated by feedback
      policy, with a None-safe expected_answer fallback since M6's keyless path
      won't always have one), `k12ta.store.sessions` (resolved/decisive filters),
      `k12ta.web.app` (new named `_GRADED_DISPLAY_BUCKETS` constant -- the bare
      2-tuple it replaced is exactly the kind of thing that silently drops a new
      verdict, confirmed by temporarily reverting it and watching the new test fail),
      `k12ta.keys.app` (verdict button, counts, items, template section). Also made
      partially_correct disputable by the child, same as incorrect, in both
      session_result.html and my_pages.html. Full suite + browser suite + mypy +
      ruff all green.
- [x] 4. Content source `keyed | keyless` + `archived`; keyed-never-guesses rule --
      committed. Reused the existing `has_answer_key` field rather than renaming it
      (it already meant exactly "keyed", was already asked at setup, but had no
      switch function and was never actually read by grading -- confirmed dead via
      grep before touching anything). Added `content.set_has_answer_key` (switch,
      no regrade) and `content.set_archived` + migration 0025 (`archived` column).
      `k12ta.web.app.submit_capture` blocks a new upload to an archived source with
      its own message. `manage_source.html` gets two new controls. The "keyed never
      guesses" rule turned out to already be fully true and tested since M2
      (`decide()` -> NO_KEY_FOR_PAGE) -- added a docstring connecting it to the flag
      explicitly rather than a redundant test. Did NOT touch k12ta.content.source/
      registry.py's ContentSource/example_sources -- confirmed dead code (only
      SourceKind is imported live from that module), not worth the churn.
- [ ] 5. M9a design system (tokens + shared stylesheet only, not 9b polish)
- [ ] 6. M6 agentic evaluator (offline first: ladder, prompts, parsing, tests)

Live model calls spent so far: 0 / 5 (budget applies to item 6 validation only)

## Current item: 5

Starting M9a (design system only, not 9b's polish pass). Need: design tokens (colour,
type scale, spacing, radius, state), one shared stylesheet consumed by both apps
(today k12ta.web and k12ta.keys each have their own Jinja2Templates directory with
physically duplicated markup), plus designed empty/failure states per AGENTS.md rule
11. Hard constraints: no build step/toolchain, the two-tap capture path must not grow
a tap, accessibility baseline, one design for both children (no grade-band variation
-- that's V3). Retrofit existing screens only as far as quota allows; a new
inconsistent screen is not acceptable, a partially-retrofitted old one is.

## Questions for the morning

(none yet)
