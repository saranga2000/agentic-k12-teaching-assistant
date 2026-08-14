-- Replaces the single-authoritative-kind page-identity design (0007/0008) with a
-- per-source, versioned, composite identity schema: Summer Bridge needs section AND
-- day together, which a single `page_identity_kind` string has no way to express.
-- See docs/ROADMAP.md's page-identity discussion for the full argument.
--
-- content_sources.page_identity_kind (0007) is left in place, unused -- dropping a
-- column is unnecessary risk for one that just goes unused now.

-- The per-source, versioned, ordered list of identity components. Never mutated in
-- place: editing a schema inserts a new schema_version rather than updating an old
-- one, so an old version stays queryable -- same additive philosophy as
-- answer_key_audit_log. A source's current schema is MAX(schema_version) for it;
-- zero rows means no schema has been learned yet, and that source never
-- auto-resolves until a parent's first key scan teaches one.
CREATE TABLE page_identity_schemas (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    component_name TEXT NOT NULL,
    label TEXT NOT NULL,
    example TEXT,
    position INTEGER NOT NULL,
    PRIMARY KEY (student_id, source_id, schema_version, component_name),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

-- page_identities (0007/0008) had zero production rows as of this migration
-- (confirmed by direct query before writing it), so this recreates the table with
-- a composite key rather than migrating data that does not exist. composite_key is
-- the schema's component values joined in schema position order with a control
-- character separator (never printable, so it can't collide with a real marker
-- value). schema_version is the version this mapping was confirmed under -- a
-- resolve() lookup always filters on the source's *current* version, so a mapping
-- confirmed under an old schema is never deleted and never silently reinterpreted
-- under the new shape; it just stops being eligible for auto-resolution until a
-- parent re-confirms it, which is exactly the "flagged for review, not dropped or
-- silently reinterpreted" requirement this replaces the old single-string design
-- to satisfy.
DROP TABLE page_identities;

CREATE TABLE page_identities (
    student_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    composite_key TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'model',
    PRIMARY KEY (student_id, source_id, composite_key),
    FOREIGN KEY (student_id, source_id) REFERENCES content_sources (student_id, source_id)
);

-- PARTIAL_PAGE_MARKERS is the one needs-human cause whose message needs facts
-- beyond the cause itself ("I can see the day but not the section") -- decided
-- here, in the pipeline, never re-derived by the renderer, same rule as every
-- other cause. Small JSON object, {"seen": [...], "missing": [...]}, using the
-- schema's parent-facing labels -- same "structured fact in a text column"
-- precedent diagnosis_skill_ids already set on this table. NULL for every other
-- cause.
ALTER TABLE graded_problems ADD COLUMN needs_human_detail TEXT;
