"""Orchestrating one capture through ingest -> transcribe -> grade -> persist.

Owns the daily quota gate: quota safety is a property of calling this package at all,
not something every caller has to remember to check first. Must not render HTML or
call a model directly (that goes through `k12ta.transcribe`, which goes through
`k12ta.llm`).
"""
