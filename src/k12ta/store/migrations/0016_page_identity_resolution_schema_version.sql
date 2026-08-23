-- Which schema version a resolution attempt actually resolved (or failed) against --
-- needed once k12ta.grading.page_identity.resolve_with_schema_history can try two
-- versions for one photo (a source's current schema, then its immediately preceding
-- one as a fallback; see docs/ROADMAP.md's M3.7). Nullable: every row written before
-- this migration predates the fallback entirely and was always resolved against
-- whatever "current" meant at the time, which is not reconstructable after the fact.
ALTER TABLE page_identity_resolutions ADD COLUMN schema_version INTEGER;
