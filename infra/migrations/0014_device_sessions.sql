-- Installed panel: explicitly trusted devices keep their MFA session until
-- 90 idle days or revocation. Existing browser sessions retain their limits.
ALTER TABLE panel_sessions
  ADD COLUMN IF NOT EXISTS id BIGSERIAL,
  ADD COLUMN IF NOT EXISTS kind VARCHAR(10) NOT NULL DEFAULT 'browser'
    CHECK (kind IN ('browser', 'device')),
  ADD COLUMN IF NOT EXISTS device_name VARCHAR(80),
  ADD COLUMN IF NOT EXISTS cookie_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_panel_sessions_id ON panel_sessions (id);
CREATE INDEX IF NOT EXISTS idx_panel_sessions_device_seen
  ON panel_sessions (last_seen_at) WHERE kind = 'device' AND mfa;
