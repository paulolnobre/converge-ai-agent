-- Push notifications in the panel app: one subscription per browser, tied to
-- the session of the device that made it. Signing out, disconnecting the
-- device or removing the session takes the subscription with it.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES panel_sessions(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ok_at  TIMESTAMPTZ,
    failures    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_session ON push_subscriptions (session_id);
