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

The eval harness can report scores sliced by either field.

Aim for 40 to 60 pages before implementing any transcriber. Deliberately include: poor
light, angled shots, pencil that has smudged, crossed-out work, work that wraps around
the page, and at least five pages you personally find hard to read. Those five are the
ones that matter, because they are the ones where the confidence gate has to fire.
