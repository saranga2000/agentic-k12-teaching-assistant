-- docs/ROADMAP.md's V1 "Attempts": "a resubmission is only accepted if she
-- confirms she actually redid the work." NULL means pending confirmation
-- (this capture is a 2nd or 3rd photograph of a page that already has one on
-- file); non-NULL is the timestamp confirmation happened -- either the
-- child's own explicit "yes I redid it" tap, or auto-set for a page's first
-- capture, which has nothing to confirm redoing. Never cleared once set.
ALTER TABLE page_captures ADD COLUMN resubmit_confirmed_at TEXT DEFAULT NULL;
