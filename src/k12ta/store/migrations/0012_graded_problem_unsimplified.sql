-- Fix 3 (units/fractions): "2/6" against a key of "1/3" is numerically correct
-- but not in lowest terms -- CORRECT, not INCORRECT, but not silently plain
-- CORRECT either when the workbook says "Simplify if possible". Decided once in
-- k12ta.grading.needs_human.decide, never re-derived at render time -- same rule
-- as every other needs_human/grading fact on this row. 0/1, NOT NULL DEFAULT 0:
-- every existing row is unambiguously "no note", not unknown.
ALTER TABLE graded_problems ADD COLUMN unsimplified INTEGER NOT NULL DEFAULT 0;
