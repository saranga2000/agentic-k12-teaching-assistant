-- docs/ROADMAP.md's V1 "Archiving": a parent can archive a program so children
-- can no longer upload to it, while everything already evaluated stays fully
-- visible to both parent and child, and the parent's review queue on it stays
-- workable. 0/1, NOT NULL DEFAULT 0: every existing source is unarchived, which
-- is correct -- archiving is always a deliberate later parent action, never an
-- inferred state.
ALTER TABLE content_sources ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
