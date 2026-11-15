-- The "proposal by e-mail" questionnaire: one row per request, from the first
-- question to the team's answer. Not the morning briefing (table `briefings`).
-- The answers (a JSON object) are a sealed field from the start: the old
-- column is locked to ''. A contact has one active questionnaire at a time
-- ('open' or 'incomplete_sent'). `notified_at` records the team told that the
-- request was sent; `incomplete_notified_at`, that it was abandoned. Safe to repeat.
CREATE TABLE IF NOT EXISTS proposal_briefings (
    id                     BIGSERIAL PRIMARY KEY,
    contact_id             INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status                 TEXT NOT NULL CHECK (status IN
                               ('open', 'sent', 'incomplete_sent', 'cancelled', 'proposal_sent')),
    step                   TEXT,
    answers                TEXT NOT NULL DEFAULT '',
    answers_sealed BYTEA,
    prefilled              TEXT[] NOT NULL DEFAULT '{}',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at           TIMESTAMPTZ,
    notified_at            TIMESTAMPTZ,
    incomplete_notified_at TIMESTAMPTZ,
    proposal_sent_at       TIMESTAMPTZ,
    proposal_sent_by       INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    CONSTRAINT proposal_briefings_answers_sealed_only CHECK (answers = '')
);
CREATE UNIQUE INDEX IF NOT EXISTS proposal_briefings_one_active
    ON proposal_briefings (contact_id) WHERE status IN ('open', 'incomplete_sent');
CREATE INDEX IF NOT EXISTS idx_proposal_briefings_status
    ON proposal_briefings (status, updated_at);
