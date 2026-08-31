-- Gap O (docs/USER_WORKFLOWS.md): a brand-new program's structure can now be
-- proposed by the app (from a child's own capture) and confirmed by the
-- child, instead of staying an outright refusal until a parent sets one up.
-- `provenance` records who authored a schema version -- default 'parent'
-- for every existing row, since the mechanism this migration enables didn't
-- exist before it: every schema saved until now really was parent-authored.
-- 'unconfirmed' is the only other value written today (k12ta.web.app's
-- bootstrap-schema submission) -- a parent's own save always writes
-- 'parent', whether that's a first-time author, a correction, or a bare
-- confirmation of what's already there.
ALTER TABLE page_identity_schemas ADD COLUMN provenance TEXT NOT NULL DEFAULT 'parent';

-- One unacknowledged "a grown-up changed how pages are identified for this
-- program" notice per (student, source) -- set when a parent's correction to
-- a not-yet-parent-confirmed schema retroactively changes what some already-
-- graded pages resolved to (k12ta.pipeline.process.replay_source, run
-- automatically for exactly this one trigger -- see docs/USER_WORKFLOWS.md
-- §3.5 for why every other regrade trigger in this app stays manual).
-- Re-tapping "Got it" clears it; a fresh correction before that happens
-- overwrites the timestamp rather than stacking a second notice, same
-- "nothing to protect against a repeat" reasoning as reminder_requested_at.
CREATE TABLE identity_corrections (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    corrected_at TEXT NOT NULL,
    PRIMARY KEY (student_id, source_id),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);
