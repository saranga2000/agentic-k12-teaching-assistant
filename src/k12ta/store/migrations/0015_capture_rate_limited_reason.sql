-- A real provider rate-limit exhaustion (FailureKind.RATE_LIMITED) is not a
-- transcription problem -- the photo may be perfectly legible, the provider is just
-- out of capacity. Its own column, kept distinct from transcribe_failure_reason
-- (0014), so the two causes can be told apart by a query, not by parsing free text.
-- Never both set for the same capture: process_capture's failure branches are mutually
-- exclusive by construction.
ALTER TABLE page_captures ADD COLUMN rate_limited_reason TEXT;
