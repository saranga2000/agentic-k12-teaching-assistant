-- M2.4 corner case: a parent re-scanning a key page must never silently overwrite a
-- stored answer that disagrees with the new scan -- a wrong key marks correct work
-- wrong, the worst failure this system has. Every confirm action (a brand new entry,
-- an identical re-scan, or an explicitly resolved conflict) writes one row here.

CREATE TABLE answer_key_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    problem_number TEXT NOT NULL,
    action TEXT NOT NULL,
    -- one of "created", "matched", "conflict_resolved"
    old_answer_text TEXT,
    old_ungradeable_reason TEXT,
    new_answer_text TEXT,
    new_ungradeable_reason TEXT,
    resolution TEXT,
    -- one of "kept_old", "used_new"; NULL for "created" / "matched"
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

CREATE INDEX idx_answer_key_audit_log_student ON answer_key_audit_log (student_id);
