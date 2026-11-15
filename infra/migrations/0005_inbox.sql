-- The Inbox: the team answering from /painel instead of from Telegram, the
-- channel a contact talks through and how they found Converge.
-- Safe to repeat, like every migration (cli/migrate.py applies it once).

-- contacts.source already holds the channel (init.sql: "the channel the
-- person first arrived through"). It gains the channels this service knows.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contacts_source_channel') THEN
    ALTER TABLE contacts ADD CONSTRAINT contacts_source_channel
      CHECK (source IN ('telegram', 'whatsapp', 'instagram', 'messenger', 'email'));
  END IF;
END $$;

-- How the person found Converge: the first one known wins, the team may
-- change it in the panel (origin_set_by says who did).
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS origin VARCHAR(30)
    CHECK (origin IN ('anuncio_instagram', 'anuncio_facebook', 'site', 'google',
                      'indicacao', 'outro')),
  ADD COLUMN IF NOT EXISTS origin_detail VARCHAR(160),
  ADD COLUMN IF NOT EXISTS origin_set_by INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
  -- When the team group was last told this conversation started: one notice
  -- per conversation, a conversation being 24h without the person writing.
  ADD COLUMN IF NOT EXISTS conversation_noticed_at TIMESTAMPTZ;

-- Who holds a conversation from the panel (claimed_by_id stays for the claims
-- made the old way, by replying in Telegram), and when they were last told
-- the customer wrote.
ALTER TABLE handoff_sessions
  ADD COLUMN IF NOT EXISTS claimed_by_user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS last_ping_at TIMESTAMPTZ;

-- A conversation someone took the old way, by replying in Telegram, would
-- stay silent after this deploy: that person can no longer answer from
-- Telegram, and has no /painel account on the session to be told. Back to
-- waiting: it shows under "Esperando pessoa", the bot answers meanwhile, and
-- whoever takes it in the panel carries on from the history.
UPDATE handoff_sessions
   SET status = 'open', claimed_by_id = NULL, claimed_by_name = NULL,
       last_activity_at = clock_timestamp()
 WHERE status = 'claimed' AND claimed_by_user_id IS NULL;

-- Who in the team wrote a message sent from the panel.
ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS author_user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL;
