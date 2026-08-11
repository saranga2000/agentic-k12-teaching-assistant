---
id: diagnose_error
version: 1
covered_by: tests/test_diagnosis.py (M4)
---

A problem has already been established as incorrect by a separate grading step. You are
not deciding whether it is wrong. You are deciding why.

Input: the problem, the student's written work, their final answer, and the correct
answer.

Return JSON only:
- `misconception_id`: a stable snake_case identifier for the underlying error type, for
  example `sign_error_on_distribution`, `denominator_added_directly`,
  `place_value_misalignment`, `units_dropped`. Reuse identifiers across problems so they
  aggregate.
- `error_location`: where the reasoning first went wrong, in the student's own steps
- `explanation`: one or two sentences, addressed to a reader who is not the student.
  This may contain the full reasoning; a separate policy layer decides what the student
  sees.
- `skill_ids`: the skills implicated

Distinguish carefully between:
- a conceptual misunderstanding, which should update the mastery model
- an arithmetic slip in an otherwise correct method, which should not
- a transcription artefact, which should be flagged rather than diagnosed
