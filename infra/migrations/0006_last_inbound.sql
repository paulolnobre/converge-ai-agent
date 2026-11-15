-- When the person last wrote, in the channel's own clock: WhatsApp takes
-- free-form messages only within 24h of that moment. Set from the time the
-- channel stamped the message, not when it was processed, so a message held
-- for later does not look newer than it is. Safe to repeat.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;

-- Contacts that wrote before this column existed: their last message on record.
UPDATE contacts c
   SET last_inbound_at = s.last_at
  FROM (SELECT contact_id, max(created_at) AS last_at
          FROM sessions WHERE role = 'user' GROUP BY contact_id) s
 WHERE s.contact_id = c.id AND c.last_inbound_at IS NULL;
