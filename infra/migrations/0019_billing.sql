-- Billing through a payment provider (Asaas first): the Converge charging its
-- own clients. The provider is the source of truth; these tables mirror it so
-- the panel and the desk can say "paid" or "overdue" without asking it every
-- time, and so the webhook applies each event once. The tax id (CPF/CNPJ) is a
-- sealed field from the start (old column locked empty). Safe to repeat.
CREATE TABLE IF NOT EXISTS billing_customers (
    id              BIGSERIAL PRIMARY KEY,
    contact_id      INTEGER NOT NULL UNIQUE REFERENCES contacts(id),
    provider        TEXT NOT NULL DEFAULT 'asaas',
    provider_id     TEXT NOT NULL,
    tax_id          TEXT,
    tax_id_sealed BYTEA,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_id),
    CONSTRAINT billing_customers_tax_id_sealed_only CHECK (tax_id IS NULL)
);

CREATE TABLE IF NOT EXISTS billing_subscriptions (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES billing_customers(id),
    provider_id     TEXT UNIQUE,
    value_cents     INTEGER NOT NULL CHECK (value_cents > 0),
    description     TEXT NOT NULL,
    next_due_date   DATE NOT NULL,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS billing_charges (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES billing_customers(id),
    -- Empty while the charge waits for the team's approval (born in a chat).
    provider_id     TEXT UNIQUE,
    subscription_id BIGINT REFERENCES billing_subscriptions(id),
    origin          TEXT NOT NULL CHECK (origin IN ('painel', 'conversa', 'assinatura')),
    method          TEXT NOT NULL CHECK (method IN ('link', 'boleto', 'pix')),
    value_cents     INTEGER NOT NULL CHECK (value_cents > 0),
    description     TEXT NOT NULL,
    due_date        DATE NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('aguardando_aprovacao', 'recusada',
                        'pendente', 'pago', 'vencido', 'cancelado', 'estornado')),
    invoice_url     TEXT,
    requested_by    INTEGER REFERENCES panel_users(id),
    approved_by     INTEGER REFERENCES panel_users(id),
    sent_at         TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_billing_charges_customer ON billing_charges (customer_id);
CREATE INDEX IF NOT EXISTS idx_billing_charges_status ON billing_charges (status);

-- One row per webhook event already applied: a repeated delivery is ignored.
CREATE TABLE IF NOT EXISTS billing_events (
    event_id        TEXT PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
