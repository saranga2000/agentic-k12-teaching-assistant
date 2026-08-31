# Architecture

## Shape

```
capture (tablet browser)          -- V1
  -> ingest: image on disk, PageCapture row                                -- V1
  -> transcribe: Transcriber adapter -> TranscribedItem[] with confidence  -- V1
  -> grade:  key path (deterministic)  |  keyless path (M6, model + cross-check) -- V1
  -> diagnose: misconception per error, model call, prompt from prompts/   -- V2, see note
  -> respond: policy filter decides what may be said                      -- V1
  -> mastery: evidence folded into per-skill traces with decay            -- V2
  -> schedule: next session composed from due skills                     -- V2
  -> digest: weekly rollup for the parent                                 -- V2
```

**Note added 2026-08-30**, after `docs/ROADMAP.md` narrowed V1 to an evaluator with the
parent as final authority and moved mastery/scheduling/the weekly digest to "V2. Learning
intelligence": the three stages marked V2 above do not ship as part of V1.

**`k12ta.diagnose` is resolved: V2, not V1.** This was an open question here until the
2026-08-30 V1 clarification settled it. The reasoning: V1 does owe a child an explanation
of why an answer was judged wrong, but that explanation is **the parent's own written
comment**, attached when they resolve a dispute or a flagged item — a human sentence, not
a generated diagnosis. Nothing in V1 needs a model to infer a misconception, and the
other job `diagnose` was scoped for (skill-tagging into `graded_problems.
diagnosis_skill_ids`) only has a reason to exist once M4's mastery model does, which is
V2. So the package is not built in V1 at all. Do not create it for the explanation job —
that job is a text column and a form field.

**The grading stage is materially bigger than this diagram implies as of the same
clarification.** `grade` above is now two paths chosen per program (a parent declares
which at setup) plus one shared rule:
- **keyed** — the parent supplies answers; a page with no key on file is never
  evaluated, it waits.
- **keyless** — the model generates the answers; this is V1's core capability, not a
  fallback.
- **deterministic first, agent second, in both paths** — an exact key match after Unicode
  NFC normalisation is settled without a model call; everything else (a mismatch, prose,
  an open-ended answer, a matching exercise, a keyless page) goes to one evaluator agent
  that reasons about the answer whatever shape it takes. There is deliberately **no
  enumeration of answer types** anywhere in this codebase — see the module table's
  `k12ta.grading` row.

The policy filter sits between diagnosis and response on purpose. Diagnosis always runs
in full; the restriction is on what reaches the student. That means the parent digest can
contain the worked reasoning even when the child's view cannot. (The "parent digest" this
sentence refers to is the V2 Weekly Learning Brief above, not anything V1 builds.)

**This paragraph describes the intended shape once `diagnose` exists in V2, not today's
system.** `k12ta.respond`'s filter runs in V1 with no diagnosis stage upstream of it at
all — it filters a grading verdict, not a worked explanation. Nothing is missing as a
result; there is simply no worked reasoning yet for the filter to withhold, and per the
resolution above there will not be one inside V1.

## Module boundaries

| Package | Owns | Must not |
|---|---|---|
| `k12ta.domain` | Entities and the feedback policy rail | Call a model or touch I/O |
| `k12ta.content` | Where work comes from and its attached rules | Know about grading internals |
| `k12ta.transcribe` | Image to structured problems, provider adapters | Decide correctness |
| `k12ta.grading` | Correctness verdicts and confidence gating: the deterministic key match, and the evaluator agent for everything it can't settle | Generate student-facing text; **branch on a kind of answer** (see below) |
| `k12ta.mastery` | Memory traces, decay, retrieval scheduling — **V2**, see the pipeline note above | Know about images or prompts |
| `k12ta.llm` | The only place a model provider is called | Contain any product logic |
| `k12ta.diagnose` | Turning an established error into a Diagnosis — **V2**, resolved 2026-08-30, see the pipeline note above. V1's "why was this wrong" is a parent's typed comment, not a generated diagnosis | Decide correctness |
| `k12ta.respond` | Applying the policy filter and rendering student-facing text | Call a model directly for a verdict |
| `k12ta.digest` | Weekly parent rollups — **V2**, see the pipeline note above | Reuse student-facing renderers |
| `k12ta.web` | HTTP and templates | Contain business logic |
| `k12ta.store` | SQLite schema, migrations, and typed repository functions | Contain business logic or render anything |
| `k12ta.ingest` | Turning an uploaded photo into a validated `page_captures` row; resolving the day's default assignment | Render HTML, decide grading correctness, call a model |
| `k12ta.pipeline` | Orchestrating one capture through ingest → transcribe → grade → persist, including the daily quota gate | Render HTML, call a model directly (it goes through `k12ta.transcribe`) |
| `k12ta.keys` | Parent-only answer-key ingestion: upload, transcribe, present for confirmation, persist confirmed entries | Be reachable from the student capture flow, store an *answer key entry* nobody confirmed on screen, call a model directly |
| `k12ta.design` | The M9a shared design system (docs/ROADMAP.md): `tokens.css` -- colour, type scale, spacing, radius, and state tokens, plus the base component CSS genuinely identical between the two apps before this milestone (the lightbox, the working-spinner keyframes, `[hidden]`). Not a Python package -- no `__init__.py`, nothing importable -- purely a static asset directory each app mounts independently at `/static` (`fastapi.staticfiles.StaticFiles`, itself already part of the approved `fastapi` dependency, so this needed no new line below) | Contain a build step or a frontend toolchain (plain CSS only, per `docs/ARCHITECTURE.md`'s existing commitment); contain anything importable from Python |

Three of these packages did not exist when this table was first written:
`k12ta.diagnose`, `k12ta.respond`, and `k12ta.digest`. They were listed because the
pipeline has eight stages and the table originally named owners for six, which left
diagnosis, response rendering, and the digest with no legal home under the stated
rules. Create each one when its milestone arrives, not before. **`k12ta.respond` has
since been built** (M3.2 — `render_student_result`, `summarize_results`, the
policy/oracle-suppression filter both apps render through); `k12ta.diagnose` and
`k12ta.digest` still do not exist. `k12ta.store` (M2.1) and `k12ta.web` (M2.2) have
also been built.
`k12ta.ingest` lands alongside `k12ta.web` in M2.2, not later, because resolving a
default assignment and grading image quality is business logic that `k12ta.web` is
explicitly barred from holding — it needed a home the moment `k12ta.web` existed.
`k12ta.pipeline` (M2.3) exists for the same reason: `k12ta.ingest`'s own contract says
it must not decide grading correctness, so the step that walks a capture through
transcription and grading needed a package that is allowed to. `k12ta.keys` (M2.4) is
a fully separate app from `k12ta.web` — own process, own port, matching `k12ta.label`'s
precedent — so "not reachable from the student flow" is structurally true, not a
convention resting on nobody adding a link.

`k12ta.keys`'s "no unconfirmed entry" rule is about **answer key entries** — the graded
truth a child's work is marked against — and is unaffected by the `"unconfirmed"`
provenance value on `page_identity_schemas` and `page_identities` introduced by Gap O
(`docs/USER_WORKFLOWS.md` §3). Those are a *page-identity* claim a child may propose,
deliberately safe only because `NO_SCHEMA` guarantees no key can be addressed yet, and
they are written by `k12ta.web`, never by `k12ta.keys`. Same word, two different
guarantees; do not read one as relaxing the other.

`k12ta.domain` and `k12ta.mastery` have zero third-party imports. That is deliberate: they
are the parts worth reading, and they should be testable in milliseconds.

## No answer-type enumeration, anywhere

**A standing architectural rule, added 2026-08-30.** This system must never grow a
`MatchingAnswer` / `FillInBlank` / `ProseAnswer` type hierarchy, an `answer_kind` enum, or
a grading branch per exercise format. Real material makes the reason concrete: one Tamil
worksheet on hand carries a six-pair matching exercise (the child's answer is *lines drawn
between two columns*) directly above a seven-blank fill-in exercise, and the same
household's other programs produce long division, one-word answers, and prose. Enumerating
those shapes means a code change per curriculum, forever, and the enumeration is always
one worksheet behind reality.

Instead: **one evaluator agent, given the page, the child's transcribed work, and the key
if one exists, reasoning about whether the answer is right.** Adding a new kind of
exercise must require zero code. The agent also decides how a multi-part question splits —
emitting sub-items (`5a`…`5g`) with a verdict each where the page has them, one verdict
where it doesn't — rather than a schema declaring it in advance.

## The evaluation ladder: three tiers, escalated by confidence, never by question type

Added 2026-08-30. Each tier runs only when the one above it declines. **Every branch here
is a confidence branch. None is a type branch** — nothing anywhere asks "is this a
matching question?", because that judgement is unreliable up front and is what rule 12
exists to prevent.

1. **Deterministic key match.** Exact match after Unicode NFC normalisation → correct. No
   model call, no confidence to gate. This keeps `docs/PROMPT_REVIEW.md`'s "never grade
   from the model's own arithmetic alone" true where the answer is unambiguous, and
   avoids paying a model call to confirm that `4` equals `4`.
2. **Text evaluator.** The agent gets the transcribed problem, the child's transcribed
   answer, and the key's text if one exists, and reasons about whether it's right. Cheap,
   fast, and sufficient for most work.
3. **Vision evaluator — the last resort, and the one that makes spatial answers
   possible.** The original **exercise page photograph**, plus the **answer key page
   photograph** where one is on file, sent to the model with the child's transcription as
   context. This exists because some answers are not text and never were: a line drawn
   between two columns, a circled option, an underline, a crossing-out, an arrow, a
   sketched shape. Transcribing those to text destroys the fact being graded. Reading the
   pixels is not a workaround, it is the only faithful representation.

**What triggers tier 3** — all confidence signals, none of them a question category:
- Tier 2 returns low confidence, or explicitly reports it cannot judge from text alone.
- Transcription itself was low-confidence or failed. **This turns a dead end into an
  attempt**: today an unparseable page is `NEEDS_HUMAN` and the child is asked to
  re-photograph. A page can now still be evaluated from its own pixels first.
- The child's answer transcribes to something structurally implausible for the question
  (an empty answer on a question the page clearly expects to be answered).

**Constraints on tier 3, each with a reason:**
- **It must still emit two separate confidences** — one for what it read, one for the
  verdict. Fusing vision and judgement into a single number would destroy the distinction
  the entire parent review queue is built on: "I misread her handwriting" and "she got it
  wrong" are different problems with different fixes, and a parent must be told which.
- **It must emit a textual description of the answer it saw**, per problem, which is
  stored as the transcription. The parent reviews text and a photo, the report card counts
  verdicts, and attempt history needs a record. Tier 3 is not a shortcut past
  transcription; it is transcription and evaluation fused into one call.
- **A key photo is not always available.** Key page images are only persisted from M3.8
  onward, and a manually-typed key has no photo at all. Tier 3 must work with the key as
  text, as an image, or absent entirely — never require the image.
- **Cost.** Two images per call. Tier 3 is genuinely expensive and is the reason the
  ladder exists at all rather than sending pixels every time.

**Open, and to be settled by measurement rather than argument:** whether tier 2 earns its
place. If tier 3 proves strictly better and the cost gap is small, collapse 2 into 3 and
keep only deterministic-then-vision. `docs/EVALS.md` family 4 reports accuracy per tier
specifically so this can be answered with a number.

**Unicode normalisation is part of that comparison, not an optimisation.** Tamil combines
vowel signs, so two visually identical answers can differ byte-for-byte; without NFC the
deterministic path would mark correct answers wrong in exactly the language V1 is
expanding into.

**Cost shape:** one evaluation call *per page*, batched across its problems — never one
per problem. `K12TA_DAILY_TOKEN_BUDGET_USD` was sized when a page cost one transcription
call and needs re-deriving.

## Why these dependencies

- **pydantic**: validation at the HTTP and model-output boundary only. Domain objects
  stay as plain dataclasses so they are cheap to reason about.
- **fastapi + jinja2**: server-rendered pages, no build step, no frontend toolchain. A
  tablet browser is the whole client. **This survives M9's premium UI/UX pass
  unchanged** — that milestone delivers a design system as tokens and one shared
  stylesheet, not a framework. No React, no Tailwind build, no bundler. "It would look
  better with a component library" is exactly the argument this line exists to refuse;
  the cost of a toolchain is paid by every future reviewer, and `docs/ROADMAP.md`'s M9
  states the constraint again where someone doing UI work will actually read it.
- **sqlite via stdlib**: single household, single machine. No ORM. If schema pain
  arrives, that is the moment to reconsider, not before.
- **httpx**: model calls.
- **Pillow** (M2.2): decode a captured JPEG far enough to read its dimensions and mean
  brightness for the capture-quality reject gate (too small, too dark). No other image
  processing happens in this codebase; this is not a general vision or CV dependency,
  and true skew/perspective detection is out of scope for it — that gate is an aspect
  ratio heuristic, not real skew detection.
- **pillow-heif**: stock Pillow cannot decode HEIC, the default photo format on every
  iPhone and iPad camera since iOS 11 -- not a screenshot concern, a live crash on the
  household's actual primary devices (the Pixel one child used shoots JPEG, which is
  the only reason this went unnoticed). Registers a Pillow-compatible opener
  (`pillow_heif.register_heif_opener()`, called once at import time in
  `k12ta.ingest.capture`) so `k12ta.ingest.capture.normalize_orientation`'s existing
  `Image.open` call handles a `.heic` upload the same way it already handles JPEG and
  PNG, with no branch anywhere else in the capture path. Read-only in this codebase;
  every capture is still re-encoded to JPEG on disk exactly as before.
- **python-multipart** (M2.2): FastAPI has no way to parse a `multipart/form-data`
  request without it, and a `<input type=file>` capture upload has to be sent that
  way — there's no working around it. Not a product dependency in its own right, it's
  what makes the already-approved `fastapi` dependency's file-upload feature work.
- **playwright + pytest-playwright** (dev-only, `tests/browser/`): the rest of the
  suite tests server-rendered contracts — status codes, presence of markup — but
  `TestClient` never executes JavaScript. Four real bugs (the empty student picker,
  the capture flow's silent wait, the needs-human cause wording, the key-upload dead
  end) shipped past a fully green suite and were found only on a real device, because
  nothing exercised the client-side JS between a click and the rendered result.
  Playwright drives a real headless Chromium against the real ASGI app to close that
  gap. Not a runtime dependency — `k12ta.web` and `k12ta.keys` never import it; it
  lives only in `tests/browser/` and the `dev` extra, and is excluded from the default
  `pytest -q` run (see `tests/browser/conftest.py`) since it needs a Chromium binary
  (`playwright install chromium`) most edits don't touch and shouldn't have to pay for.

No LangChain, no agent framework, no vector database. The agentic behaviour here is a
small number of explicit steps with typed handoffs, which is more legible to a reviewer
than a graph library and easier to eval.

## Prompts are artefacts

Prompts live in `prompts/*.md`, loaded by id, versioned in git, and covered by evals.
No prompt string is written inline in Python. When a prompt changes, the eval that
covers it must be rerun and the number recorded in the commit message.

## Confidence and escalation

Every stage emits a confidence. The pipeline enforces one rule: any stage below its
floor short-circuits the item to `NEEDS_HUMAN` and it is presented to the child as
"I could not read this one clearly", never as an error. `NEEDS_HUMAN` has a second,
distinct cause as of M2.3 — no answer key exists for the item at all, independent of
how confidently it was transcribed — and that case gets its own honest message ("I
don't have an answer key for this one yet") rather than being folded into the
low-confidence copy above. Both render in the same neutral visual treatment; only the
wording differs.

## Asking when exactly one component is missing

Refusing a `PARTIAL` page identity outright, every time, trades a real cost (a page a
parent already scanned sits ungraded) for a real safety property (never grading against
the wrong page's answers). `k12ta.grading.page_identity.resolve_partial` narrows that
trade in one specific, bounded case: when a photo reads every schema component except
one, and this source's already-confirmed `page_identities` mappings can settle which
value that missing component must have.

**Configuration over inference, always.** The candidates offered are never guessed from
the photo and never free text — they are exactly the confirmed mappings a parent has
already verified against the physical book, filtered to the ones agreeing with every
component the photo did read. A wrong pick is possible only in the sense that it exists
among the real candidates and someone chose the wrong one; it can never invent a page
that was never confirmed.

**The coverage limit.** A single agreeing match auto-resolves only when no other value
has ever been confirmed for the missing component anywhere in this source. Early in a
term, before a second section has been key-scanned at all, a single Day 6 match under
Section 1 is not proof a Section 2 doesn't exist — it only means nothing from Section 2
has been taught to the system yet. Auto-resolving on partial coverage would grade a
Section 2 page against Section 1's answers the first time a student photographed one,
exactly the confident-wrong-grade failure this whole system exists to refuse. Two or
more matches, or one match without full coverage, are offered to the student as a
constrained pick instead — real options only, plus an explicitly subordinate "not sure"
that falls through to the ordinary honest refusal.

**The residual risk, stated plainly rather than left implicit.** A student's pick can be
validated for candidacy — the server re-derives fresh candidates from the current
`page_identities` table at submission time and refuses anything that isn't one of them,
which catches a stale or tampered request. It cannot be validated for factual
correctness. If a child picks the wrong option among genuinely real candidates — because
she doesn't actually know which section she's in, not because anything was tampered
with — the system has no way to detect that and will grade her work against the wrong
page's key, confidently and wrongly. This is a deliberate, bounded exception to the
fail-closed rule elsewhere in this codebase, accepted because the alternative (refusing
every `PARTIAL` outright, forever) has its own real cost, and because the exposure is
narrow: only the single-missing-component case, only among pages a parent has already
verified by hand, never a guess invented for the occasion. Anyone extending this
mechanism should keep that scope narrow rather than widen it to cover more ambiguity
than this trade was actually made for.

Identity resolved is not the same as a key existing for the resolved page — the regrade
that follows a pick or a newly-added key (`k12ta.pipeline.process.
regrade_capture_for_resolved_identity`) can still land on `NEEDS_HUMAN` (typically
`NO_KEY_FOR_PAGE`) rather than a definite grade, exactly as honest as at capture time.
And a pick is never counted as the model succeeding: it is logged as its own outcome,
`page_identity.RESOLVED_BY_STUDENT_PICK`, a distinct row alongside the original honest
`PARTIAL` log entry rather than an upgrade of it, specifically so a per-source accuracy
count can never conflate a student's pick with `resolve()`'s own composite lookup
succeeding.

## Multi-user

Every row carries `student_id`, with one deliberate category of exception:
tables that track operational state for the system itself, never scoped to a student
— `schema_migrations`, and `daily_request_counts` (M2.3, the persisted daily API-quota
counter). The resource being protected there (one shared API key's daily quota) is a
household-level resource, not a per-child one. Every table holding anything about a
student's work, identity, or progress still carries `student_id`, no exceptions. There
is no authentication in v1 and there should not be; the parent PIN gates exactly one
action, which is overriding feedback policy.

`student_id` scopes a child within one household; nothing scopes households from each
other, because there is only one. See `docs/ROADMAP.md`'s "Commercial readiness" note
for what that means once a second household is real.
