-- The video meeting link (Google Meet) created with the calendar event. Kept
-- here because the confirmation, the reminder and the listing all say it, and
-- asking the calendar for it each time would be a network call per message.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS meeting_url TEXT;
