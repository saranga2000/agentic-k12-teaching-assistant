-- Gap A (docs/USER_WORKFLOWS.md): a child's "no programs set up yet" empty
-- state can now flag the parent app, in-app only -- no email/SMS infra exists,
-- same limitation already accepted for graded_problems.reminder_requested_at
-- (0019_graded_problem_reminder.sql). One row per student, overwritten on
-- each request -- re-tapping just updates the timestamp, nothing to protect
-- against a repeat, same reasoning as the reminder flag.
CREATE TABLE program_requests (
    student_id TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    FOREIGN KEY (student_id) REFERENCES students (student_id)
);
