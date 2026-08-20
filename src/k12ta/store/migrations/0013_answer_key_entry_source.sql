-- M3.4: mirrors 0008_page_identity_source.sql exactly, same field, same two
-- values, same reasoning -- who supplied the value, not how sure anyone was, so
-- an eval can measure accuracy against only what the model actually produced.
ALTER TABLE answer_key_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'model';
