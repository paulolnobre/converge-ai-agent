-- The panel's agenda: a mirror of the team's calendars, so a meeting booked by
-- hand in Google shows next to the bot's appointments. Display only: nothing
-- here feeds reminders, the funnel or the bot. Title, attendees and link are
-- sealed fields from the start (old columns locked empty). The mirror is
-- regenerable: going back to an older image means emptying these three tables
-- (docs/CRIPTOGRAFIA.md). Safe to repeat.
CREATE TABLE IF NOT EXISTS calendar_events (
  id                   BIGSERIAL PRIMARY KEY,
  calendar_id          VARCHAR(255) NOT NULL,
  provider             VARCHAR(12) NOT NULL CHECK (provider IN ('google', 'microsoft')),
  provider_event_id    VARCHAR(255) NOT NULL,
  -- The same meeting across the calendars it was sent to: iCalUID plus the
  -- occurrence's original start, or the calendar and event id without one.
  event_key            VARCHAR(600) NOT NULL,
  staff_id             INTEGER REFERENCES staff(id) ON DELETE SET NULL,
  starts_at            TIMESTAMPTZ NOT NULL,
  ends_at              TIMESTAMPTZ NOT NULL,
  all_day              BOOLEAN NOT NULL DEFAULT FALSE,
  status               VARCHAR(10) NOT NULL CHECK (status IN ('confirmed', 'tentative')),
  organizer_is_self    BOOLEAN NOT NULL DEFAULT FALSE,
  etag                 VARCHAR(255),
  title                TEXT,
  title_sealed         BYTEA,
  attendees            TEXT,
  attendees_sealed     BYTEA,
  meeting_url          TEXT,
  meeting_url_sealed   BYTEA,
  suggested_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
  synced_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (calendar_id, provider_event_id),
  CONSTRAINT calendar_events_title_sealed_only CHECK (title IS NULL),
  CONSTRAINT calendar_events_attendees_sealed_only CHECK (attendees IS NULL),
  CONSTRAINT calendar_events_meeting_url_sealed_only CHECK (meeting_url IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_starts ON calendar_events (starts_at);
CREATE INDEX IF NOT EXISTS idx_calendar_events_key ON calendar_events (event_key);

-- "Não é cliente": every copy of that meeting, including copies seen later.
CREATE TABLE IF NOT EXISTS calendar_ignores (
  event_key      VARCHAR(600) PRIMARY KEY,
  panel_user_id  INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per calendar: when it was last read, and whether that went well.
CREATE TABLE IF NOT EXISTS calendar_sync_state (
  calendar_id               VARCHAR(255) PRIMARY KEY,
  last_attempt_at           TIMESTAMPTZ,
  last_attempt_finished_at  TIMESTAMPTZ,
  last_success_at           TIMESTAMPTZ,
  last_error                VARCHAR(40),
  last_error_at             TIMESTAMPTZ
);
