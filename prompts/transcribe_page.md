---
id: transcribe_page
version: 5
covered_by: evals/run_transcription_eval.py
---

You are reading a photograph of a page of completed schoolwork.

Return JSON only. No prose, no markdown fence.

For every problem visible on the page, emit one object with:
- `problem_id`: the number or label printed on the page, as printed
- `prompt_text`: the problem as printed, in plain readable text -- a student reading
  it back should not need to know any typesetting notation. Write a fraction as
  "3/4", an exponent as "x^2" or "x squared" (whichever the page's own style implies),
  a square root as "the square root of 16", never as LaTeX or any other markup
- `student_answer_raw`: exactly what the student wrote as their final answer, character
  for character, including errors. Do not correct it. Do not complete it.
- `confidence`: 0.0 to 1.0, your probability that `student_answer_raw`, as you
  transcribed it, is exactly what is on the page. This is a claim about your reading
  of a mark that is there. When `student_answer_raw` is empty, there is nothing to
  have read, so report 0.0 here -- put your confidence about the blank itself in
  `blank_confidence` below, not here. Never use this field to mean "I'm sure nothing
  is written."
- `blank_confidence`: 0.0 to 1.0, your probability that the problem is genuinely
  blank -- nothing written, not just faint or hard to see. Only meaningful when
  `student_answer_raw` is empty; report 0.0 when it is not.

Rules:
- If handwriting is ambiguous, lower `confidence`. Do not pick the more plausible
  reading. Guessing at high confidence is the worst thing you can do here.
- If a student crossed something out, transcribe only the final uncrossed answer.
- If a problem is visible but has no answer written, use an empty string for
  `student_answer_raw`, set `confidence` to 0.0, and set `blank_confidence` to your
  confidence that it is genuinely blank rather than just illegible in this photo.
- If work is shown but no final answer is circled or boxed, take the last line.
- Never add a problem that is not on the page.

Alongside `items`, also report `page_identity`: whatever markers identify which
workbook page this is, independent of the problems themselves.

{{SCHEMA_COMPONENTS}}
If the list above is non-empty, report a value for exactly those markers, using
exactly the names given, and nothing else -- ignore any other marker on the page,
even a legible one, if it is not in that list.

If the list above is empty, no marker names are known for this page yet: report
every identifier-like marker you can see -- a prominent heading, a printed code,
a small page number, anything that could help tell one page apart from another --
each under your own short, descriptive name for it (e.g. `"day"`, `"section"`,
`"worksheet_code"`).

Either way, each reported marker is a list of the distinct values you actually
see for it on this photo -- usually zero or one, but exactly two when a two-page
spread shows two different values for the same marker (e.g. "Day 2" on the left
page and "Day 3" on the right). Never invent a value or infer one from anything
not printed on the page. Reporting no markers at all is a normal, expected result
on a page with none printed, not an error.

Also report `confidence`: 0.0 to 1.0, your probability that the values you
reported for `page_identity` are exactly correct. This is independent of any
single problem's own `confidence` above -- a page's heading can be perfectly
legible even when an answer next to it is not, and the reverse also happens.

Output shape when specific markers are known to look for:

```
{"items": [{"problem_id": "...", "prompt_text": "...", "student_answer_raw": "...", "confidence": 0.0, "blank_confidence": 0.0}],
 "page_identity": {"section": ["Section 1"], "day": ["Day 5"], "confidence": 0.9}}
```

Output shape when no markers are known yet (report whatever you see, your own names):

```
{"items": [...],
 "page_identity": {"day": ["Day 5"], "worksheet_code": [], "confidence": 0.9}}
```
