-- M2 schema: students, content sources, assignments, page captures, problems,
-- graded problems, sessions, and skill mastery traces.
--
-- Every table's primary key includes student_id, and every foreign key that points
-- at a parent table includes student_id in the join. That is deliberate: it means a
-- row cannot be inserted that references another student's assignment, capture, or
-- session, because SQLite has nothing to match the foreign key against. Scoping is
-- enforced by the schema, not only by application code remembering to filter.

CREATE TABLE students (
    student_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    grade_level INTEGER NOT NULL,
    state_code TEXT NOT NULL,
    coach_name TEXT NOT NULL,
    birth_year INTEGER
);

CREATE TABLE content_sources (
    student_id TEXT NOT NULL REFERENCES students (student_id),
    source_id TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    has_answer_key INTEGER NOT NULL,
    graded_by_someone_else INTEGER NOT NULL,
    default_mode TEXT NOT NULL,
    typical_session_minutes INTEGER NOT NULL,
    standards_frame TEXT,
    PRIMARY KEY (student_id, source_id)
);

CREATE TABLE assignments (
    student_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (student_id, assignment_id),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

CREATE TABLE page_captures (
    student_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    image_path TEXT NOT NULL,
    PRIMARY KEY (student_id, capture_id),
    FOREIGN KEY (student_id, assignment_id) REFERENCES assignments (student_id, assignment_id)
);

CREATE TABLE problems (
    student_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    student_answer_raw TEXT NOT NULL,
    transcription_confidence REAL NOT NULL,
    skill_ids TEXT NOT NULL DEFAULT '[]',
    page_region TEXT,
    PRIMARY KEY (student_id, capture_id, problem_id),
    FOREIGN KEY (student_id, capture_id) REFERENCES page_captures (student_id, capture_id)
);

CREATE TABLE sessions (
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    PRIMARY KEY (student_id, session_id),
    FOREIGN KEY (student_id, assignment_id) REFERENCES assignments (student_id, assignment_id)
);

CREATE TABLE graded_problems (
    student_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    problem_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    expected_answer TEXT,
    grader_confidence REAL NOT NULL,
    diagnosis_misconception_id TEXT,
    diagnosis_explanation TEXT,
    diagnosis_error_location TEXT,
    diagnosis_skill_ids TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (student_id, session_id, capture_id, problem_id),
    FOREIGN KEY (student_id, session_id) REFERENCES sessions (student_id, session_id),
    FOREIGN KEY (student_id, capture_id, problem_id)
        REFERENCES problems (student_id, capture_id, problem_id)
);

CREATE TABLE skill_mastery_traces (
    student_id TEXT NOT NULL REFERENCES students (student_id),
    skill_id TEXT NOT NULL,
    p_at_last_review REAL NOT NULL,
    stability_days REAL NOT NULL,
    last_reviewed_on TEXT NOT NULL,
    review_count INTEGER NOT NULL,
    correct_count INTEGER NOT NULL,
    PRIMARY KEY (student_id, skill_id)
);

CREATE INDEX idx_assignments_student ON assignments (student_id);
CREATE INDEX idx_page_captures_student ON page_captures (student_id);
CREATE INDEX idx_problems_student ON problems (student_id);
CREATE INDEX idx_sessions_student ON sessions (student_id);
CREATE INDEX idx_graded_problems_student ON graded_problems (student_id);
