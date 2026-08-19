-- Supports asking the student when exactly one identity component is missing
-- (see docs/ARCHITECTURE.md's "asking when exactly one component is missing"
-- section, and k12ta.grading.page_identity.resolve_partial). Only the
-- components a photo actually read need to be stored -- the candidates
-- themselves are never persisted, and are always re-derived fresh from the
-- current page_identities table both when the pick screen renders and again
-- when a pick is submitted, so a mapping added or corrected in between is
-- never missed or silently trusted stale.
ALTER TABLE page_identity_resolutions ADD COLUMN seen_values_json TEXT;
