-- The setup charged when the contract is signed (spec venda-ate-pagamento,
-- increment 5): the charge knows its proposal request and which payment of
-- the setup it is (1 = the one at signing), so a repeated webhook never
-- charges twice. The customer's card choice is charged as a card. The
-- contract keeps the setup's payments it stated when it was sent: the charge
-- follows the signed contract, not a quote edited later.
-- Safe to repeat.
ALTER TABLE contract_signatures ADD COLUMN IF NOT EXISTS setup_terms JSONB;
ALTER TABLE billing_charges
    ADD COLUMN IF NOT EXISTS briefing_id BIGINT REFERENCES proposal_briefings(id),
    ADD COLUMN IF NOT EXISTS setup_part  SMALLINT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_charges_setup_part
    ON billing_charges (briefing_id, setup_part) WHERE briefing_id IS NOT NULL;

ALTER TABLE billing_charges DROP CONSTRAINT IF EXISTS billing_charges_origin_check;
ALTER TABLE billing_charges ADD CONSTRAINT billing_charges_origin_check
    CHECK (origin IN ('painel', 'conversa', 'assinatura', 'contrato'));
ALTER TABLE billing_charges DROP CONSTRAINT IF EXISTS billing_charges_method_check;
ALTER TABLE billing_charges ADD CONSTRAINT billing_charges_method_check
    CHECK (method IN ('link', 'boleto', 'pix', 'cartao'));
