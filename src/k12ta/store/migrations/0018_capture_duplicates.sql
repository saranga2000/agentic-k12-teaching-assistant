-- A parent's explicit "this photo is the same page as that one" (2026-08-22 M3.9),
-- for the unresolved-capture case automatic dedup can't reach: page_identity_
-- resolutions only groups captures that already resolved to a page_number
-- (k12ta.keys.app._group_pending_by_capture); an unresolved capture has none yet,
-- so it never groups with another unresolved capture of the same physical page
-- without the parent saying so herself. One row per capture -- a capture can be
-- marked a duplicate of at most one other at a time; re-marking overwrites, same
-- upsert-not-append reasoning as k12ta.store.page_identities. Nothing here is ever
-- deleted or regraded; it only changes which block k12ta.keys.app folds a capture's
-- items into on screen.
CREATE TABLE capture_duplicates (
    student_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    duplicate_of_capture_id TEXT NOT NULL,
    marked_at TEXT NOT NULL,
    PRIMARY KEY (student_id, capture_id),
    FOREIGN KEY (student_id, capture_id) REFERENCES page_captures (student_id, capture_id),
    FOREIGN KEY (student_id, duplicate_of_capture_id)
        REFERENCES page_captures (student_id, capture_id)
);
