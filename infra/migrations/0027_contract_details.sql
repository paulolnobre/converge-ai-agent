-- What the contract needs and the proposal request does not have: the party
-- (legal name, CNPJ or CPF, address) and who signs (name, CPF, e-mail, role),
-- sent by the customer after accepting the proposal (WhatsApp Flow
-- "dados_contrato"). One row per request, all of it one sealed JSON document:
-- the clear column is locked to ''. Safe to repeat.
CREATE TABLE IF NOT EXISTS contract_details (
    id           BIGSERIAL PRIMARY KEY,
    briefing_id  BIGINT NOT NULL UNIQUE REFERENCES proposal_briefings(id) ON DELETE CASCADE,
    data         TEXT NOT NULL DEFAULT '',
    data_sealed  BYTEA,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT contract_details_data_sealed_only CHECK (data = '')
);
