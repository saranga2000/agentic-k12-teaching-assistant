# Review of the original spec

**Note added 2026-08-30:** this is a frozen review from M0 and is kept as the historical
record, not updated in place. Two recommendations below (Gap 4's weekly digest, Gap 5's
manual score entry) named "M5" as their home; `docs/ROADMAP.md`'s 2026-08-30 V1/V2
rescoping moved both out of M5 and into "V2. Learning intelligence" — a weekly digest
with skill language and a parent-typed outside score are both out of V1's narrowed scope
(an AI-assisted evaluator, not a general progress/record system). See `docs/ROADMAP.md`
directly for what M5 covers now.

The same rescoping reaches three more things below, so read them as V2 rather than as
near-term recommendations: **Gap 5**'s "first-attempt accuracy on resurfaced skills"
proxy (resurfacing is M4, now V2), **Gap 7**'s reshaped 1st-grader product ("the same
mastery model with no transcription in the loop" — the mastery model is V2, and
`docs/ROADMAP.md`'s M7 item 1 already carries this warning), and the "Smaller notes"
item on grade rollover writing an audit row, whose stated justification is keeping
*mastery history* interpretable across years. Rollover may still matter to V1 for a
different reason — an ended enrollment, a new book mid-year — but the argument given
below is a V2 argument, not that one.

The original spec is unusually good for a v1 document. It already contains three things
most product specs miss: a stated failure asymmetry (a confident wrong mark is worse
than an escalation), a memory model that decays, and an integrity guardrail treated as
a testable requirement rather than a preference. What follows is the gap list.

## What is strong and should not be renegotiated

1. **Provider abstraction from day one for transcription.** Correct. The cost of the
   abstraction is one file; the cost of not having it is a rewrite.
2. **Never grade from the model's own arithmetic alone.** This is the single most
   important line in the spec. Keep it.

   **Still true after the 2026-08-30 clarification made the model the source of answers
   for keyless programs — but it now has to be actively defended rather than assumed.**
   Two mechanisms carry it: an exact key match resolves deterministically with no model
   call at all, so unambiguous answers never depend on model judgement; and the keyless
   path is independent solve **plus an adversarial cross-check plus agreement gating**,
   never a single solve taken at face value. That cross-check is what makes this line
   survive contact with M6. Anyone tempted to simplify it into one call should read this
   item first.
3. **Bias to "I cannot read this, ask a grown-up."** Correct, and it needs a numeric
   confidence floor so it is enforceable rather than aspirational.
4. **Effort-and-consistency leaderboards, accuracy kept private.** The stated rationale
   (accuracy leaderboards drive avoidance of hard material) is right and is the kind of
   reasoning worth writing up in the repo README.
5. **Decay in the mastery model.** This is the technically interesting part and the best
   portfolio chapter. It is scaffolded and tested in `src/k12ta/mastery/`.

## Gap 1: the mode switch is modelled at the wrong level

The spec has one global mode with a seasonal default. Reality on a Tuesday evening is
three concurrent policies: school homework that a teacher will grade, outside-programme
homework that an outside teacher will grade, and self-directed review with an answer key
in the back of the book. A global toggle will be wrong for at least one of them, and the
failure mode is the coach handing over an answer to graded work.

**Fix applied here:** feedback policy is a property of the assignment, derived from the
content source, and it fails closed. See `src/k12ta/domain/policy.py` and
`tests/test_policy.py`. There is no global switch to forget to flip.

This also cleanly absorbs the outside programmes without special cases. Each becomes a
content source row carrying: has a key or not, externally graded or not, default mode,
typical session length.

## Gap 2: a third feedback mode is missing

The spec has two modes. Timed fluency drilling is a third and behaves differently: the
coach must not interrupt during the timer, and the score of interest is items per minute
at high accuracy, not concept diagnosis. Mixing it into either existing mode produces a
coach that talks during a speed drill. `FeedbackMode.FLUENCY` is added.

## Gap 3: the binding constraint is capture friction, not model quality

Nothing in the spec addresses how a photo actually gets taken at 8:40pm by a tired
child. If the flow is longer than roughly ten seconds and two taps, adoption goes to
zero and none of the ML matters. This deserves to be a first-class requirement with its
own measurement (captures per assigned session, not accuracy).

**Recommendation:** the capture surface is a single always-open page on the tablet with
one big button, no login, and assignment selection defaulted from a weekly schedule the
parent enters once.

## Gap 4: the busiest user is not in the spec

The stated motivation is that both parents are time-poor and cannot coach, evaluate, or
track. But every artefact in the spec is consumed by the student. There is no output
addressed to the adult.

**Recommendation:** a weekly digest is a milestone in its own right (M5), not a
nice-to-have. Six lines: minutes on task, skills that improved, skills that regressed,
the two things worth asking about at dinner, and anything the coach refused to grade.
This is the feature that makes the system pay for itself in a busy household, and it is
cheap to build once sessions are persisted.

## Gap 5: no definition of success

"Grades should improve significantly" is not measurable inside this system, which never
sees a report card. Pick proxies now and log them from M2 so there is a baseline:

- minutes on task per week, per student
- proportion of sessions initiated by the student without an adult prompt
- first-attempt accuracy on resurfaced skills, which is the real learning signal
- outside-programme quiz and test scores, entered manually by a parent in ten seconds
- number of NEEDS_HUMAN escalations per session, which should fall over time

Without the manual score entry there is no way to correlate the system with outcomes.
It is two fields and it should exist by M5.

## Gap 6: the accuracy risk sits on keyless grading, and it is scheduled too early

For work with no answer key, "independent solve then cross-check" means the system's
correctness is bounded by the model's own competence on grade-level problems, which for
a strong 7th grade outside-programme curriculum is not a solved problem. Attempting
this before the key-based path is solid will produce wrong marks and destroy the
student's trust, which is unrecoverable.

**Recommendation:** keyless grading moves to M6, behind a calibration harness that
reports precision on INCORRECT verdicts specifically. Ship key-based grading first and
route unkeyed work to "I flagged three problems for you to check" until precision is
measured, not assumed.

**Half-reversed 2026-08-30, and this is the most consequential correction in this file.**
The *sequencing* advice was right and was followed — key-based grading shipped first, and
M6 still ships behind exactly the calibration gate described here. The *framing* was
wrong: this gap treats keyless grading as a risky enhancement that could be deferred
indefinitely or dropped, and the roadmap's cut list duly listed it first. It is not. The
household's real programs (RSM, Kumon) have no keys at all, so keyless grading is the
core of what V1 is for, and V1 cannot ship without it. See `docs/ROADMAP.md`'s V1
definition and M6. The caution survives; "cut it if evenings disappear" does not.

## Gap 7: the 1st grader case is harder than the 7th grader case, not easier

Handwriting at six years old plus early reading work plus short attention makes photo
transcription the worst it will ever be, with the least tolerance for a wrong mark. The
spec correctly defers it, but should also change the shape: for the younger child the
first useful product is probably not grading at all. It is a five minute parent-run
routine, scheduling, and streak tracking that uses the same mastery model with no
transcription in the loop.

## Gap 8: cost and retention are unstated

Two additions: a per-day token budget with a hard stop (present in `config.py`), and a
written policy on what happens to photographs of the children's work. Both belong in the
repo before any image is captured. See `docs/DATA_POLICY.md`.

## Gap 9: the calendar has moved

The spec assumes a summer start with a long runway. That runway is now about three
weeks. The roadmap is reordered so the school-year path, which is the restrictive one,
ships before term starts, and the summer-mode niceties come after.

## Smaller notes

- "Standards aligned to state, not district" is right, but outside programmes follow
  their own sequence. Make `standards_frame` nullable rather than forcing a mapping.
- Grade rollover should be an explicit event that writes an audit row, not a config edit,
  otherwise the mastery history becomes uninterpretable across years.
- Praise that "references prior performance" needs the prior performance in the prompt
  context. That is a retrieval requirement, not a tone requirement, and it should be
  built as one.
