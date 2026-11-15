-- Blocking a contact and ending a conversation, from the panel's Inbox.
--   blocked_at: the bot answers nothing and tells nobody; the messages are
--     still recorded, under the "Bloqueadas" filter. NULL when not blocked.
--   ended_at: the team ended the conversation. It is archived until the
--     person writes again, which starts a new one. Safe to repeat.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS blocked_by_user_id BIGINT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
