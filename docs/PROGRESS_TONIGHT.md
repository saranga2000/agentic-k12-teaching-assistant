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
- [x] 5. M9a design system -- committed. New `src/k12ta/design/tokens.css`, one
      physical file, mounted at `/static` independently by both apps (StaticFiles,
      no new dependency), proven byte-identical by a direct test. Colour tokens kept
      their existing names (unchanged, dozens of templates reference them); new
      type/spacing/radius scales added; the CSS genuinely identical between both
      apps' base.html before this pass (lightbox, working-spinner keyframes,
      [hidden], base reset) moved here once. New: a :focus-visible ring and a
      44x44px minimum scoped to button-shaped controls only (not every <input> --
      would have undone k12ta.keys' own dense-table tradeoff). Both base.html files
      keep only their genuinely different layout (child kiosk .screen vs parent
      scrolling document), now referencing shared tokens. Full suite + browser suite
      + mypy all green. NOT done, left for 9b: retrofitting the new scales into any
      other screen, merging _photo_source.html's two copies (a real separate design
      decision -- child app's large buttons vs parent app's normal ones -- not a
      mechanical extraction), sharing the lightbox HTML/JS (only its CSS was in
      scope), and a deliberate empty/failure-state audit.
- [x] 6a. M6 agentic evaluator, OFFLINE portion -- committed. New
      `k12ta.grading.evaluator`: `evaluate_keyed_mismatch` (1 call) and
      `evaluate_keyless` (2 independent calls, agreement-gated -- not one call with
      self-critique, per "the cross-check is load-bearing, not ceremony").
      `should_escalate_to_vision` is real tested policy; nothing calls it (tier 3 not
      built). New prompt `prompts/evaluate_text.md`. Wired into
      `k12ta.pipeline.process` behind two independent flags
      (`Settings.evaluator_enabled`, `Settings.evaluator_mark_wrong_enabled`), both
      default False -- confirmed by running the entire pre-existing suite unchanged
      after wiring (byte-identical default behaviour, not just asserted). Critical
      boundary tested directly: NO_KEY_FOR_PAGE only escalates on a source actually
      configured keyless, never on a keyed source still waiting for its key scanned.
      Live-wired into k12ta.web.app (`get_text_model`, mirrors `get_transcriber`) so
      the flags actually reach live code, not just tests. 22 new offline tests
      (tests/test_grading_evaluator.py, tests/test_pipeline.py), all model calls
      faked (tests/fakes.py's new FakeTextModel). Full suite + browser suite + mypy
      all green throughout.
- [ ] 6b. M6 LIVE portion -- NOT STARTED. Deliberately held for explicit
      confirmation before spending real API budget or touching real household data,
      rather than run automatically at the end of an already-large offline pass. See
      "Questions for the morning" below.

Live model calls spent so far: 0 / 5

## Current item: none -- all 6 items' offline work is complete and committed.

Every numbered item (1-5 fully, 6's offline half) is done, tested, and committed.
Nothing further to do without either (a) a decision on the live smoke test (6b), or
(b) new direction. Not proceeding to 6b without explicit confirmation -- see below.

## Questions for the morning

1. **Run the M6 live smoke test (6b)?** Real household data (Summer Bridge pages
   with both a confirmed key and real child captures on disk), real API spend,
   capped at 5 pages. Held for explicit confirmation rather than run automatically.
2. **A more specific NeedsHumanCause for an evaluator-gated INCORRECT?** Currently
   reuses whatever cause `decide` already gave the row (ANSWER_DIFFERS_FROM_KEY or
   NO_KEY_FOR_PAGE) rather than a new cause naming the evaluator's own involvement.
   Safe (the child sees nothing different either way), but a parent reviewing the
   queue can't yet tell "the evaluator flagged this" apart from "nothing has looked
   at this yet." Deferred rather than adding a fifth cause's render/migration
   surface in the same pass as everything else.
3. **Multi-part sub-item splitting (`5a`...`5g`) is not implemented at all.** Real
   structural change (one transcribed item -> one graded_problems row today, always)
   -- needs its own design pass, not a decision made solo tonight.
4. **Vision (tier 3) is not implemented.** `should_escalate_to_vision` exists and is
   tested; nothing calls it yet.
