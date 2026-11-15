-- Photos, files and audio the customer sends, kept encrypted for the Inbox
-- (docs/superpowers/specs/2026-10-22-midia-no-inbox-design.md). The bytes live
-- in files under MEDIA_DIR, sealed by the app; this table says whose they are,
-- which line of the conversation they belong to and until when they are kept.
-- The original file name is a sealed field from the start: its old column is
-- locked empty (the same lock cli.seal_data installs for the other fields).
-- Safe to repeat.
CREATE TABLE IF NOT EXISTS media (
  id              BIGSERIAL PRIMARY KEY,
  contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  session_id      BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  chat_id         VARCHAR(20) NOT NULL,
  source_id       VARCHAR(160) NOT NULL UNIQUE,
  kind            VARCHAR(10) NOT NULL
                  CHECK (kind IN ('image', 'audio', 'video', 'document', 'sticker')),
  mime            VARCHAR(100),
  declared_bytes  BIGINT CHECK (declared_bytes >= 0),
  stored_bytes    BIGINT CHECK (stored_bytes >= 0),
  filename        TEXT,
  filename_sealed BYTEA,
  storage_key     UUID UNIQUE,
  status          VARCHAR(12) NOT NULL
                  CHECK (status IN ('stored', 'too_large', 'failed', 'no_space',
                                    'rate_limited', 'late', 'expired', 'missing')),
  stored_at       TIMESTAMPTZ,
  hold_until      TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ NOT NULL,
  deleted_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT media_filename_sealed_only CHECK (filename IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_media_contact ON media (contact_id);
CREATE INDEX IF NOT EXISTS idx_media_session ON media (session_id);
CREATE INDEX IF NOT EXISTS idx_media_status_expires ON media (status, expires_at);

-- The updates carrying media already handled: a turn that committed and lost
-- its ACK is not handled twice. Only the update id, never content.
CREATE TABLE IF NOT EXISTS media_events (
  update_id  VARCHAR(160) PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
