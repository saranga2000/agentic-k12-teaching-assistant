-- M3: "Parent override requires a PIN and writes an audit row"
-- (docs/ROADMAP.md). `resolve_mode()`'s `parent_override` parameter
-- (k12ta.domain.policy) has existed since M3.2 with nothing ever supplying
-- it; this is that supply -- current state (one row per student+source, the
-- override in effect right now, or none) plus an append-only audit log of
-- every change, mirroring answer_key_audit_log's own shape and reasoning
-- (0005_answer_key_audit_log.sql): a record of what happened, never
-- rewritten, separate from current state.

CREATE TABLE policy_overrides (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY (student_id, source_id),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

CREATE TABLE policy_override_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    previous_mode TEXT,
    -- NULL means "no override was in effect before this change"
    new_mode TEXT,
    -- NULL means this change cleared the override, back to automatic resolution
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

CREATE INDEX idx_policy_override_audit_log_student ON policy_override_audit_log (student_id);
