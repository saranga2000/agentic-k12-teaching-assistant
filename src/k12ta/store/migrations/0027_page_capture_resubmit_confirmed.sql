-- docs/ROADMAP.md's V1 "Attempts": "a resubmission is only accepted if she
-- confirms she actually redid the work." NULL means pending confirmation
-- (this capture is a 2nd or 3rd photograph of a page that already has one on
-- file); non-NULL is the timestamp confirmation happened -- either the
-- child's own explicit "yes I redid it" tap, or auto-set for a page's first
-- capture, which has nothing to confirm redoing. Never cleared once set.
ALTER TABLE page_captures ADD COLUMN resubmit_confirmed_at TEXT DEFAULT NULL;

-- A row that already existed before this column did has nothing pending --
-- the gate above is a forward-looking behavior change, not a retroactive one.
-- Found live 2026-09-02: a real household's pre-existing captures were all
-- read as "still awaiting confirmation" (k12ta.respond.render withholds the
-- grade on NULL), masking every already-seen, already-settled result behind
-- a confirmation prompt that never existed when the child first saw it. Safe
-- as a blanket backfill specifically at migration time: nothing has run the
-- app's own confirmation logic against this table yet, so every row present
-- right now necessarily predates the column, and no genuinely-pending
-- resubmission can exist yet to backfill over incorrectly.
UPDATE page_captures SET resubmit_confirmed_at = captured_at WHERE resubmit_confirmed_at IS NULL;
