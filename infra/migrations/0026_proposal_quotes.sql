-- The preliminary quote of a proposal request, for the team only: calculated
-- once from the questionnaire's button answers and content/pricing.toml when
-- the request reaches the team, then edited by the team. One row per request.
-- calc_*: what the table gave; setup_cents/monthly_cents: the current values.
-- The team's note is sealed from the start: the clear column is locked to NULL.
-- Safe to repeat.
CREATE TABLE IF NOT EXISTS proposal_quotes (
    id                     BIGSERIAL PRIMARY KEY,
    briefing_id            BIGINT NOT NULL UNIQUE REFERENCES proposal_briefings(id) ON DELETE CASCADE,
    inputs                 JSONB NOT NULL,
    scope                  JSONB NOT NULL,
    detail                 JSONB NOT NULL,
    premises               JSONB NOT NULL,
    table_digest           TEXT NOT NULL,
    calc_setup_cents       INTEGER NOT NULL,
    calc_setup_low_cents   INTEGER NOT NULL,
    calc_setup_high_cents  INTEGER NOT NULL,
    calc_monthly_cents     INTEGER NOT NULL,
    setup_cents            INTEGER NOT NULL,
    monthly_cents          INTEGER NOT NULL,
    note                   TEXT,
    note_sealed            BYTEA,
    edited_by              INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    edited_at              TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT proposal_quotes_note_sealed_only CHECK (note IS NULL)
);
