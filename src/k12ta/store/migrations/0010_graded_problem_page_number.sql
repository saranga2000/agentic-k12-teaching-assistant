-- M3.2b: the resolved page number process_capture already computes at grading
-- time was never persisted, so nothing could recognise two captures as attempts
-- at the same underlying problem -- the gap behind the multi-attempt oracle (see
-- docs/ROADMAP.md's M3 gap note). NULL for rows that never resolved a page (most
-- NEEDS_HUMAN causes); always set for CORRECT and INCORRECT, because
-- k12ta.grading.answer_keys.get_entry cannot produce a verdict without one.
ALTER TABLE graded_problems ADD COLUMN page_number INTEGER;
