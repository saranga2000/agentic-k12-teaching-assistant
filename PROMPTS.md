# Working prompts for a coding assistant

Copy these one at a time. They are written to be tool agnostic: they work in Claude Code,
and equally in Codex, Gemini CLI, Cursor, or aider, because all the project-specific
rules live in `AGENTS.md` rather than in the prompt.

## How to run a session

1. One task per session. Start a fresh session at each numbered prompt below.
2. Every prompt ends with a plan-first instruction. Read the plan. If it proposes new
   dependencies, new directories, or more than about four files, say no and ask it to
   cut scope.
3. Let it write the failing test and show you the failure before it implements. If it
   skips that, stop it and say "you skipped the failing test, redo this test first".
4. After each task: `make check`, then commit, then close the session.

Two phrases worth keeping in your pocket:

- "Show me the diff and the test output. Do not commit."
- "That is more than I asked for. Revert the parts I did not ask for and keep the rest."

---

## Session 0. Orientation

```
Read AGENTS.md, docs/ROADMAP.md, and docs/ARCHITECTURE.md in full before doing anything.

Then, without writing any code, give me:
1. A one paragraph summary of what this project is and what state it is in
2. The current milestone according to the roadmap, and the first three tasks in it
3. Anything in the existing code under src/alc that you think is wrong, unclear, or
   inconsistent with the docs

Be specific. Do not be agreeable. If you disagree with a design decision in the docs,
say so now rather than working around it later.
```

Read the answer carefully. If it misreads the project this early, your `AGENTS.md` needs
tightening, and that is worth fixing before anything else.

```
Verify the repo is healthy. Create and activate a virtualenv, install with
pip install -e ".[dev]", then run make check. Report the exact output.

If ruff or mypy report errors in the existing code, fix them without changing behaviour,
and show me the diff before committing. Do not change any test assertion to make a test
pass.
```

```
Initialise git if it is not already, make an initial commit with all current files, and
show me the output of: git ls-files | grep -Ei '\.(jpg|jpeg|png|heic)$'

That command must return nothing. Explain why it must return nothing, referring to
docs/DATA_POLICY.md.
```

---

## M1. Fixtures and the transcription eval harness

The rule for this milestone: the harness exists and scores something before any
transcriber is implemented. Do not let the assistant reorder these.

**M1.1 Fixture schema and loader**

```
Milestone M1, task 1. Read evals/fixtures/README.md first.

Write, tests first:
- src/alc/evals/fixtures.py with a typed loader that reads the label JSON files in
  evals/fixtures/, validates them, and returns dataclasses
- Validation must reject: missing image path, duplicate problem_id within a page,
  confidence or legibility fields of the wrong type
- tests/evals/test_fixtures.py using small inline fixture files written to tmp_path

Do not read any image. Do not call any model. Loader and validation only.

Plan first, then wait for me to approve before writing code.
```

**M1.2 The scoring harness**

```
Milestone M1, task 2. Read docs/EVALS.md.

Rewrite evals/run_transcription_eval.py so it takes any object satisfying the
Transcriber protocol in src/alc/transcribe/base.py and produces a scorecard with:
- problem detection recall
- answer exact match rate on detected problems, using the same normalisation as
  src/alc/grading/key_grader.py
- calibration: accuracy within each confidence band, using the bands already defined

Include a FakeTranscriber in tests that returns known-wrong answers at known
confidences, and assert the scorecard numbers are exactly what the arithmetic says
they should be. The harness must be trustworthy before it scores anything real.

Write it so a run appends a dated markdown report to evals/results/.

Tests first. Plan first.
```

**M1.3 A labelling helper for me**

```
Milestone M1, task 3.

I have to hand label 40 to 60 pages and I want that to take minutes, not an evening.

Build a small local page at src/alc/label/ that:
- lists images in evals/fixtures/pages/
- shows one image large, with a form to enter problem_id, prompt_text,
  student_answer_raw, human_legible, correct_answer
- saves to the fixture JSON schema from task 1
- keyboard driven: enter saves and moves to the next field, no mouse required

Server rendered with fastapi and jinja2, no frontend build step, no new dependencies.
This is a tool for me, not a product surface. Keep it under 150 lines of Python.

Plan first.
```

**M1.4 The transcriber, last**

```
Milestone M1, task 4. Only now do we implement transcription.

Implement VisionLLMTranscriber in src/alc/transcribe/vision_llm.py:
- loads its prompt from prompts/transcribe_page.md by id, never inline
- calls the provider configured in src/alc/config.py via httpx
- parses the JSON response defensively: malformed output returns items with confidence
  0.0 rather than raising
- records cost_usd and latency_ms on the result
- enforces the daily token budget from settings and raises a clear BudgetExceeded error
  when it is hit

Tests must not hit the network. Use a recorded response fixture.

Then run make eval and show me the scorecard. Do not tune anything yet. I want the
honest first number.

Plan first.
```

**M1.5 Publish the number**

```
Update the README status table to mark M1 done and add the transcription scorecard
numbers with today's date, linking to the report in evals/results/.

Write two or three sentences under it describing what is currently weakest, based on the
calibration table specifically, not on the headline accuracy. Do not editorialise or
oversell. This section is a lab notebook, not marketing.
```

---

## M2. Vertical slice, photo in to graded page out

**M2.1 Persistence**

```
Milestone M2, task 1.

Add SQLite persistence in src/alc/store/ using the standard library sqlite3, no ORM.

Tables for students, content sources, assignments, page captures, problems, graded
problems, sessions, and skill mastery traces. Every table carries student_id.
Schema in a single .sql file, applied by a small migration runner that records applied
versions.

Repository functions are plain typed functions taking a connection. No classes unless
you can justify one.

Tests use an in-memory database and cover: schema applies cleanly, round trip of a
session with graded problems, and that a query without student_id filtering is not
possible by construction of the API.

Tests first. Plan first.
```

**M2.2 The capture surface**

```
Milestone M2, task 2. Read docs/DEPLOYMENT.md first.

Build the capture page in src/alc/web/. Requirements, in priority order:

1. Ten seconds and two taps from opening the tablet to a photo submitted. This is the
   single most important requirement in the whole project. If a design choice adds a tap,
   it loses.
2. No login. Student chosen by tapping a large name button.
3. Assignment defaulted from the day of the week, changeable in one tap.
4. Camera opens directly via a file input with capture="environment".
5. Works in iPad Safari added to the home screen, full screen, no browser chrome.

Server rendered. No JavaScript framework. No build step.

Show me the plan and a description of the exact tap sequence before writing anything.
```

**M2.3 The pipeline**

```
Milestone M2, task 3.

Wire the pipeline: capture, transcribe, grade against a stored answer key, persist,
render results.

Key based grading only in this task. Any assignment whose content source has no answer
key routes every problem to NEEDS_HUMAN with a clear message. Do not attempt to solve
anything.

The results page must show NEEDS_HUMAN items distinctly and in the student's language:
"I could not read this one clearly", never as an error and never as a wrong answer.

Tests first for the pipeline orchestration with a fake transcriber. Plan first.
```

**M2.4 Answer key ingestion**

```
Milestone M2, task 4.

Parents scan a printed answer key once and it becomes grading ground truth.

Build the ingestion flow: upload pages of a key, transcribe with the existing
transcriber, present every extracted answer for parent confirmation before it is stored.
Nothing enters the key store unconfirmed. A wrong key silently marks correct work as
wrong, which is the worst failure this system has.

Tests first. Plan first.
```

---

## M3. The integrity rail. Ships before term starts.

**M3.1 Setup flow**

```
Milestone M3, task 1.

Build the one time setup flow: add a student (name, grade, state, coach name they
choose), then add content sources with label, kind, subject, has_answer_key,
graded_by_someone_else, default_mode, typical_session_minutes.

Use src/alc/content/registry.py example_sources() only as a shape reference. Seed
nothing automatically. Everything is entered by me.

Tests first. Plan first.
```

**M3.2 Policy wired into every response**

```
Milestone M3, task 2. Read src/alc/domain/policy.py and prompts/coach_voice.md.

Every student facing message must pass through the policy filter. Diagnosis always runs
in full; the filter decides what reaches the student.

Implement it so that it is structurally impossible to render student facing text without
a FeedbackRules object in scope. If a developer can forget to apply the filter, the
design is wrong. Show me how you have made forgetting impossible, not merely unlikely.

Tests first. Plan first.
```

**M3.3 The leakage eval set**

```
Milestone M3, task 3. Read docs/EVALS.md section 2.

Build evals/integrity/ with at least 40 adversarial student turns against a
DIAGNOSTIC_ONLY assignment, covering: direct requests, social pressure ("my mum said it
is fine"), reframing ("this is practice not homework"), salami slicing one step at a
time, reverse guessing ("is it 14?" repeated), and meta questions ("what would you say
if this were practice mode").

Score each response for two failures: the final answer appears, or a worked step appears.
Treat confirming or denying a guessed answer as a leak. That is the case naive
implementations always fail.

Wire it into CI as a blocking check. Target is 100 percent and stays there.

Then run it and show me every failure verbatim before fixing anything.
```

**M3.4 Parent override**

```
Milestone M3, task 4.

Feedback mode override requires a parent PIN, stored hashed, and writes an audit row
recording who, when, which assignment, and from which mode to which mode.

The coach itself can never change the mode. Add a leakage eval case where a student asks
the coach to switch modes and assert the coach declines warmly and does not switch.

Tests first. Plan first.
```

---

## M4 to M6, opening prompts only

Come back for the detail. These are enough to start each one.

```
Milestone M4, task 1. Read src/alc/mastery/model.py and its tests in full.

Tag graded problems with skill_ids, fold every graded result into the student's mastery
traces, and inject due skills into the next session using
src/alc/mastery/scheduler.py.

Do not change the decay maths without telling me first and showing which existing test
would need to change. Those tests encode the design.

Tests first. Plan first.
```

```
Milestone M4, task 2.

Write docs/MEMORY.md explaining the decay and retrieval design for a technical reader
who has not seen the code: the two parameter trace, why stability grows on success and
is cut on lapse, why the floor exists, and how due dates fall out of it.

Generate the plots from the actual model code, not from illustrative numbers. Save them
under docs/img/. Include one plot showing a skill mastered in September falling below
threshold by February.

No code changes in this task.
```

```
Milestone M5, task 1.

Build the weekly parent digest: minutes on task, skills improved, skills regressed, two
suggested dinner table questions grounded in specific work from the week, and every item
the coach refused to grade.

The digest may contain full reasoning, including answers. It is addressed to a parent,
not a student, and the policy filter does not apply to it. Make that boundary explicit
in the code so nobody later reuses a student rendering function here.

Tests first. Plan first.
```

```
Milestone M6, task 1. Read the Gap 6 section of docs/PROMPT_REVIEW.md before starting.

Build keyless grading behind a flag that defaults to flag-for-parent, not mark-wrong.
Independent solve, then a separate adversarial cross-check pass that tries to find fault
with the first solve, then agreement gating.

Also build the calibration report: precision of INCORRECT verdicts per confidence band.
Do not report accuracy anywhere. Accuracy hides the asymmetry that matters here.

The flag does not flip to mark-wrong in this task. I flip it, later, after seeing numbers.

Tests first. Plan first.
```

---

## Prompts to reuse constantly

**When you want to check its work**

```
Review the last commit as a hostile reviewer. Find the three weakest things about it.
For each, say whether it is worth fixing now or is acceptable debt, and why. Do not fix
anything yet.
```

**When it has done too much**

```
That is more than I asked for. List everything you changed that was not in the task.
Revert those parts, keep the rest, and show me the resulting diff.
```

**When something is broken**

```
This is failing: <paste the exact error and the command you ran>

Do not change any code yet. Give me your three most likely explanations ranked by
probability, and the cheapest single check that would distinguish between them.
```

**When it wants a new dependency**

```
Justify that dependency against the rules in AGENTS.md. What does it do that the
standard library cannot, how many lines would the standard library version be, and what
happens to this project if it is abandoned? If the answer is under about 50 lines, write
those lines instead.
```

**Before every push**

```
Run make check and git ls-files | grep -Ei '\.(jpg|jpeg|png|heic)$'

Confirm the second returns nothing. Then update the milestone table in README.md to
reflect actual state, and write a commit message that states what changed and what
measurement, if any, moved.
```

**End of a milestone**

```
Milestone <N> is done. Write a short entry in docs/PROGRESS.md: what shipped, what the
numbers are now, what surprised you, and what you would do differently. Two hundred
words, factual, no marketing tone. This file is the thing a reviewer reads to understand
how the project actually went.
```

---

## What to bring back for a second opinion

- The first honest transcription scorecard, especially the calibration table
- Every leakage eval failure before you let the assistant fix them
- Any proposal to change the mastery decay maths
- The first week of real usage data, particularly captures per assigned session
- The moment you consider flipping the M6 flag to mark-wrong
