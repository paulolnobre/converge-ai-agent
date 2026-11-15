-- Contact details are asked for before a booking or a handoff, not after.
-- pending_action: what the person asked for while the desk waited for their
--   e-mail and phone ("booking:<day>:<period>", "escalation:<reason>"), resumed
--   when the details arrive. NULL when nothing is waiting.
-- details_requested_at: when the desk asked. Asked once: someone who would
--   rather not give them is not asked again on every request.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS pending_action VARCHAR(60);
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS details_requested_at TIMESTAMPTZ;
