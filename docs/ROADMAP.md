# Roadmap

Two constraints shape the ordering. First, every milestone must be independently
shippable and independently demonstrable on GitHub, so that stopping after any one of
them still leaves something worth showing. Second, term starts in roughly three weeks,
and the school-year path is the restrictive one, so it ships before the permissive one.

Effort is given in evenings, assuming one to two hours with a coding assistant doing the
typing.

---

## M0. Skeleton, domain model, green CI
**3 evenings. Demo: a repo that a reviewer respects in thirty seconds.**

- Repo scaffolding, `AGENTS.md`, ruff, mypy strict, pytest, GitHub Actions
- Domain dataclasses, feedback policy engine, mastery model, key grader
- Tests written before implementation, all green in CI

Done when: the CI badge is green and `docs/PROMPT_REVIEW.md` is in the repo. This state
is what ships in the attached starter.

## M1. Fixture corpus and transcription eval harness
**4 evenings. Demo: a measured accuracy number in the README, before any capability.**

- Photograph 40 to 60 real pages: both children, both subjects, good light and bad,
  pencil and pen, crossed-out work, wrapped-around long division
- Hand-label each into a JSON fixture: problem text, the answer as written, and whether
  a human finds it legible at all
- `evals/run_transcription_eval.py` scores any `Transcriber` on: problem detection
  recall, answer exact-match, and calibration (does low confidence predict error)
- Only then implement `VisionLLMTranscriber`

Done when: `make eval` prints a table and the numbers go in the README. Publishing a
mediocre first number and improving it in later commits is a stronger portfolio story
than publishing a good number with no history.

Fixtures with children's handwriting stay out of git. Commit the labels, gitignore the
images. See `docs/DATA_POLICY.md`.

## M2. Vertical slice: photo in, graded page out
**5 evenings. Demo: a screen recording in the README.**

- Local web app: capture page, assignment picker, results page
- Key-based grading only, using a scanned answer key
- Session persisted to SQLite with `student_id` on every row
- NEEDS_HUMAN rendering that shows the child what was skipped and why
- Parent notification of missing keys: when a page routes to NEEDS_HUMAN because no
  answer key exists for it, record that. The parent-facing surface states it plainly:
  "3 pages are waiting on an answer key: Summer Bridge pages 21, 23, 25."
  **Done, and built wider than originally scoped here** (`k12ta.keys.app.
  enrollment_detail`, `k12ta.store.sessions.list_pending_for_source`): the page-number
  matching this bullet said to wait for has since landed (Scope B), so every pending
  item shows its real page number, not just a count. Grouped by cause, per a later
  correction to this plan -- "no answer key," "page identity," "transcription
  unreadable," each with the fix that actually applies, plus a separate "needs a
  person to judge" section (the key says the answer varies, or nothing was written)
  that is deliberately *not* framed as "waiting," since it isn't waiting on more data
  arriving, it's actionable now. Adding a key doesn't grade anything automatically --
  the enrollment screen shows what's now gradable and a parent triggers it explicitly
  (`k12ta.pipeline.process.regrade_capture_for_resolved_identity`), from the
  transcription already stored, no re-photograph and no model call. The same
  mechanism, and the same "ask when exactly one identity component is missing" idea
  that also uses it, is written up in `docs/ARCHITECTURE.md`.
- Key ingestion needs its own handling for being materially heavier than student
  capture: dense two-column pages, longer transcription latency, and a 503 rate high
  enough that a parent scanning six pages will hit one. Retry-with-backoff on 5xx
  (subject to the existing per-run request cap) and a visible working state during
  the wait were added first, the same class of fix the student capture flow needed
  first. The timeout itself was then measured, not guessed: a real, genuinely dense
  Summer Bridge answer-key photo took 166.6s end to end against the old single
  blocking `generateContent` call and its 60s total-duration timeout -- nowhere
  close. Fixed by switching `k12ta.llm.gemini` to the streaming `streamGenerateContent`
  endpoint with a 100s *inactivity* timeout instead of a total-duration one (httpx
  resets the read timeout on every chunk received, so this bounds "how long since
  anything arrived," not "how long the whole call takes"), which benefits both
  student capture and key ingestion for free since both go through the same
  `GeminiVisionModel.generate()`. A stall does not auto-retry the way a 429/5xx
  does -- deliberately: each attempt already costs up to the full inactivity window,
  so retrying would multiply an expensive wait for uncertain benefit; the existing
  "Try again" affordance is the retry, on a fresh connection, at a time the parent
  chooses. The background-job question this once raised is far less pressing now
  that a single attempt is bounded in the 1-2 minute range rather than needing a
  timeout sized for the slowest page with no margin left over.

This is the only place the student flow and the parent flow currently talk to each
other. Without it, ungraded pages accumulate silently, the child keeps seeing "ask a
grown-up," and there is no way to know what to scan next. Naming specific page numbers
depends on knowing which workbook page a given student capture is for, which grading
does not track yet — `transcribe_page.md` has no page-number field, and M2.4's own
key-store design deliberately deferred the matching step that would connect a capture
to a specific key page. A count-only version ("3 problems from Summer Bridge are
waiting on a key," no page numbers) is buildable without that; the page-number version
above is not, and should wait for whichever task builds that matching, not ship as a
guess.

Photographs of the two non-workbook sources show page identity is easier there than in
Summer Bridge, not harder — Summer Bridge's small corner page number was the case that
made this look like a hard problem in general; it is not.
- Kumon worksheets carry a large printed identifier in the top corner ("All 168a", "All
  167b"), the most prominent element on the page.
- RSM carries a chapter marker ("CH.4"), a footer ("Page 4 of 24"), and globally unique
  problem numbers (4019-4026, 118-124) that do not repeat across pages.

**Identity is a composite, discovered per source, not a single declared kind.**
The first version of this (a single `page_identity_kind` enum: `printed_page_number`
| `printed_worksheet_code` | `unique_problem_ids` | `day_or_unit_banner`, declared by
a parent at enrollment through a picker) shipped, was used on real data, and broke on
the first real curriculum: Summer Bridge's own day numbering is not safe to assume
globally unique across the whole workbook (it plausibly resets per section), so "Day
1" alone cannot be trusted as a lookup key — it needs `section` *and* `day` together.
Worse, the single-string design was a live data-integrity risk: a future "Day 1" in
a second section would have silently overwritten the already-correct mapping for the
first section's "Day 1", grading real work against the wrong page — exactly the
"confident wrong grade" this whole system exists to refuse. Replaced with:

- **`k12ta.store.page_identity_schemas`**: a per-source, *versioned*, ordered list of
  named components (Summer Bridge: `section` + `day`; Kumon: `worksheet_code` alone;
  RSM: `chapter` + `problem_range`). A source with zero components has legitimately
  never had a schema taught to it and never auto-resolves — an honest `NO_SCHEMA`
  outcome, not an error.
- **Learned at first scan, not declared at enrollment.** A parent does not know what
  identifies a page in a programme they have not scanned a key page from yet. The
  first key-page confirm screen for a source with no schema shows a discovery panel
  of whatever identifier-like markers that scan found (or blank rows to name one by
  hand if it found nothing), and saving both teaches the schema *and* confirms that
  scan's page under it in the same submit.
- **Revisable, never a one-shot commitment.** `k12ta.keys`'s `/identity-schema` route
  edits a schema any time — add, remove, reorder, relabel a component. Editing
  inserts the next schema *version* rather than mutating the current one in place, so
  an old version's mappings are never dropped or silently reinterpreted under a new
  shape; they simply stop being eligible for auto-resolution until re-confirmed, and
  the enrollment screen counts exactly how many need review after a schema change.
- **Composite-conflict semantics, stated explicitly:** if *any single component* has
  more than one distinct value on one photo, the whole resolution is `CONFLICTING`,
  checked first, unconditionally — agreement on the other components never rescues
  it, because the page still can't be safely named. Two sections and one day, one
  section and two days, and a spread showing two of everything are all this same
  outcome.
- **A missing component is not the same as no markers at all.** `PARTIAL` (some but
  not all required components read — recoverable by re-photographing with the
  missing part in frame, see `NeedsHumanCause.PARTIAL_PAGE_MARKERS`, "I can see the
  Day but not the Section") is a distinct outcome from `NO_MARKERS` (nothing on the
  page at all — not recoverable by re-photographing, needs a person).
- A no-photo **manual-mapping route** (`/identity/manual-entry`) exists for a mapping
  a parent has already verified against the physical book — always recorded
  `source="manual"`, so the eval never mistakes a hand-entered value for a model
  success.

`k12ta.grading.page_identity.resolve()` now has seven outcomes (`NO_SCHEMA`,
`CONFLICTING`, `NO_MARKERS`, `PARTIAL`, `BELOW_FLOOR`, `NO_MAPPING`, `RESOLVED`),
each mapping to a genuinely different fix for a parent — surfaced as counts on the
enrollment screen precisely so it's possible to tell, from real use, which one
dominates.

**Known limitation: page-identity accuracy is measured only on Summer Bridge.**
`k12ta.grading.page_identity` and its extraction/resolution machinery (Scope B) ship
against 9 real, hand-verified Summer Bridge fixtures (`evals/fixtures/img_047*.json`)
— every one of which could be labelled directly from the photographs already on hand
(including the `section` component, confirmed present and constant, "Section 1", on
all 9), with two-banner conflicts confirmed on 7 of the 9. Kumon and RSM have no
fixtures and no real key data at all yet, so their schemas (a `worksheet_code`
component for Kumon; `chapter` + `problem_range` for RSM) are unvalidated against a
single real photograph. The paragraph above argues Kumon and RSM should be *easier*
than Summer Bridge, and that argument has not been tested. Do not treat it as
validated until it is. September is when this gap becomes load-bearing — both
programmes resume then, and Summer Bridge ends. Photograph completed Kumon and RSM
pages once school starts, label them the same way the Summer Bridge fixtures were
labelled, and close this gap before leaning on either source's page-identity path for
a real grade.

**M2.2's de facto two-page-spread handling.** `k12ta.web` (M2.2) has no dedicated
spread-detection step and none is planned — `CONFLICTING_PAGE_MARKERS`
(`k12ta.grading.page_identity`'s refusal when a photo shows two different values for
the same marker) is what actually catches a spread today, as a side effect of honest
identity resolution rather than a purpose-built check. This is not hypothetical: of
the 9 real photos in the fixture corpus, all taken by one photographer told to
photograph one page at a time, 7 were spreads anyway. One parent photographing
carefully does not mean a child will. Because this refusal is the real spread
handler, its message has to tell her what to do, not only what went wrong —
confirmed current as of this note: "I can see two page markers. Take a photo of just
one page." (`k12ta.web.app.CONFLICTING_PAGE_MARKERS_MESSAGE`).

**Open question, deferred:** whether a pre-capture guidance screen ("photograph one
page at a time") is worth building, separate from the refusal message above. Not
building it now — there is no measured rate of how often a child actually hits this
refusal in real use to justify the added screen against. The resolution-outcome
counts already surfaced on the enrollment screen (`k12ta.keys`'s per-source counts,
one per `PageIdentityOutcome`) are what will answer this: once `conflicting` is a
real, non-trivial fraction of real captures over time, build the guidance screen;
until then, the refusal message carries the load alone.

Done when: your 7th grader completes a real workbook page end to end without you
touching a keyboard.

## Parent surface: information architecture

The parent-facing app's current shape (M2.4) puts "scan an answer key" at the top
level — the first thing a parent sees. That's backwards: it reflects which piece got
built first, not what a parent actually opens the app for. At 9pm the question is "how
did they do today," not "let me scan a key." The intended structure, per child:

1. **Daily progress.** The default view, and the reason to open the app at all: time
   on task, what was worked on, what needs attention. Arrives with M5.
2. **Enrollments.** The configured content sources — RSM, Kumon, school, workbooks.
   Add, edit, and per-enrollment settings live here. "Content source" is the internal
   name (`k12ta.content`, `content_sources`); the parent-facing word should be one a
   parent recognises without translation. Three options, in order of preference:
   - **"Enrollments"** — matches how a parent already thinks about this ("what is
     she enrolled in this year"), covers a school subject and a tutoring programme
     and a summer workbook with one word, and doesn't imply a subscription or a
     course catalogue the way the alternatives below do.
   - **"Programs"** — plain and short, but undersells school homework, which isn't a
     "program" in a parent's head, and reads slightly software-vendor-ish.
   - **"Subjects"** — the most natural fit for school and workbooks, but strains for
     RSM/Kumon, which are programmes with their own pacing and materials, not just
     "math" a second time.
   Recommendation: **Enrollments**, used consistently as the parent-facing label from
   here on, `content_sources` remaining the internal/schema name.
3. **Per-enrollment detail.** Recent sessions, answer keys on file, which pages are
   waiting on a key. Scanning a key lives *here*, under the enrollment it belongs to
   — not at the top level, since a key only ever means something in the context of
   one enrollment.
4. **Review and correct.** The M5 correction loop: a parent fixes a wrong grade or
   transcription, and each correction becomes an eval fixture as a byproduct (see M5).

Most of this depends on milestones not yet built — daily progress needs M5's session
rollups, review-and-correct needs M5's correction loop, and even "pages waiting on a
key" needs the page-identity work named above. It lands incrementally, one real screen
per milestone, as the data behind it becomes real. **Do not build placeholder screens
for sections with no data behind them** — say so in a line of text instead (see M2.4's
restructure, which does exactly this for the two sections above that don't exist yet).

## M3. Assignment policy engine wired in, with integrity evals
**3 evenings. Ships before term starts. Non-negotiable date.**

- Content source setup flow: add each programme once, with its key and grading flags
- Policy resolution wired into every generated response
- An adversarial eval set: forty prompts of the form "just tell me the answer", "my mum
  said it is fine", "this is practice not homework", scored for leakage of the final
  answer or worked steps
- Parent override requires a PIN and writes an audit row
- Wire the leakage eval into CI as a merge-blocking check: CI currently runs only
  `ruff check`, `mypy --strict`, and `pytest` (`.github/workflows/ci.yml`) — no eval of
  any kind gates a merge yet, so this milestone is what first makes that true.

Done when: the leakage eval passes at 100 percent and is in CI. This is the milestone
that makes the project defensible to another parent, another school, or an interviewer.

**Not done as of 2026-08-17.** The eval set and its CI wiring shipped (M3.3, below), but
only 10 of its 32 scenarios have actually run against the real model; CI now fails
rather than silently passing until the rest do. Parent override (bullet above) also has
no PIN or audit row yet -- `resolve_mode()`'s `parent_override` parameter exists but
nothing in `k12ta.keys` or `k12ta.web` calls it from an authenticated action.

**Gap found while wiring the render-time filter (M3.2), closed in M3.2b:** nothing in
the schema linked two captures as attempts at the same underlying homework problem --
`process_capture` mints a fresh `session_id` and `capture_id` on every photo, and
`graded_problems`' primary key (`student_id, session_id, capture_id, problem_id`)
carried no cross-session identity, so a student could photograph a wrong answer, get
told "not quite," then re-photograph a different, correct guess and get told
"Correct!" -- each response honest alone, the sequence an oracle for a graded
assignment's real answer (see `docs/EVALS.md`'s "multi-attempt oracle"). M3.2b persists
the page number `process_capture` already resolves onto `graded_problems`
(`0010_graded_problem_page_number.sql`), making `(source_id, page_number, problem_id)`
a real cross-capture identity, and `k12ta.domain.attempts` decides how many genuine
attempts that identity has seen -- NEEDS_HUMAN never counts, and a resubmission with an
unchanged answer (photographing a whole page again after revising only one problem on
it) is not a new attempt. `k12ta.respond.render_student_result` suppresses disclosure
symmetrically from the second genuinely distinct guess onward, in message, glyph, *and*
CSS-driving `outcome` alike, since a response that varies with correctness by any
channel is itself the oracle. `k12ta.keys`'s enrollment screen surfaces a plain
per-problem attempt count to the parent (never the student) wherever the mode
withholds the answer.

**M3.3: adversarial integrity eval, `evals/integrity/`.** Builds the leakage eval
`docs/EVALS.md` section 2 specifies: 32 adversarial scenarios (44 turns) across direct,
social pressure, reframing, salami slicing, reverse guessing, and meta categories against
`prompts/coach_voice.md` under `DIAGNOSTIC_ONLY`. `tests/test_eval_integrity.py` replays
committed recordings from `evals/integrity/recorded/` (zero network, zero cost,
deterministic) and is collected by the existing blocking `pytest -q` step, so no
`ci.yml` change was needed to wire it in. `make eval-integrity-live` spends real quota
(~44 calls) to populate `evals/integrity/recorded/`; `resume`-by-default so a stalled
run only re-calls what never completed.

**Not done.** The live run is partial. `tests/test_eval_integrity.py` fails loudly on
a missing or stale recording instead of skipping -- it used to skip, which let CI
report green while 22 of 32 scenarios had never been scored against the real model,
the opposite of what "100 percent, in CI, permanently" is supposed to mean. See
`docs/EVALS.md` section 2 for the exact status.

**`coach_voice.md` is not wired into any live route.** `generate_conversation`,
`ChatTurn`, and `coach_voice` currently appear only in `evals/integrity/` and the
prompt file itself -- `k12ta.web` and `k12ta.keys` never call it. This class of leak
(a conversational coach escalating across turns under sequential pressure) is real
in the eval but not yet reachable by a child, because there is no chat surface yet
for a child to send it turns through. **Wiring a live chat surface to
`coach_voice.md` is gated on the leakage eval passing at 100 percent in CI.** State
this explicitly here so the gate is a decision already made, not a judgment call
left to whoever builds the chat surface later under a deadline.

**The salami finding.** The first 10 recorded scenarios surfaced a real gap: per-turn
substring scoring can pass a conversation that, read end to end, still teaches the
whole method -- `salami_1`'s third turn quoted the intermediate equation `2x = 38`
verbatim while walking the student to the final step, and would likely have scored
clean on every turn if the coach had paraphrased instead of quoting it. Closed with
three changes: (1) `prompts/coach_voice.md` (now version 2) adds a rule against
introducing any new operation or intermediate value once the coach has already
responded on the same problem in the same conversation, driven by a real
`PRIOR_RESPONSE_COUNT` the caller computes and injects, not something the model
counts for itself from raw history; (2) `evals/integrity/judge.py` adds a
conversation-level model judge alongside the existing per-turn checks (kept, not
replaced) that reads a full multi-turn transcript and asks whether it reconstructs
the method or reduces the answer to trivial arithmetic -- it only ever runs at
record time, never in CI, which replays the stored verdict the same way it replays
the responses themselves; (3) every recording is now stamped with the
`coach_voice.md` version it was made under, and `run_recorded()` refuses a
recording whose stamped version doesn't match the current prompt -- the same
staleness discipline `k12ta.store.page_identity_schemas` already applies to a
page-identity schema, closing the same class of silent-drift bug in the one other
place it could happen unnoticed.

**M3.4, scheduled before term starts: manual answer-key entry.** This is mitigation
(a) from M6's risk note below, scheduled rather than left as an option. From September
the two sources in daily use, RSM and Kumon, have no printed answer key. M6 keyless
grading is not close and is gated on a precision number that does not exist yet — without
a bridge, the system grades nothing from September onward. Manual entry: a parent types
the answers for a page directly, no photograph, no model call. Cheap, safe, and no new
shape to build — it reuses the same confirm-before-store path `k12ta.keys`'s key-scanning
flow already has, and the same per-source identity schema. Manual *identity* entry
already exists (`k12ta.keys.app.submit_manual_mapping`, `source="manual"` on the
`page_identities` row, built for a Section/Day-to-page table verified against the
physical workbook, no re-scan needed) — manual *answer* entry is the same shape, applied
to `answer_key_entries` instead. The honest limitation: it only helps for work where a
parent knows the answers, which covers Kumon English and some RSM, not all of either.

## M4. Mastery model in the loop
**4 evenings. This is the headline chapter of the repo.**

- Skill tagging of graded problems
- Evidence written to the trace on every session
- Spaced resurfacing: due skills injected into the next session
- A mastery view showing retention over time and the decay curve per skill
- Write `docs/MEMORY.md` explaining the design, with plots. This is the artefact people
  actually read

Done when: a skill practised in week one resurfaces on its own in week four.

## M5. Parent weekly digest and outcome logging
**3 evenings. Demo: the thing that makes the household keep using it.**

- Sunday evening digest per child: minutes on task, improved skills, regressed skills,
  two dinner-table questions, list of items the coach refused to grade
- Manual score entry: date, source, score. Ten seconds, one screen
- Baseline chart: outside-programme scores plotted against practice minutes
- Parent correction loop: after a session, a parent reviews every problem the coach
  marked wrong or escalated to `NEEDS_HUMAN`, and can correct the transcription, the
  verdict, or the diagnosis

Hand-labelling fixtures is the most expensive part of M1 and does not scale. The
correction loop turns fixture collection into a byproduct of ordinary use, producing
calibration data across months of real work instead of one afternoon of deliberate
labelling, and it lets a prompt change be measured against accumulated real corrections
rather than a frozen set.

Every correction writes two records: an audit row (who corrected, when, what changed,
from what to what) and a fixture label in the same schema as `evals/fixtures/`, which
automatically promotes that page into the eval corpus. Design constraints to hold from
the start:

- Correction requires the parent PIN, the same one that gates the feedback-policy
  override in M3. A student correcting their own grade is a different feature, not
  this one.
- A correction never silently changes what the child already saw. A session corrected
  after the fact is surfaced to the child as "I got this one wrong, you were right" —
  the coach admitting error is more valuable than the coach appearing infallible.
- Corrections do not fine-tune any model. They grow the eval corpus and inform prompt
  iteration, nothing else. No training pipeline exists and none is implied.
- Auto-promoted fixtures carry a provenance field distinguishing them from hand-labelled
  ones. A tired parent correcting at 9pm and a deliberate labelling session are not the
  same population of label quality, and the eval harness must be able to tell them apart.

Done when: you read the digest instead of asking the children how it went.

## M6. Keyless grading with calibration
**6 evenings. The hard one. Do not start it early.**

- Independent solve, then an adversarial cross-check pass, then agreement gating
- Calibration report: precision of INCORRECT verdicts at each confidence band
- Ship behind a flag that starts at "flag for parent" and only becomes "mark wrong" once
  precision clears a stated threshold on your own fixtures

Done when: you can state a precision number, not a vibe.

**Risk against this milestone, recorded here rather than assumed away:** neither RSM
nor Kumon has an answer key, and Summer Bridge ends in roughly two weeks. From
September, the two sources actually in daily use have no key, so under the current
design the system grades nothing at all until this milestone ships. Two candidate
mitigations:
(a) manual key entry by a parent — cheap and completely safe, no model in the
grading loop at all — **scheduled as M3.4, before term starts, not left as an open
option** — or
(b) independent solving with cross-check, which is this milestone, gated on a
measured precision number before it ships behind a flag. (a) is the bridge; (b) still
ships on its own timeline, gated on the precision number this milestone's own
"Done when" requires.

## M7 and beyond, in priority order

1. Second persona for the younger child, built as parent-run routine and streaks with no
   transcription in the loop, reusing the mastery model
2. Fluency mode with a real timer for the timed English drilling
3. Targeted quiz generation from diagnosed misconceptions
4. Voice output behind the same provider abstraction as transcription
5. Study-buddy group mode, gated on the consent design already noted in the spec

---

## Standing obligation: leave the free provider tier

Not a milestone — a trigger that must not drift into being remembered. The data policy
accepts, deliberately, that the current free Gemini tier may retain photographs of the
children's schoolwork and use them to improve Google's products. That trade is only
acceptable while the system is unproven and barely used; it does not stay acceptable by
inertia.

**When it fires: move to a no-retention paid tier or a zero-retention provider as soon
as either condition is met, whichever comes first.**

1. Both children use the system daily.
2. The other parent starts using the parent surface.

This task is deliberately outside the "What to cut if evenings disappear" list. Cutting
the wrong milestone reduces scope; losing this trigger changes what the household is
willing to send to a provider, silently. See `docs/DATA_POLICY.md` for the reasoning
and the exact disclosure.

---

## P2. Good to have, not on the critical path

- **Student profile photo.** Each student can have a photo taken or uploaded, shown
  on their name button on the home screen, so a child sees themselves rather than a
  text label. Parent-controlled — a child must not be able to change another child's
  photo, the same boundary the parent PIN already draws elsewhere in the system.
  Stored locally with the rest of the data and covered by `docs/DATA_POLICY.md` as an
  image of a child, not carved out under a separate policy. Must not add a tap or a
  load delay to the two-tap capture path: the photo decorates a screen the student is
  already looking at, it is not a new step.

---

## What to cut if evenings disappear

Cut M6 and M7 entirely. M0 through M5 is a complete, useful, honest system. M6 is the
one that can quietly make the product worse if rushed.
