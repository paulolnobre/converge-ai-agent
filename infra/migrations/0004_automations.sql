-- Automations: messages the desk starts itself, to people who went quiet.
-- Each one is off until someone turns it on in the panel, and starts in
-- "approve" mode: it builds a list, a person approves the sends.

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS automation_opt_out BOOLEAN NOT NULL DEFAULT FALSE;
-- The last automated message this person got, from any automation: the cap of
-- one every 30 days is read from here.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_automation_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS automations (
    id          SERIAL PRIMARY KEY,
    kind        VARCHAR(20) NOT NULL UNIQUE
                CHECK (kind IN ('reactivate', 'stalled', 'no_show', 'no_proposal')),
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    mode        VARCHAR(10) NOT NULL DEFAULT 'approve' CHECK (mode IN ('approve', 'auto')),
    days        INTEGER NOT NULL CHECK (days BETWEEN 1 AND 365),
    -- What the person receives; {nome} is their first name. Empty for
    -- no_proposal, which only ever tells the team.
    message     TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  VARCHAR(160)
);

INSERT INTO automations (kind, days, message) VALUES
  ('reactivate', 60,
   'Olá, {nome}! Aqui é o atendimento da Converge. Faz um tempo que não conversamos. Se quiser retomar, posso tirar dúvidas sobre as nossas soluções ou marcar uma conversa com a equipe.'),
  ('stalled', 3,
   'Olá, {nome}! Vi que a nossa conversa parou no meio. Aconteceu alguma coisa? Se puder, me conta o motivo, ajuda a gente a melhorar:'),
  ('no_show', 1,
   'Olá, {nome}! Sentimos a sua falta na conversa com a equipe. Quer marcar um novo horário?'),
  ('no_proposal', 3, '')
ON CONFLICT (kind) DO NOTHING;

CREATE TABLE IF NOT EXISTS automation_queue (
    id              BIGSERIAL PRIMARY KEY,
    automation_id   INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    contact_id      INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    appointment_id  BIGINT REFERENCES appointments(id) ON DELETE SET NULL,
    status          VARCHAR(12) NOT NULL
                    CHECK (status IN ('pending', 'approved', 'sent', 'discarded', 'failed')),
    -- Why this person was picked, as the panel shows it.
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ,
    decided_by      VARCHAR(160),
    sent_at         TIMESTAMPTZ,
    replied_at      TIMESTAMPTZ,
    -- The button tapped on "por que parou?": preco, avaliando, momento, outro.
    reply_reason    VARCHAR(20)
);
-- One live entry per person per automation: a list rebuilt every few minutes
-- must not queue the same person twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_queue_live
    ON automation_queue (automation_id, contact_id) WHERE status IN ('pending', 'approved');
-- And once per appointment, for the ones about a meeting.
CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_queue_appointment
    ON automation_queue (automation_id, appointment_id) WHERE appointment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_automation_queue_contact ON automation_queue (contact_id, sent_at);
