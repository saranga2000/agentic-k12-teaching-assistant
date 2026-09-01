-- docs/ROADMAP.md's M5 "correction audit trail": today k12ta.store.sessions.
-- apply_human_verdict and overturn_dispute_to_correct both do a direct,
-- silent UPDATE on graded_problems -- no record of what the row was before,
-- when it changed, or how. Append-only, mirroring policy_override_audit_
-- log's own shape and reasoning (0020_policy_overrides.sql): a record of
-- what happened, never rewritten, separate from current state.
CREATE TABLE verdict_correction_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    corrected_at TEXT NOT NULL,
    previous_outcome TEXT NOT NULL,
    -- 'needs_human' for the first real verdict a row ever gets (apply_human_
    -- verdict); 'correct'/'partially_correct'/'incorrect' when a parent
    -- flips a verdict the child was already shown.
    previous_needs_human_cause TEXT,
    new_outcome TEXT NOT NULL,
    previous_student_answer_raw TEXT NOT NULL,
    new_student_answer_raw TEXT NOT NULL,
    -- Equal to previous_student_answer_raw when only the verdict changed --
    -- always populated, never NULL, so "did the transcription change too" is
    -- a plain string comparison, not a NULL check.
    source TEXT NOT NULL,
    -- 'needs_human_resolution' | 'decided_verdict_correction' |
    -- 'dispute_overturned' -- which of k12ta.store.sessions's three
    -- verdict-changing functions produced this row.
    FOREIGN KEY (student_id, session_id, capture_id, problem_id)
        REFERENCES graded_problems (student_id, session_id, capture_id, problem_id)
);

CREATE INDEX idx_verdict_correction_audit_log_problem
    ON verdict_correction_audit_log (student_id, session_id, capture_id, problem_id);
