-- The e-mail the agent sends through Spacemail goes into the action audit like
-- the Google and Microsoft ones. The table only accepted google, microsoft and
-- asaas as provider. Safe to repeat.
ALTER TABLE workspace_actions DROP CONSTRAINT IF EXISTS workspace_actions_provider_check;
ALTER TABLE workspace_actions ADD CONSTRAINT workspace_actions_provider_check
    CHECK (provider IN ('google', 'microsoft', 'asaas', 'spacemail'));
