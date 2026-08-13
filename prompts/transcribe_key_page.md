---
id: transcribe_key_page
version: 1
covered_by: tests/test_key_page_transcriber.py
---

You are reading a photograph of a page from a printed answer key.

Return JSON only. No prose, no markdown fence.

The page is organized into blocks, each headed by something like "Day 5/Page 17". One
photograph commonly contains several such blocks — read every one visible, not just
the first. Within a block, answers are usually compressed into running text, for
example: "1. 8 m; 2. 15 cm; 3. 28 m; 4. 32 in; 5. 1/6". Split that into one entry per
numbered answer.

For every answer on the page, emit one object with:
- `page_number`: the workbook page number from that block's "Page NN" heading, as an
  integer
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

Rules:
- If handwriting or print is ambiguous, lower the confidence. Do not guess at high
  confidence — an answer key entry that looks plausible but is wrong is worse than one
  flagged as uncertain, because it will be trusted to grade a child's work.
- Never invent a page number, problem number, or answer that is not on the page.
- Do not attempt to transcribe or summarize a graph or table's content. Mark it
  `graph_or_table` and stop there.

Output shape:

```
{"entries": [{"page_number": 17, "problem_number": "1", "answer_text": "8 m",
              "ungradeable_reason": null, "confidence": 0.95}]}
```
