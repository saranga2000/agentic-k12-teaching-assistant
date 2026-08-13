# agentic-k12-teaching-assistant

[![ci](https://github.com/saranga2000/agentic-k12-teaching-assistant/actions/workflows/ci.yml/badge.svg)](https://github.com/saranga2000/agentic-k12-teaching-assistant/actions/workflows/ci.yml)

A tool that takes a photo of your kid's finished homework page and turns it into a
graded page, a plain explanation of why each mistake happened, a short follow-up quiz on
just the skill that slipped, and an updated picture of what they still need to practise.

There are two doors into this repository, depending on why you are here:

- **[Use it with your kids](#use-it-with-your-kids)** — you are a parent who wants a
  practical tool for getting homework checked without sitting over a shoulder every
  night.
- **[Learn agentic AI by building something real](#learn-agentic-ai-by-building-something-real)**
  — you want to see what a small, typed, eval-driven Agentic AI pipeline looks like when it is
  built for a real household instead of a demo.

## Status

| Milestone | What it proves | State |
|---|---|---|
| M0 Skeleton + domain model | Repo hygiene, tests-before-code, CI | done |
| M1 Fixture corpus + transcription eval | Measurement before capability | done |
| M2 Vertical slice (photo to graded page) | End to end value | not started |
| M3 Assignment policy engine + integrity evals | Safety rail with its own tests | not started |
| M4 Mastery model with decay and resurfacing | The headline chapter | scaffolded |
| M5 Parent weekly digest | Payoff for the busy adult | not started |
| M6 Keyless grading (independent solve + cross-check) | Hard accuracy work | not started |

See `docs/ROADMAP.md` for what each milestone includes and why they are ordered this way.

**Transcription eval, 2026-08-12**: detection recall 0.396 (19/48), answer exact match
0.842 (16/19), detection precision 1.000. Full report:
[`evals/results/2026-08-12-0825-vision_llm.md`](evals/results/2026-08-12-0825-vision_llm.md).

Calibration is still the number that matters most, not recall: answers reported at
0.85–0.95 confidence were right 3 out of 5 (0.600 accuracy), while answers at 0.95 and
above were right all 13 out of 13 (1.000 accuracy) — the confidence gate is trustworthy
only at the top band on this sample. Detection precision reads as a perfect 1.000 for
the same reason it did before: a two-page-spread fixture labels only one visible page,
so a detection on the unlabelled page is reported as unattributed rather than folded in
as a hallucination it never made. The number this run actually exposes as weak is
recall, not precision: only 19 of 48 expected problems were found at all, and
misnumbered items are 0 across every slice, so every miss is a clean failure to detect
the problem, not a numbering mixup. The corpus is eight scored pages (one hit a network
timeout, excluded rather than counted as a model failure), all two-page-spread, one
source, one capture device — the roadmap already predicted spread layout would score
worse than single-page, and there is no single-page data yet to say how much of this
gap is that specifically.

## Use it with your kids

There is nothing to install yet, but here is what it will look like. Your child
photographs a finished page on a tablet, and a few seconds later the same screen shows
what they got right, what they got wrong, and why — one page that appears after a tap,
not a conversation. The whole thing runs on one laptop in your house: no account to
create, no app to install beyond an ordinary browser tab, no cloud service holding your
child's work.

The setup guide for families arrives at M3, once the app can actually capture a page,
grade it, and be trusted not to hand over an answer on work someone else is going to
grade. Until then, `docs/ROADMAP.md` has the full plan and timeline.

## Learn agentic AI by building something real

A K-12 teaching assistant that runs in a real house, on a real school schedule, for two
real kids.

### Run it yourself

```bash
git clone https://github.com/saranga2000/agentic-k12-teaching-assistant
cd agentic-k12-teaching-assistant
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

That runs the 31 tests M0 shipped. Implemented and tested right now: the domain model,
the feedback policy engine, the mastery model, and the key-based grader. Not built yet:
transcription, persistence, and the web interface.

### What you will learn

- **Plain dataclasses for domain objects** — no framework, testable in milliseconds —
  `src/k12ta/domain/models.py`
- **Feedback policy as assignment data, not a global switch** — fails closed by
  default — `src/k12ta/domain/policy.py`
- **Deterministic grading kept apart from model calls** — the key-based grader never
  touches a model — `src/k12ta/grading/key_grader.py`
- **A two-parameter spaced-decay memory model** — stability grows on success, a floor
  stops it decaying to zero — `src/k12ta/mastery/model.py`
- **Session composition against burnout** — mixes due, weak, and solid items instead of
  "weakest skill first" — `src/k12ta/mastery/scheduler.py`
- **Confidence-gated escalation** — below a stated threshold the system says it could
  not read the page, never a guess — see "Confidence and escalation" in
  `docs/ARCHITECTURE.md`
- **A single model-provider adapter boundary** — swapping providers is a new file, not
  a refactor (M1) — `src/k12ta/llm/`
- **Eval-driven development** — a scoring harness written and run before the thing it
  measures exists (M1) — `evals/run_transcription_eval.py`
- **Adversarial evals as a CI gate** — scores leakage of graded answers, blocking merges
  (M3) — `docs/EVALS.md`

### Why there is no agent framework here

No LangChain, no agent framework, no vector database, and no reasoning loop deciding
what to do next. The pipeline is a fixed sequence of typed stages — capture, transcribe,
grade, diagnose, respond, update mastery, schedule — each a plain function with a typed
input and output. That is a deliberate trade: less flexible than a general agent loop,
but every stage is independently testable, independently eval-able, and legible to a
reviewer who has never seen the code before. See `docs/ARCHITECTURE.md` for the full
reasoning.

### Where to go next

- `docs/PROMPT_REVIEW.md` — the design critique that shaped every decision below,
  including why a single global feedback toggle was rejected.
- `docs/ARCHITECTURE.md` — the module boundaries, the dependency choices, and the case
  against an agent framework, in full.
- `docs/EVALS.md` — the three eval families that gate every milestone, and the number
  each one has to clear.
- `AGENTS.md` — the working agreement a coding assistant follows in this repo: tests
  first, one module one job, `mypy --strict`.
- `docs/DATA_POLICY.md` — what happens to a photograph of a child's homework, written
  down before the first one was ever taken.
- `PROMPTS.md` — the actual prompts used to build this, one milestone at a time, if you
  want to reproduce the process yourself.

`docs/MEMORY.md` and `docs/PROGRESS.md` arrive at M4 and M5, once there is a mastery
history and a weekly digest to write about.

## Roadmap and non-goals

Milestones are ordered around one constraint: each one has to be independently
shippable and independently demonstrable, and the school-year path (M0 through M3)
ships before the more permissive summer-mode work. Full reasoning and the milestone
detail live in `docs/ROADMAP.md`.

Deliberately out of scope for v1: no account system, no cloud multi-tenancy, no
peer or group mode, no speech, no mobile native app, no always-on capture of any kind.
