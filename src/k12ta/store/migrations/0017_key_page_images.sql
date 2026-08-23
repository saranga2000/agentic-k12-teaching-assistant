-- Key-scan images, persisted going forward (2026-08-22 parent-scan-display work,
-- docs/ROADMAP.md). k12ta.pipeline.key_ingestion discarded every upload before this --
-- nothing retroactive is recoverable, only new scans get an image on file from here on.
-- Keyed by page_number, not capture_id: k12ta.keys.app.submit_confirm links whatever
-- page numbers a single scan actually confirmed answers for to that scan's saved image,
-- and a later re-scan of the same page overwrites which image is "the" one for it --
-- same upsert-not-append reasoning as k12ta.store.page_identities.
CREATE TABLE key_page_images (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (student_id, source_id, page_number),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);
