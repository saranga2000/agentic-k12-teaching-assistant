---
id: transcribe_key_page
version: 4
covered_by: tests/test_key_page_transcriber.py
---

You are reading a photograph of a page from a printed answer key.

Return JSON only. No prose, no markdown fence.

The page is organized into blocks, each headed by something like "Day 5/Page 17". One
photograph commonly contains several such blocks — read every one visible, not just
the first. Within a block, answers are usually compressed into running text, for
example: "1. 8 m; 2. 15 cm; 3. 28 m; 4. 32 in; 5. 1/6". Split that into one entry per
numbered answer.

A photograph sometimes starts mid-block: the first few answers continue a block whose
heading was on a different, earlier photograph and is not visible here at all. When
you see numbered answers before any "Day N/Page NN" heading on this page:
- They belong to the day immediately before the first heading actually visible on this
  page (heading says "Day 11" and answers precede it → those answers are "Day 10").
- Find that day's page number by the page-number step between two consecutive
  headings elsewhere on this same page (this workbook increments the page number by a
  fixed amount per day, commonly 2 — "Day 11/Page 33" then "Day 12/Page 35" is a step
  of 2) and subtract that step from the first visible heading's page number ("Day
  10" = Page 33 − 2 = Page 31).
- This is inference from a pattern printed elsewhere on the same page, not the
  "invent a page number" this prompt tells you not to do elsewhere — do it whenever at
  least two headings on the page let you establish the step. If the page shows only
  one heading total, there is no step to infer from: leave those leading entries out
  of `entries` entirely rather than guessing one.

For every answer on the page, emit one object with:
- `page_number`: the workbook page number from that block's "Page NN" heading, as an
  integer
- `identifier_value`: the "Day N" part of that same block's heading, as printed (e.g.
  "Day 11") -- the prominent banner a student's own photo will show, which is a
  different, more legible thing than the small printed page number. For an inferred
  leading block (see above), use the inferred day ("Day 10" if you inferred page 31
  for it), not the page number.
- `problem_number`: the number or label printed before the answer, as printed (e.g.
  "1", "4a")
- `answer_text`: the answer exactly as printed, including units and fractions. `null`
  if this entry is ungradeable (see below) — never a description or paraphrase of an
  ungradeable answer.
- `ungradeable_reason`: `null` for a normal answer. Otherwise exactly one of:
  - `"answers_vary"` — the key itself declines to give one answer (e.g. "Answers will
    vary", "Students' writing will vary")
  - `"graph_or_table"` — the answer is a graph, drawing, or table, not text
- `confidence`: 0.0 to 1.0, your probability that `answer_text` (or the ungradeable
  classification) is exactly correct
- `identifier_confidence`: 0.0 to 1.0, your probability that `identifier_value` and
  `page_number` are exactly correct. This is a separate judgment from `confidence` --
  a block's heading can be smudged, cropped, or ambiguous even when the answer next
  to it is perfectly legible, and the reverse also happens. Score them independently.

Rules:
- If handwriting or print is ambiguous, lower the confidence. Do not guess at high
  confidence — an answer key entry that looks plausible but is wrong is worse than one
  flagged as uncertain, because it will be trusted to grade a child's work.
- Never invent a page number, problem number, or answer that is not on the page.
- Do not attempt to transcribe or summarize a graph or table's content. Mark it
  `graph_or_table` and stop there.

Output shape:

```
{"entries": [{"page_number": 17, "identifier_value": "Day 5", "problem_number": "1",
              "answer_text": "8 m", "ungradeable_reason": null, "confidence": 0.95,
              "identifier_confidence": 0.9}]}
```
