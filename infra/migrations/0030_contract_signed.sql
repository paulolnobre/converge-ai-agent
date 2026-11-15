-- The contract signed on Autentique (spec venda-ate-pagamento, increment 4,
-- part 3B): when each party signed, and when the document was finished, with
-- the address of the signed copy on Autentique. Set by its webhook.
-- Safe to repeat.
ALTER TABLE contract_signatures
    ADD COLUMN IF NOT EXISTS customer_signed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS converge_signed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS signed_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS signed_file        TEXT;

ALTER TABLE contract_signatures DROP CONSTRAINT IF EXISTS contract_signatures_status;
ALTER TABLE contract_signatures ADD CONSTRAINT contract_signatures_status
    CHECK (status IN ('sending', 'sent', 'signed'));
