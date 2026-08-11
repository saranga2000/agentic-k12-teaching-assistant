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

Aim for 40 to 60 pages before implementing any transcriber. Deliberately include: poor
light, angled shots, pencil that has smudged, crossed-out work, work that wraps around
the page, and at least five pages you personally find hard to read. Those five are the
ones that matter, because they are the ones where the confidence gate has to fire.
