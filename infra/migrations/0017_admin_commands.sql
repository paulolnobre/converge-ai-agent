-- Confirmations belong to the account that asked, not to whoever sees a token.
CREATE TABLE IF NOT EXISTS admin_room_commands (
    id UUID PRIMARY KEY,
    message_id BIGINT NOT NULL UNIQUE REFERENCES admin_room_messages(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    action VARCHAR(32) NOT NULL CHECK (action IN ('inbox.claim', 'inbox.release', 'agenda.sync')),
    -- Keep the audit target when a contact/test is later deleted.
    contact_id INTEGER,
    expected JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(expected) = 'object'),
    status VARCHAR(12) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'cancelled', 'expired', 'refused')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '5 minutes'),
    finished_at TIMESTAMPTZ,
    CHECK ((action = 'agenda.sync') = (contact_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_admin_commands_actor ON admin_room_commands (user_id, status);
