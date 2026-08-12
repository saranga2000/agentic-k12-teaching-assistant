# Fixtures

Label files live here and are committed. Page images live in `pages/` and are gitignored.
See `docs/DATA_POLICY.md`.

One label file per page, named `<page_id>.json`:

```json
{
  "page_id": "2026-08-15-math-p12",
  "image": "pages/2026-08-15-math-p12.jpg",
  "source_id": "summer_bridge",
  "subject": "math",
  "capture_quality": "good",
  "capture_device": "ipad-air-m1",
  "capture_method": "camera-roll",
  "layout": "single-page",
  "items": [
    {
      "problem_id": "3",
      "prompt_text": "Solve for x: 3(x - 4) = 18",
      "student_answer_raw": "x = 2",
      "human_legible": true,
      "correct_answer": "x = 10"
    }
  ]
}
```

`capture_device` and `capture_method` are both required; the loader fails loudly if
either is missing rather than let a page silently fall into an "unknown" slice.

`capture_device` is the device the photo was taken on, e.g. `ipad-air-m1` or
`pixel-9a`. It is a free string, not a fixed list, since new devices will show up over
time — but the loader normalises it (lowercase, trimmed, whitespace collapsed to
hyphens) so "Pixel 9a", "pixel 9a", and "pixel-9a" always land in the same slice.

`capture_method` is how the image reached this folder: `camera-roll` for a photo taken
and then copied over, or `app-ui` once pages start coming through the capture page built
in M2. Unlike `capture_device`, this is a closed set of exactly those two values.

`layout` is required and is `single-page` or `two-page-spread`. It does not carry
forward between pages even when the other page-level fields do, because it is a
judgement about this specific photo, not something that stays constant across a run.

A `two-page-spread` photo shows two facing workbook pages at once, which means the
image also contains problems from the page you did *not* label. Without recording
that, the scoring harness would count those unlabelled problems as things the
transcriber hallucinated, when really they were simply never in scope. When
`layout` is `two-page-spread`, `spread_side` is also required — `left` or `right`,
naming which of the two visible pages the labelled items came from. When `layout` is
`single-page`, `spread_side` must be absent; the loader rejects a file that sets it
anyway rather than silently ignore an inconsistency.

Layout is never auto-detected from the image. That is image analysis inside a
disposable labelling tool: it can be wrong, and a silently mislabelled fixture is a
worse failure than one extra dropdown.

The eval harness can report scores sliced by capture_device, capture_method, layout, or
source_id. Single-page accuracy is expected to be materially better than two-page-spread
accuracy, since a spread halves the effective resolution of each page.

Aim for 40 to 60 pages before implementing any transcriber. Deliberately include: poor
light, angled shots, pencil that has smudged, crossed-out work, work that wraps around
the page, and at least five pages you personally find hard to read. Those five are the
ones that matter, because they are the ones where the confidence gate has to fire.
