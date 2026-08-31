-- Gap B/K/L (docs/USER_WORKFLOWS.md): a child's own contest of an already-
-- graded incorrect verdict, escalated into a distinct, parent-visible queue
-- -- not a needs_human row (the grader called this one confidently, unlike
-- the six needs_human causes), and unreachable through "Remind a grown-up"
-- (that button only ever appears on a row the grader itself refused to
-- call). One dispute per graded_problems row, ever: the household's own
-- explicit decision is that a parent's resolution is final, so a resolved
-- dispute is never reopened, and this table's own write path (see
-- k12ta.store.disputes) refuses a second row rather than overwriting one.
CREATE TABLE disputes (
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    -- The child's own short reason, required at filing time (household
    -- decision: not a one-tap action).
    disputed_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    -- 'upheld' (the incorrect verdict stands) or 'overturned' (the child was
    -- right; graded_problems.outcome is flipped to correct in the same
    -- action -- see k12ta.store.sessions.overturn_dispute_to_correct). NULL
    -- while still open.
    resolution_comment TEXT,
    -- Required at resolution time -- mandatory for a dispute specifically
    -- (household decision), unlike an ordinary NEEDS_HUMAN verdict, where a
    -- comment stays optional (Gap L).
    PRIMARY KEY (student_id, session_id, capture_id, problem_id),
    FOREIGN KEY (student_id, session_id, capture_id, problem_id)
        REFERENCES graded_problems (student_id, session_id, capture_id, problem_id)
);

CREATE INDEX idx_disputes_open ON disputes (student_id, resolved_at);
