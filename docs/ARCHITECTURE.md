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

## Multi-user

Every row carries `student_id`, with one deliberate category of exception:
tables that track operational state for the system itself, never scoped to a student
— `schema_migrations`, and `daily_request_counts` (M2.3, the persisted daily API-quota
counter). The resource being protected there (one shared API key's daily quota) is a
household-level resource, not a per-child one. Every table holding anything about a
student's work, identity, or progress still carries `student_id`, no exceptions. There
is no authentication in v1 and there should not be; the parent PIN gates exactly one
action, which is overriding feedback policy.
