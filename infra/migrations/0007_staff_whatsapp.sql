-- The team on WhatsApp: the chat ids (wa:<number>) each person's number may
-- arrive as, so their own chats are left out of the customer lists like their
-- Telegram ones. Brazilian mobiles come with and without the ninth digit, so a
-- person may have two. Safe to repeat.
ALTER TABLE staff ADD COLUMN IF NOT EXISTS whatsapp_ids TEXT[] NOT NULL DEFAULT '{}';
