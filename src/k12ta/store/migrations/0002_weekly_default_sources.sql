-- M2.2: which content source a student's capture defaults to on a given weekday,
-- read by the capture surface so "today's assignment" is never a code constant.

CREATE TABLE weekly_default_sources (
    student_id TEXT NOT NULL REFERENCES students (student_id),
    weekday INTEGER NOT NULL,  -- date.weekday(): 0 = Monday .. 6 = Sunday
    source_id TEXT NOT NULL,
    PRIMARY KEY (student_id, weekday),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);
