"""Parent-only answer-key ingestion: scan a printed key, confirm every extracted
answer, and it becomes grading ground truth.

A fully separate app from `k12ta.web` -- own process, own port, matching
`k12ta.label`'s existing precedent -- so "not reachable from the student capture
flow" is structurally true, not a convention resting on nobody adding a link.
"""
