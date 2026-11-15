-- The team's test mode: a staff member's own chat marked as a test ('novo' or
-- 'antigo'), shown in the Inbox and funnel with a TESTE tag, left out of every
-- number, and deleted on /sair or after a while without messages. NULL is an
-- ordinary contact. Safe to repeat.
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS test_mode VARCHAR(10)
    CHECK (test_mode IN ('novo', 'antigo')),
  ADD COLUMN IF NOT EXISTS test_started_at TIMESTAMPTZ;
