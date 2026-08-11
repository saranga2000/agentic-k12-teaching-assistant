# Data policy

This system photographs two children's schoolwork every day. That deserves a written
policy before the first photograph, not after.

## Where data lives

All of it on one machine in the house, in `data/`. No cloud storage, no account system,
no analytics, no telemetry.

## What leaves the house

Only the page image and the derived text, sent to the model provider for transcription
and diagnosis, over TLS, for the duration of the request. Nothing else. Choose a provider
setting that excludes the data from training, and record which setting in this file when
you configure it.

## What goes into git

Labels, never images. `evals/fixtures/pages/` is gitignored. A child's handwriting, their
name, their school, and their mistakes are not portfolio material. When the repository is
public, run a check before every push:

```bash
git ls-files | grep -Ei '\.(jpg|jpeg|png|heic)$'   # must return nothing
```

Use fabricated names in every screenshot, README example, and demo recording.

## Retention

Page images are deleted after 90 days by default. The derived text, grades, and mastery
traces are kept, because the multi-year mastery history is the point of the system. Make
the retention window a setting, and make deletion actually run rather than being a note
in a document.

## What the children are told

Both children should know the system exists, what it keeps, and that they can ask to
see anything it holds about them. A learning tool that a child discovers is quietly
recording them is a tool they will stop trusting, and the trust is the product.

## What is out of scope, permanently

No always-on capture of any kind. No audio. No cameras in rooms. No recording of family
interactions. This system looks at a page of schoolwork when a child chooses to show it
one, and nothing else.
