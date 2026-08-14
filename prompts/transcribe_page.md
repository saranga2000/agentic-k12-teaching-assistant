---
id: transcribe_page
version: 2
covered_by: evals/run_transcription_eval.py
---

You are reading a photograph of a page of completed schoolwork.

Return JSON only. No prose, no markdown fence.

For every problem visible on the page, emit one object with:
- `problem_id`: the number or label printed on the page, as printed
- `prompt_text`: the problem as printed, in plain text, LaTeX for mathematical notation
- `student_answer_raw`: exactly what the student wrote as their final answer, character
  for character, including errors. Do not correct it. Do not complete it.
- `confidence`: 0.0 to 1.0, your probability that `student_answer_raw` is exactly what
  is on the page

Rules:
- If handwriting is ambiguous, lower the confidence. Do not pick the more plausible
  reading. Guessing at high confidence is the worst thing you can do here.
- If a student crossed something out, transcribe only the final uncrossed answer.
- If a problem is visible but has no answer written, use an empty string and set
  confidence to your confidence that it is genuinely blank.
- If work is shown but no final answer is circled or boxed, take the last line.
- Never add a problem that is not on the page.

Alongside `items`, also report `page_identity`: whatever markers identify which
workbook page this is, independent of the problems themselves. Report every kind
you can see evidence for, not just one -- a photo can show more than one:
- `day_or_unit_banner`: a prominent heading like "Day 11" or "Unit 3", as printed
- `printed_worksheet_code`: a worksheet code or label printed on the page (e.g. a
  Kumon-style code), as printed
- `printed_page_number`: a small printed page number, as printed, distinct from
  the prominent banner above -- workbooks often print both
- `unique_problem_ids`: globally unique problem numbers printed on the page (e.g.
  a chapter-scoped numbering scheme), as printed

Each of these four fields is a list of the distinct values you actually see for
that kind on this photo -- usually zero or one, but exactly two when a two-page
spread shows two different values for the same kind (e.g. "Day 2" on the left
page and "Day 3" on the right). Never invent a value or infer one from anything
not printed on the page. An empty list means you saw no evidence of that kind at
all -- that is a normal, expected result, not an error.

Also report `confidence`: 0.0 to 1.0, your probability that the values you
reported for `page_identity` are exactly correct. This is independent of any
single problem's own `confidence` above -- a page's heading can be perfectly
legible even when an answer next to it is not, and the reverse also happens.

Output shape:

```
{"items": [{"problem_id": "...", "prompt_text": "...", "student_answer_raw": "...", "confidence": 0.0}],
 "page_identity": {"day_or_unit_banner": ["Day 1"], "printed_worksheet_code": [],
                    "printed_page_number": ["13"], "unique_problem_ids": [], "confidence": 0.9}}
```
