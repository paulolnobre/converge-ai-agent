-- Appointments follow what changes in the calendar (agenda no painel,
-- increment 2): the provider's version of the event, the first pass that did
-- not find it (two are needed to call it cancelled), a problem that suspends
-- its reminder, and when the sync last moved it (buttons sent before a move no
-- longer act). Offers remember the time of the meeting they would replace.
-- Safe to repeat.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS provider_etag VARCHAR(255);
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS provider_missing_at TIMESTAMPTZ;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS sync_problem VARCHAR(20);
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS moved_at TIMESTAMPTZ;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_sync_problem_check') THEN
    ALTER TABLE appointments ADD CONSTRAINT appointments_sync_problem_check
      CHECK (sync_problem IN ('all_day', 'not_organizer', 'start_collision'));
  END IF;
END $$;
ALTER TABLE offers ADD COLUMN IF NOT EXISTS replaces_starts_at TIMESTAMPTZ;
