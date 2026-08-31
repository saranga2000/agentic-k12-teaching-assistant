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
- [ ] 4. Content source `keyed | keyless` + `archived`; keyed-never-guesses rule
- [ ] 5. M9a design system (tokens + shared stylesheet only, not 9b polish)
- [ ] 6. M6 agentic evaluator (offline first: ladder, prompts, parsing, tests)

Live model calls spent so far: 0 / 5 (budget applies to item 6 validation only)

## Current item: 4

Starting. Need: ContentSource domain/store model, enrollment setup form (both apps?
or just k12ta.keys), the "no key on file -> wait, don't guess" rule made explicit in
process_capture, and an archived flag threaded through upload/review/report-card
queries so an archived source's queue stays workable but closed to new uploads.

## Questions for the morning

(none yet)
