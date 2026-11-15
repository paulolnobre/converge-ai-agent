-- The reply the owner leaves with a "Reabrir conversa" template, delivered on
-- the customer's first message after it (usually the button tap).
-- reopen_pending_at: a reopen went out and the customer has not written since;
-- clearing it is the claim, so the reply (or the "só um instante" notice) goes once.
-- The reply is sealed from the start: the clear column is locked to NULL. Safe to repeat.
ALTER TABLE handoff_sessions ADD COLUMN IF NOT EXISTS reopen_pending_at TIMESTAMPTZ;
ALTER TABLE handoff_sessions ADD COLUMN IF NOT EXISTS reopen_reply TEXT;
ALTER TABLE handoff_sessions ADD COLUMN IF NOT EXISTS reopen_reply_sealed BYTEA;
ALTER TABLE handoff_sessions DROP CONSTRAINT IF EXISTS handoff_sessions_reopen_reply_sealed_only;
ALTER TABLE handoff_sessions ADD
  CONSTRAINT handoff_sessions_reopen_reply_sealed_only CHECK (reopen_reply IS NULL);
