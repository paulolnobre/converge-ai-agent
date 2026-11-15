-- Outgoing Inbox attachments and files shared only with explicit room members.
-- Existing incoming media retain their target and retention policy.
ALTER TABLE media ALTER COLUMN contact_id DROP NOT NULL;
ALTER TABLE media ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE media ADD COLUMN IF NOT EXISTS admin_message_id BIGINT REFERENCES admin_room_messages(id) ON DELETE CASCADE;
ALTER TABLE media ADD COLUMN IF NOT EXISTS author_user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL;
ALTER TABLE media ADD COLUMN IF NOT EXISTS client_nonce UUID;
ALTER TABLE media ADD COLUMN IF NOT EXISTS request_fingerprint TEXT;
ALTER TABLE media ADD COLUMN IF NOT EXISTS delivery_state VARCHAR(12)
 CHECK (delivery_state IN ('sending', 'sent', 'failed', 'uncertain'));
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='media_one_target' AND conrelid='media'::regclass) THEN
 ALTER TABLE media ADD CONSTRAINT media_one_target CHECK (
  (contact_id IS NOT NULL AND session_id IS NOT NULL AND admin_message_id IS NULL) OR
  (contact_id IS NULL AND session_id IS NULL AND admin_message_id IS NOT NULL));
 END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_author_nonce ON media(author_user_id, client_nonce) WHERE client_nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_media_admin_message ON media(admin_message_id) WHERE admin_message_id IS NOT NULL;
