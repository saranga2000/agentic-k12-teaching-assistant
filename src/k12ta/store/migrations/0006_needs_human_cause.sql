-- Scope A: record why a problem was flagged needs-human, so the renderer never has
-- to re-derive the reason from a transcription-confidence float.
--
-- NULL is meaningful here: a row graded before this column existed genuinely does
-- not know why it was flagged, and the renderer says "needs a grown-up" with no
-- claimed reason for those rows rather than inventing one.
ALTER TABLE graded_problems ADD COLUMN needs_human_cause TEXT;
