-- Explicit membership, independent of Telegram and of general panel access.
CREATE TABLE IF NOT EXISTS admin_room_members (
    user_id INTEGER PRIMARY KEY REFERENCES panel_users(id) ON DELETE CASCADE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admin_room_messages (
    id BIGSERIAL PRIMARY KEY,
    sender_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('person', 'bot', 'alert')),
    body_enc BYTEA NOT NULL,
    nonce UUID,
    event_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sender_id, nonce),
    CHECK ((kind = 'person') = (nonce IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS admin_monitor_state (
    name TEXT PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL,
    healthy BOOLEAN NOT NULL,
    failed_since TIMESTAMPTZ,
    announced BOOLEAN NOT NULL DEFAULT FALSE,
    generation BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_alert_outbox (
    message_id BIGINT PRIMARY KEY REFERENCES admin_room_messages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    push_done BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_alert_deliveries (
    message_id BIGINT REFERENCES admin_alert_outbox(message_id) ON DELETE CASCADE,
    subscription_id BIGINT REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, subscription_id)
);
