-- The contract of a proposal request sent to be signed on Autentique (spec
-- venda-ate-pagamento, increment 4, part 3). One row per request: written as
-- "sending" before Autentique is called, so a second click sends nothing, and
-- removed again when Autentique refused it. Nothing personal is kept here: the
-- signing link is asked of Autentique again whenever it is needed.
-- Safe to repeat.
CREATE TABLE IF NOT EXISTS contract_signatures (
    id                   BIGSERIAL PRIMARY KEY,
    briefing_id          BIGINT NOT NULL UNIQUE REFERENCES proposal_briefings(id) ON DELETE CASCADE,
    status               TEXT NOT NULL DEFAULT 'sending',
    sandbox              BOOLEAN NOT NULL,
    document_id          TEXT,
    customer_signature   TEXT,
    converge_signature   TEXT,
    sent_by              INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at              TIMESTAMPTZ,
    CONSTRAINT contract_signatures_status CHECK (status IN ('sending', 'sent'))
);

CREATE UNIQUE INDEX IF NOT EXISTS contract_signatures_document
    ON contract_signatures (document_id) WHERE document_id IS NOT NULL;
