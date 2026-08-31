# Data policy

This system photographs two children's schoolwork every day. That deserves a written
policy before the first photograph, not after.

## Where data lives

All of it on one machine in the house, in `data/`. No cloud storage, no account system,
no analytics, no telemetry.

## What leaves the house

Only the page image and the derived text, sent to the model provider for transcription
and diagnosis, over TLS, for the duration of the request. Nothing else.

**Provider: Gemini API, paid quota (Paid Tier 1), as of 2026-08-30.** Before that date
this system ran on the free tier. The two are governed by materially different terms,
and the difference is the whole substance of this section, so both are quoted verbatim
from the [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms)
rather than paraphrased. Verified against the live terms page on 2026-08-30, not
recalled — this file has been wrong about Gemini's terms by paraphrase before (see
`docs/PROGRESS.md`'s M1 entry, where a fetched *summary* of Gemini's docs fabricated
detail twice).

**Paid quota — what applies now:**

> "When you use Paid Services, including, for example, the paid quota of the Gemini API,
> Google doesn't use your prompts (including associated system instructions, cached
> content, and files such as images, videos, or documents) or responses to improve our
> products."

> "Google logs prompts and responses for a limited period of time, solely for detecting
> and preventing violations of the Prohibited Use Policy to maintain the safety and
> security of the Services, and any required legal or regulatory disclosures."

**Unpaid quota — what applied to every image sent before 2026-08-30:**

> "When you use Unpaid Services, including, for example, Google AI Studio and the unpaid
> quota on Gemini API, Google uses the content you submit to the Services and any
> generated responses to provide, improve, and develop Google products and services."

> "To help with quality and improve our products, human reviewers may read, annotate, and
> process your API input and output."

**What this means in plain terms.** Photographs of the children's schoolwork sent from
2026-08-30 onward are **not** used to train or improve Google's products, and are not
read by human reviewers for quality. That closes the exposure this policy was written
around. It is **not** zero-retention: prompts and responses are still logged for a
limited period for abuse detection and legal disclosure. That residual is accepted, and
stated here rather than rounded down to "nothing leaves."

**Work sent before 2026-08-30 is not retroactively covered.** Every page photographed
during M1 and M2 went through the unpaid quota under the terms quoted above. That cannot
be undone and is not being quietly dropped from the record.

**One thing to re-check if the API key ever changes.** The paid-vs-unpaid distinction
follows the *quota the request is billed against*, not the existence of a billing account
on some other project. An API key belonging to a project without billing enabled runs on
the unpaid quota — and the terms above flip — even while a billing account exists
elsewhere. Whenever the key in `K12TA_*` config is rotated or a new project is used,
confirm the request is actually landing on paid quota before sending a child's page
through it.

## Historical: the free tier stayed, deliberately, until 2026-08-30

**Superseded by the section above.** Kept because the reasoning is the reason the trigger
existed, and because the exposure it describes really did apply to the M1/M2 corpus.

M2 shipped on the free tier. This was an accepted trade, not an oversight.

- The earlier text in a previous version of this file — "Moving to a paid tier ... or a
  zero-retention provider is a prerequisite before M2 ... Do not ship M2 on the free
  tier" — was written as a prerequisite and was not met at the time. M2's pipeline ran
  against the same free tier the eval harness ran on.
- That meant Google could retain the submitted content — photographs of my children's
  schoolwork — and use it to improve its products, with human reviewers able to read and
  annotate it. That was the real cost, and it was accepted on purpose.

**Why the trade was worth taking then.** The system was unproven and barely used. Paying
for a tier before knowing whether the tool earned daily use is premature spending on top
of a product whose usefulness was still a hypothesis.

**The trigger, and how it actually resolved.** The rule was: move to a no-retention paid
tier or a zero-retention provider when either both children use the system daily, or the
other parent starts using the parent surface. Neither is what caused the move — the
account moved to paid quota on 2026-08-30 to clear a throughput limit that was blocking
an eval run. The obligation is nonetheless **discharged**, because the terms quoted above
are exactly the outcome it demanded; it was satisfied by accident rather than by
observance, which is worth recording honestly rather than claiming foresight.

**The second trigger condition is now unobservable, and is replaced.** Both parents share
one identity in this app by design, so "the other parent starts using the parent surface"
can never be detected. It is retired. In its place, one condition that has not been met
and must not be met silently:

> **If any child outside this household ever uses this system, the data posture is
> re-opened before their first photograph, not after.** Accepting a retention trade on
> your own children's work is a decision a parent is entitled to make. Making it on
> another family's child is not. This fires ahead of any pilot, trial, or favour for a
> school — see `docs/ROADMAP.md`'s "Commercial readiness."

**Third-party material.** Program worksheets photographed through this system are often
someone else's copyrighted material (a tutoring centre's printed workbook, a language
school's exercise sheets). For one household evaluating work its own children were
assigned, this is ordinary personal use. It stops being obviously that if the system is
ever offered to a school or sold, and the pages being uploaded are the school's own
published material. Not a blocker for household use; a question to answer before the
first external user, alongside the trigger above.

That trigger is recorded as a tracked task in docs/ROADMAP.md so it is honoured rather
than remembered.

Source: [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms_preview).

## What goes into git

Labels, never images. `evals/fixtures/pages/` is gitignored. A child's handwriting, their
name, their school, and their mistakes are not portfolio material. When the repository is
public, run a check before every push:

```bash
git ls-files | grep -Ei '\.(jpg|jpeg|png|heic)$'   # must return nothing
```

Use fabricated names in every screenshot, README example, and demo recording.

## Retention

Page images are deleted after 90 days by default. The derived text and grades are kept,
because that evaluation record — what was submitted, what was correct, what's still
unresolved — is the point of V1 (see `docs/ROADMAP.md`'s "V1. Evaluate, parent as final
authority", rescoped 2026-08-30). Mastery traces, once V2 exists, are kept for the same
reason and the same multi-year argument, but are a V2 concern layered on top of this
record, not what V1 itself is for. Make the retention window a setting, and make
deletion actually run rather than being a note in a document.

## What the children are told

Both children should know the system exists, what it keeps, and that they can ask to
see anything it holds about them. A learning tool that a child discovers is quietly
recording them is a tool they will stop trusting, and the trust is the product.

## What is out of scope, permanently

No always-on capture of any kind. No audio. No cameras in rooms. No recording of family
interactions. This system looks at a page of schoolwork when a child chooses to show it
one, and nothing else.
