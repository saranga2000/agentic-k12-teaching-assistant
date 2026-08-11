# agentic-k12-teaching-assistant

An agentic tutoring system that turns a photo of completed schoolwork into a graded
page, a diagnosis of *why* each error happened, a short targeted follow-up quiz, and
an updated per-skill mastery model that drives the next session.

Built as a working household tool first and a portfolio artifact second. The two goals
are compatible only if every milestone is independently shippable.

## Status

| Milestone | What it proves | State |
|---|---|---|
| M0 Skeleton + domain model | Repo hygiene, tests-before-code, CI | in progress |
| M1 Fixture corpus + transcription eval | Measurement before capability | not started |
| M2 Vertical slice (photo to graded page) | End to end value | not started |
| M3 Assignment policy engine + integrity evals | Safety rail with its own tests | not started |
| M4 Mastery model with decay and resurfacing | The headline chapter | scaffolded |
| M5 Parent weekly digest | Payoff for the busy adult | not started |
| M6 Keyless grading (independent solve + cross-check) | Hard accuracy work | not started |

See `docs/ROADMAP.md`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # add your model API key
make test
```

## Reading order for a reviewer

1. `docs/PROMPT_REVIEW.md` - the design critique that shaped this build
2. `docs/ARCHITECTURE.md` - boundaries and why they are where they are
3. `src/k12ta/mastery/model.py` - memory with decay and retrieval scheduling
4. `src/k12ta/domain/policy.py` - the academic integrity rail
5. `evals/` - how any of this is known to work

## Non-goals for v1

No account system. No cloud multi-tenancy. No peer/group mode. No speech. No mobile
native app. No always-on capture of any kind.
