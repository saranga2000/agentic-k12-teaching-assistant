-- Scope B: recognizing which workbook page a student photographed, so a capture can
-- be graded against a real key page instead of always landing on UNKNOWN_PAGE.
--
-- page_identity_kind is per content source, never global (docs/ROADMAP.md's page-
-- identity discussion): Summer Bridge's most legible marker is a "Day N" banner,
-- distinct from its own small printed page number; Kumon's is a worksheet code;
-- RSM's is its globally unique problem numbers. NULL until a parent's source setup
-- flow (M3.1, not built yet) records which applies -- no source is assumed to have
-- one.
ALTER TABLE content_sources ADD COLUMN page_identity_kind TEXT;

-- The day/code/marker -> page_number mapping the key-scanning flow already reads
-- (the "Day N/Page NN" heading) but used to discard once page_number was computed.
-- Populated the same way answer_key_entries is: nothing enters this table until a
-- parent confirms a scanned key page.
CREATE TABLE page_identities (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    identifier_value TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (student_id, source_id, identifier_value),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

-- One row per automatic page-identity resolution attempt (student capture only --
-- a caller-supplied page_number, e.g. from a test or a manual override, is never
-- recorded here, since the point is measuring real extraction behaviour, not
-- padding the count). Four honest outcomes, not a single pass/fail: "resolved",
-- "below_floor", "not_found", and "conflicting" each call for a different fix, and
-- a parent-facing count needs to be able to tell them apart, not just know grading
-- didn't happen.
CREATE TABLE page_identity_resolutions (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    resolved_page_number INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

CREATE INDEX idx_page_identity_resolutions_source
    ON page_identity_resolutions (student_id, source_id);
