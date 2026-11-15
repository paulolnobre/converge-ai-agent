-- Encryption of the customer fields by the app (core/crypto.py,
-- core/sealed_fields.py). Each field gets a BYTEA column beside it; the old
-- column stays (additive only: a rollback image still reads it) and is emptied
-- as rows are converted by cli.seal_data, never here. Safe to repeat.
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS name_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS email_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS phone_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS organization_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS notes_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS origin_detail_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS interest_sealed BYTEA,
  -- HMAC of the canonical phone/e-mail: a lookup without the value in clear.
  ADD COLUMN IF NOT EXISTS phone_fp BYTEA,
  ADD COLUMN IF NOT EXISTS email_fp BYTEA;
CREATE INDEX IF NOT EXISTS idx_contacts_phone_fp ON contacts (phone_fp);
CREATE INDEX IF NOT EXISTS idx_contacts_email_fp ON contacts (email_fp);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS message_sealed BYTEA;

ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS title_sealed BYTEA,
  ADD COLUMN IF NOT EXISTS meeting_url_sealed BYTEA,
  -- The test mode finds its own appointments by this, not by a title it can
  -- no longer read in SQL.
  ADD COLUMN IF NOT EXISTS test_mode BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE automation_queue ADD COLUMN IF NOT EXISTS reason_sealed BYTEA;
ALTER TABLE mail_threads ADD COLUMN IF NOT EXISTS subject_sealed BYTEA;
ALTER TABLE mail_drafts ADD COLUMN IF NOT EXISTS body_text_sealed BYTEA;

-- The newest data format that may already be in this database. An image of a
-- lower format must not start on it (deploy/deploy.sh). One row at most.
CREATE TABLE IF NOT EXISTS data_format (
  version SMALLINT NOT NULL CHECK (version >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS data_format_one_row ON data_format ((true));
