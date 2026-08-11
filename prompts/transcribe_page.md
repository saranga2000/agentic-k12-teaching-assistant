---
id: transcribe_page
version: 1
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

Output shape:

```
{"items": [{"problem_id": "...", "prompt_text": "...", "student_answer_raw": "...", "confidence": 0.0}]}
```
