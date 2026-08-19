# Architecture

## Shape

```
capture (tablet browser)
  -> ingest: image on disk, PageCapture row
  -> transcribe: Transcriber adapter -> TranscribedItem[] with confidence
  -> grade:  key path (deterministic)  |  keyless path (M6, model + cross-check)
  -> diagnose: misconception per error, model call, prompt from prompts/
  -> respond: policy filter decides what may be said
  -> mastery: evidence folded into per-skill traces with decay
  -> schedule: next session composed from due skills
  -> digest: weekly rollup for the parent
```

The policy filter sits between diagnosis and response on purpose. Diagnosis always runs
in full; the restriction is on what reaches the student. That means the parent digest can
contain the worked reasoning even when the child's view cannot.

## Module boundaries

| Package | Owns | Must not |
|---|---|---|
| `k12ta.domain` | Entities and the feedback policy rail | Call a model or touch I/O |
| `k12ta.content` | Where work comes from and its attached rules | Know about grading internals |
| `k12ta.transcribe` | Image to structured problems, provider adapters | Decide correctness |
| `k12ta.grading` | Correctness verdicts and confidence gating | Generate student-facing text |
| `k12ta.mastery` | Memory traces, decay, retrieval scheduling | Know about images or prompts |
| `k12ta.llm` | The only place a model provider is called | Contain any product logic |
| `k12ta.diagnose` | Turning an established error into a Diagnosis | Decide correctness |
| `k12ta.respond` | Applying the policy filter and rendering student-facing text | Call a model directly for a verdict |
| `k12ta.digest` | Weekly parent rollups | Reuse student-facing renderers |
| `k12ta.web` | HTTP and templates | Contain business logic |
| `k12ta.store` | SQLite schema, migrations, and typed repository functions | Contain business logic or render anything |
| `k12ta.ingest` | Turning an uploaded photo into a validated `page_captures` row; resolving the day's default assignment | Render HTML, decide grading correctness, call a model |
| `k12ta.pipeline` | Orchestrating one capture through ingest → transcribe → grade → persist, including the daily quota gate | Render HTML, call a model directly (it goes through `k12ta.transcribe`) |
| `k12ta.keys` | Parent-only answer-key ingestion: upload, transcribe, present for confirmation, persist confirmed entries | Be reachable from the student capture flow, store an unconfirmed entry, call a model directly |

Three of these packages do not exist yet: `k12ta.diagnose`, `k12ta.respond`, and
`k12ta.digest`. They are listed because the pipeline has eight stages and the table
originally named owners for six, which left diagnosis, response rendering, and the
digest with no legal home under the stated rules. Create each one when its milestone
arrives, not before. `k12ta.store` (M2.1) and `k12ta.web` (M2.2) have since been built.
`k12ta.ingest` lands alongside `k12ta.web` in M2.2, not later, because resolving a
default assignment and grading image quality is business logic that `k12ta.web` is
explicitly barred from holding — it needed a home the moment `k12ta.web` existed.
`k12ta.pipeline` (M2.3) exists for the same reason: `k12ta.ingest`'s own contract says
it must not decide grading correctness, so the step that walks a capture through
transcription and grading needed a package that is allowed to. `k12ta.keys` (M2.4) is
a fully separate app from `k12ta.web` — own process, own port, matching `k12ta.label`'s
precedent — so "not reachable from the student flow" is structurally true, not a
convention resting on nobody adding a link.

`k12ta.domain` and `k12ta.mastery` have zero third-party imports. That is deliberate: they
are the parts worth reading, and they should be testable in milliseconds.

## Why these dependencies

- **pydantic**: validation at the HTTP and model-output boundary only. Domain objects
  stay as plain dataclasses so they are cheap to reason about.
- **fastapi + jinja2**: server-rendered pages, no build step, no frontend toolchain. A
  tablet browser is the whole client.
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
