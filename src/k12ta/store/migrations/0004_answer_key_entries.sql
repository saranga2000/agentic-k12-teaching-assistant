-- M2.4: confirmed answer-key entries, one row per (student, source, workbook page,
-- problem). A wrong key silently marks correct work as wrong -- the worst failure in
-- this system -- so ungradeable entries (an open-ended "answers will vary" prompt, or
-- a graph/table with no text answer to store) are represented explicitly rather than
-- guessed at: exactly one of answer_text / ungradeable_reason is ever set.

CREATE TABLE answer_key_entries (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    problem_number TEXT NOT NULL,
    answer_text TEXT,
    ungradeable_reason TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (student_id, source_id, page_number, problem_number),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id),
    CHECK (
        (answer_text IS NOT NULL AND ungradeable_reason IS NULL) OR
        (answer_text IS NULL AND ungradeable_reason IS NOT NULL)
    )
);

CREATE INDEX idx_answer_key_entries_student ON answer_key_entries (student_id);
