-- Billing (Asaas) writes to the action audit like the calendar, mail and files
-- do. The table only accepted google/microsoft and calendar/mail/files/sheets,
-- so every audit row of a charge was refused. Safe to repeat.
ALTER TABLE workspace_actions DROP CONSTRAINT IF EXISTS workspace_actions_provider_check;
ALTER TABLE workspace_actions ADD CONSTRAINT workspace_actions_provider_check
    CHECK (provider IN ('google', 'microsoft', 'asaas'));
ALTER TABLE workspace_actions DROP CONSTRAINT IF EXISTS workspace_actions_capability_check;
ALTER TABLE workspace_actions ADD CONSTRAINT workspace_actions_capability_check
    CHECK (capability IN ('calendar', 'mail', 'files', 'sheets', 'billing'));
