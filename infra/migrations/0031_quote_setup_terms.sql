-- How the setup is paid, chosen by the team on the card (spec
-- venda-ate-pagamento, increment 5): "metade", half when the contract is
-- signed and half when the setup is accepted; or "parcelado", equal monthly
-- installments, the first when the contract is signed. Quotes made before
-- keep the installments their proposals and contracts already showed; new
-- ones start half and half.
-- Safe to repeat.
ALTER TABLE proposal_quotes ADD COLUMN IF NOT EXISTS forma_implantacao TEXT NOT NULL DEFAULT 'parcelado';
ALTER TABLE proposal_quotes ALTER COLUMN forma_implantacao SET DEFAULT 'metade';
ALTER TABLE proposal_quotes DROP CONSTRAINT IF EXISTS proposal_quotes_forma_implantacao;
ALTER TABLE proposal_quotes ADD CONSTRAINT proposal_quotes_forma_implantacao
    CHECK (forma_implantacao IN ('metade', 'parcelado'));
