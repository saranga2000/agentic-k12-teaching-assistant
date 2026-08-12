"""Turning an uploaded photo into a validated `page_captures` row.

Owns the capture-quality reject gate and resolving a student's default assignment
for the day. Must not render HTML (that's `k12ta.web`) or decide grading correctness
or call a model.
"""
