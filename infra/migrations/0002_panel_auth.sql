-- /painel sign-in: one account per person, password + authenticator code.
-- Nothing stored here is enough to get in on its own — see
-- core/services/panel_auth.py for what is hashed and what is encrypted.

CREATE TABLE IF NOT EXISTS panel_users (
    id               SERIAL PRIMARY KEY,
    email            VARCHAR(160) NOT NULL UNIQUE,   -- always lower-case
    name             VARCHAR(160) NOT NULL,
    password_hash    TEXT,                           -- Argon2id; NULL until enrolled
    totp_secret_enc  TEXT,                           -- Fernet, PANEL_SECRET_KEY
    enrolled_at      TIMESTAMPTZ,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    failed_attempts  INTEGER NOT NULL DEFAULT 0,
    locked_until     TIMESTAMPTZ,
    -- The last TOTP step accepted: a code works once, even inside its window.
    last_totp_step   BIGINT,
    last_login_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A one-time link to set a password and the authenticator. Only the hash.
CREATE TABLE IF NOT EXISTS panel_invites (
    token_hash  CHAR(64) PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS panel_recovery_codes (
    user_id    INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    code_hash  CHAR(64) NOT NULL,
    used_at    TIMESTAMPTZ,
    PRIMARY KEY (user_id, code_hash)
);

-- mfa = false is the short step between the password and the code.
CREATE TABLE IF NOT EXISTS panel_sessions (
    token_hash    CHAR(64) PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES panel_users(id) ON DELETE CASCADE,
    mfa           BOOLEAN NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    ip            VARCHAR(64),
    user_agent    VARCHAR(300)
);
CREATE INDEX IF NOT EXISTS idx_panel_sessions_user ON panel_sessions (user_id);

-- Who signed in, who failed, who was locked out. Read with SQL when needed.
CREATE TABLE IF NOT EXISTS panel_auth_events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    email       VARCHAR(160),
    event       VARCHAR(30) NOT NULL,
    ip          VARCHAR(64),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_panel_auth_events_time ON panel_auth_events (created_at DESC);
