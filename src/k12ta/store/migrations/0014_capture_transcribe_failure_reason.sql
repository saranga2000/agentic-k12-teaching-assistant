-- A transcribe failure (a raised exception, or a declared TranscriptionResult.failure)
-- previously only reached a log line, which does not survive a server restart. 3 of 10
-- real dev-jahnvi/summer_bridge captures produced zero problem rows with no recoverable
-- reason. Nullable: set only on a capture whose transcribe step failed, never on a
-- successful one (including a successful transcribe that found zero problems -- that is
-- not a failure, see k12ta.pipeline.process).
ALTER TABLE page_captures ADD COLUMN transcribe_failure_reason TEXT;
