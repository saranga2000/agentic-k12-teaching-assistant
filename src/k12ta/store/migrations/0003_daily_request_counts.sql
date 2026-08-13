-- M2.3: a persisted daily count of model-provider requests, so the quota gate in
-- k12ta.pipeline survives a server restart. Deliberately not student-scoped: the
-- resource being protected (one shared API key's daily quota) is a household-level
-- resource, not a per-child one. See docs/ARCHITECTURE.md, "Multi-user".

CREATE TABLE daily_request_counts (
    request_date TEXT PRIMARY KEY,
    request_count INTEGER NOT NULL DEFAULT 0
);
