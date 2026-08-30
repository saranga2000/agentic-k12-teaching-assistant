-- A child's own "remind my grown-up about this" flag (M7's "my pages" view,
-- k12ta.web.app), for a graded_problems row still waiting on a person --
-- honest and local-only, since no email/SMS infra exists to page anyone: a
-- parent sees it as a badge on k12ta.keys' pending list next time the app is
-- open, not a push notification. NULL until a student taps the button; never
-- cleared automatically (the item leaving "needs_human" removes it from both
-- screens on its own).
ALTER TABLE graded_problems ADD COLUMN reminder_requested_at TEXT;
