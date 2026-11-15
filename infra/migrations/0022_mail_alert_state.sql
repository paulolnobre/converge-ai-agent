-- Where the mailbox watcher stopped reading: the last UID it has seen, and the
-- UIDVALIDITY it belongs to (a new one means the UIDs started over). One row
-- per mailbox. Safe to repeat.
CREATE TABLE IF NOT EXISTS mail_alert_state (
    mailbox      VARCHAR(160) PRIMARY KEY,
    uidvalidity  BIGINT NOT NULL,
    last_uid     BIGINT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
