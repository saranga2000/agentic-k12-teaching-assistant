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
- [ ] 3. Verdict model: `answered: bool`, `partially_correct` verdict, migration
- [ ] 4. Content source `keyed | keyless` + `archived`; keyed-never-guesses rule
- [ ] 5. M9a design system (tokens + shared stylesheet only, not 9b polish)
- [ ] 6. M6 agentic evaluator (offline first: ladder, prompts, parsing, tests)

Live model calls spent so far: 0 / 5 (budget applies to item 6 validation only)

## Current item: 2

Starting. `k12ta.grading.key_grader.normalise()` is the comparison entry point
(`src/k12ta/grading/key_grader.py`) -- need NFC applied there, before the existing
lower/whitespace/unit normalisation, plus real Tamil test strings that differ
byte-wise but not visually (a combining vowel sign vs. its precomposed form).

## Questions for the morning

(none yet)
