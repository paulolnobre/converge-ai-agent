-- The BASELINE schema. Postgres runs this once, when the data volume is first
-- created. Do not edit it to change an existing database: add a numbered file
-- to infra/migrations/ instead, which cli/migrate.py applies exactly once on
-- every deploy (see its docstring for why this file is not simply re-run).

-- Reception and secretarial assistant schema.
--
-- The calendar, the mailbox and the drive belong to Google Workspace or
-- Microsoft 365 — this database never becomes a second copy of them. What it
-- holds is what the suites cannot: who the agent is talking to, what it decided
-- and why, and which of its actions a human still has to approve.

-- ── People ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contacts (
    id            SERIAL PRIMARY KEY,
    chat_id       VARCHAR(20) UNIQUE,
    name          VARCHAR(160),
    email         VARCHAR(160),
    phone         VARCHAR(30),
    organization  VARCHAR(160),
    -- Free notes the agent may read back ("prefers afternoons", "deaf, please
    -- write"). Never a place for special-category personal data.
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts (lower(email));

CREATE TABLE IF NOT EXISTS staff (
    id           SERIAL PRIMARY KEY,
    chat_id      VARCHAR(20) UNIQUE,
    name         VARCHAR(120) NOT NULL,
    -- Unique because it is what the organization's own staff file is keyed
    -- on: re-running the seed must update a person, never duplicate them.
    email        VARCHAR(160) UNIQUE,
    role         VARCHAR(20) NOT NULL DEFAULT 'staff'
                 CHECK (role IN ('staff', 'manager', 'admin')),
    -- The calendar this person is booked in: a Google calendar id, or a
    -- Microsoft 365 UPN. One column, because the agent only ever needs one.
    calendar_id  VARCHAR(255),
    -- What this person is booked for. Routing by specialty is roadmap 2.5; the
    -- column exists now so the organization's own staff file loads whole rather
    -- than losing the half the schema had no room for.
    specialties  TEXT[] NOT NULL DEFAULT '{}',
    -- Who picks this up when they cannot. Self-referencing, and nullable at the
    -- top: somebody has to be the end of the chain.
    escalates_to INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_staff_specialties ON staff USING GIN (specialties);

-- ── Phase 1: the conversation ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    chat_id     VARCHAR(20) NOT NULL,
    role        VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
    message     TEXT NOT NULL,
    intent      VARCHAR(30),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions (chat_id, created_at DESC);

-- What the desk is allowed to say about the organization.
--
-- This is the one table that is a copy of something in the drive, and it earns
-- the exception: the FAQ is read on every question, and a Drive round-trip per
-- message would put the client's rate limit between a patient and an answer.
-- The content hash is what keeps the copy honest — a chunk whose text has not
-- changed is never re-embedded, and a document that has changed replaces its
-- chunks wholesale.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id            BIGSERIAL PRIMARY KEY,
    source        VARCHAR(255) NOT NULL,   -- the file id or path it came from
    heading       TEXT NOT NULL,           -- the question, as the FAQ asks it
    body          TEXT NOT NULL,           -- the answer, verbatim
    -- The embedding, as a plain array. No pgvector: this corpus is an FAQ, not a
    -- corpus, and a cosine over a few hundred rows in Python costs less than an
    -- extension the client's managed Postgres may not offer.
    embedding     DOUBLE PRECISION[] NOT NULL,
    content_hash  CHAR(64) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, heading)
);

-- ── Phase 2: appointments ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS appointments (
    id                 BIGSERIAL PRIMARY KEY,
    contact_id         INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    staff_id           INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    calendar_id        VARCHAR(255) NOT NULL,
    -- The event id in Google Calendar or Graph. Unique so a retried booking
    -- links to the existing row instead of double-booking the same slot.
    provider           VARCHAR(12) NOT NULL CHECK (provider IN ('google', 'microsoft')),
    provider_event_id  VARCHAR(255) UNIQUE,
    title              VARCHAR(200) NOT NULL,
    starts_at          TIMESTAMPTZ NOT NULL,
    ends_at            TIMESTAMPTZ NOT NULL,
    status             VARCHAR(12) NOT NULL DEFAULT 'confirmed'
                       CHECK (status IN ('confirmed', 'cancelled', 'no_show', 'done')),
    booked_via         VARCHAR(12) NOT NULL DEFAULT 'telegram',
    reminder_sent_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_appointments_upcoming
    ON appointments (starts_at) WHERE status = 'confirmed';
CREATE INDEX IF NOT EXISTS idx_appointments_reminder
    ON appointments (starts_at) WHERE status = 'confirmed' AND reminder_sent_at IS NULL;

-- One confirmed appointment per calendar slot. The calendar is re-read at the
-- moment of booking, but two people answering "a terca" seconds apart both read
-- it as free; this index is what makes the second one lose, rather than both
-- winning and one of them arriving to a room that is taken.
CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_slot
    ON appointments (calendar_id, starts_at) WHERE status = 'confirmed';

-- The times the agent put in front of someone, waiting for them to pick one.
-- Without this row the offer is lost the moment it is sent: the next message
-- ("a terca") arrives with nothing to match against, and the desk offers the
-- same three times again, and again.
CREATE TABLE IF NOT EXISTS offers (
    id           BIGSERIAL PRIMARY KEY,
    contact_id   INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    chat_id      VARCHAR(20) NOT NULL,
    -- The offered times, exactly as they were said out loud. Kept here rather
    -- than recomputed, because a slot that has since been taken must still be
    -- recognisable when the person picks it — the answer is "that one just
    -- went", not "I never offered that".
    slots        JSONB NOT NULL,
    calendar_id  VARCHAR(255) NOT NULL,
    staff_id     INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    -- Set when the offer is a reschedule: taking the new time cancels this one.
    replaces_id  BIGINT REFERENCES appointments(id) ON DELETE SET NULL,
    status       VARCHAR(12) NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'taken', 'superseded', 'expired')),
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One live offer per chat. A second offer supersedes the first instead of
-- competing with it, so "a terca" is never ambiguous between two sets of times.
CREATE UNIQUE INDEX IF NOT EXISTS idx_offers_pending_chat
    ON offers (chat_id) WHERE status = 'pending';

-- ── Phase 3: the mailbox ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mail_threads (
    id                  BIGSERIAL PRIMARY KEY,
    mailbox             VARCHAR(160) NOT NULL,
    provider            VARCHAR(12) NOT NULL CHECK (provider IN ('google', 'microsoft')),
    provider_thread_id  VARCHAR(255) NOT NULL,
    subject             VARCHAR(400),
    contact_id          INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    category            VARCHAR(40),
    priority            SMALLINT NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status              VARCHAR(12) NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'triaged', 'answered', 'escalated', 'ignored')),
    last_message_at     TIMESTAMPTZ,
    triaged_at          TIMESTAMPTZ,
    UNIQUE (mailbox, provider_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_threads_open
    ON mail_threads (priority, last_message_at DESC) WHERE status IN ('new', 'triaged');

CREATE TABLE IF NOT EXISTS mail_drafts (
    id                   BIGSERIAL PRIMARY KEY,
    thread_id            BIGINT NOT NULL REFERENCES mail_threads(id) ON DELETE CASCADE,
    provider_message_id  VARCHAR(255),   -- the message being replied to
    body_text            TEXT NOT NULL,
    -- A reply the agent wrote is a draft until a person approves it, unless
    -- AUTOSEND_REPLIES was deliberately turned on. The status column is what
    -- makes that switch auditable after the fact.
    status               VARCHAR(10) NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'approved', 'sent', 'discarded')),
    approved_by          INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    approved_at          TIMESTAMPTZ,
    sent_at              TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Phase 4: secretarial follow-through ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    id          BIGSERIAL PRIMARY KEY,
    title       VARCHAR(300) NOT NULL,
    detail      TEXT,
    due_on      DATE,
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    thread_id   BIGINT REFERENCES mail_threads(id) ON DELETE SET NULL,
    assigned_to INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    status      VARCHAR(12) NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'done', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (due_on) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS meeting_notes (
    id               BIGSERIAL PRIMARY KEY,
    appointment_id   BIGINT REFERENCES appointments(id) ON DELETE SET NULL,
    summary          TEXT NOT NULL,
    decisions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    action_items     JSONB NOT NULL DEFAULT '[]'::jsonb,
    document_file_id VARCHAR(255),        -- the minutes written into the drive
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Audit ───────────────────────────────────────────────────────────────────

-- Every write the agent makes into someone else's calendar, mailbox or drive.
-- It acts with the organization's own credentials, so without this row there is
-- no way to tell an agent action from a person's.
CREATE TABLE IF NOT EXISTS workspace_actions (
    id          BIGSERIAL PRIMARY KEY,
    provider    VARCHAR(12) NOT NULL CHECK (provider IN ('google', 'microsoft')),
    capability  VARCHAR(12) NOT NULL
                CHECK (capability IN ('calendar', 'mail', 'files', 'sheets')),
    action      VARCHAR(40) NOT NULL,
    target      VARCHAR(255),
    contact_id  INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    staff_id    INTEGER REFERENCES staff(id) ON DELETE SET NULL,
    succeeded   BOOLEAN NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workspace_actions_created
    ON workspace_actions (created_at DESC);

-- ── The funnel ──────────────────────────────────────────────────────────────
-- Where each person stands commercially, read by the /painel dashboard.
--
-- Additive and idempotent on purpose: init.sql only runs by itself on an empty
-- database, so an existing one is brought up to date by running this file
-- again, and every statement here must be safe to repeat.
--
-- The bot moves a contact forward only through the stages it can observe
-- (novo → respondido → agendado, and back to respondido on a cancellation).
-- proposta, fechado and perdido are a person's call, made in the panel, and the
-- bot never overwrites them — see core/services/funnel.py.

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS stage VARCHAR(12) NOT NULL DEFAULT 'novo'
    CHECK (stage IN ('novo', 'respondido', 'agendado', 'proposta', 'fechado', 'perdido'));
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS stage_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
-- The channel the person first arrived through. Telegram is the only one today
-- (see docs/ROADMAP.md, phase 5).
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'telegram';
-- Which of the organization's offerings this is about. Free text, set in the
-- panel: the names belong to the organization, not to the schema.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS interest VARCHAR(80);
-- The staff member following this lead up, if anyone has taken it.
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS owner_staff_id INTEGER
    REFERENCES staff(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_contacts_stage ON contacts (stage);
CREATE INDEX IF NOT EXISTS idx_sessions_contact ON sessions (contact_id, created_at DESC);

-- Contacts that predate the funnel start as 'novo'. Bring them to where their
-- history already puts them — forward only, so re-running this never undoes a
-- stage the bot or a person has set since.
UPDATE contacts c SET stage = 'respondido', stage_changed_at = NOW()
 WHERE c.stage = 'novo'
   AND EXISTS (SELECT 1 FROM sessions s WHERE s.contact_id = c.id AND s.role = 'assistant');
UPDATE contacts c SET stage = 'agendado', stage_changed_at = NOW()
 WHERE c.stage IN ('novo', 'respondido')
   AND EXISTS (SELECT 1 FROM appointments a WHERE a.contact_id = c.id
                AND a.status = 'confirmed' AND a.starts_at > NOW());

-- ── Handing a conversation to a person ──────────────────────────────────────
-- A handoff posts a card in the team's Telegram group. Whoever replies to that
-- card speaks to the person through the bot, so the card's message id is the
-- only link between a reply in the group and the chat it belongs to.
CREATE TABLE IF NOT EXISTS handoffs (
    id               BIGSERIAL PRIMARY KEY,
    contact_id       INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    chat_id          VARCHAR(20) NOT NULL,
    team_chat_id     VARCHAR(30) NOT NULL,
    team_message_id  BIGINT NOT NULL,
    reason           VARCHAR(20) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (team_chat_id, team_message_id)
);

-- ── Handing a conversation to a person, and keeping it with them ───────────
-- Supersedes the earlier one-card-per-escalation design: a session persists
-- across every message from the first card until /encerrar or the customer's
-- own /bot, instead of a new row per single escalation.
DROP TABLE IF EXISTS handoffs;

CREATE TABLE IF NOT EXISTS handoff_sessions (
    id               BIGSERIAL PRIMARY KEY,
    contact_id       INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    chat_id          VARCHAR(20) NOT NULL,
    team_chat_id     VARCHAR(30) NOT NULL,
    reason           VARCHAR(20) NOT NULL,
    status           VARCHAR(10) NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'claimed', 'closed')),
    -- The Telegram user id of whoever claimed it — not a staff.id: anyone in
    -- the team group may answer, not only people in the staff table.
    claimed_by_id    BIGINT,
    claimed_by_name  VARCHAR(120),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at       TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ
);
-- One live session per customer: a second escalation while one is already
-- open or claimed joins it instead of racing a second card into existence.
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoff_sessions_active
    ON handoff_sessions (chat_id) WHERE status IN ('open', 'claimed');

-- Every message the bot posts into the team group for a session — the card,
-- then each forwarded customer message — so a reply to any of them, not just
-- the first, resolves back to the session.
CREATE TABLE IF NOT EXISTS handoff_messages (
    team_chat_id  VARCHAR(30) NOT NULL,
    message_id    BIGINT NOT NULL,
    session_id    BIGINT NOT NULL REFERENCES handoff_sessions(id) ON DELETE CASCADE,
    PRIMARY KEY (team_chat_id, message_id)
);

-- ── Two more things a handoff session needs ─────────────────────────────────
-- relay_chat_id: where the customer's messages currently get forwarded — the
-- team group at first, switched to the claimer's own private chat once a test
-- message there succeeds (see core/services/handoff.py::_switch_to_dm).
-- last_activity_at: the last message on either side, read by the inactivity
-- sweep below.
ALTER TABLE handoff_sessions ADD COLUMN IF NOT EXISTS relay_chat_id VARCHAR(30);
ALTER TABLE handoff_sessions ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
UPDATE handoff_sessions SET relay_chat_id = team_chat_id WHERE relay_chat_id IS NULL;

-- ── After the booking: confirmed by the person, and did it happen ───────────
-- attendance_confirmed_at: the person tapped "Confirmo" on the reminder.
-- outcome_asked_at: the team was asked, once, whether the meeting happened —
-- claimed before posting, like reminder_sent_at, so a restart asks once.
-- outcome_by: who in the team answered. A no-show is a person's call, never
-- the bot's: it cannot tell a missed meeting from one held on another link.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS attendance_confirmed_at TIMESTAMPTZ;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS outcome_asked_at TIMESTAMPTZ;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS outcome_by VARCHAR(120);

-- ── The morning briefing ────────────────────────────────────────────────────
-- One row per day it went out. Inserting the day is the claim, so a restart
-- inside the briefing window does not post it twice; the previous row's
-- sent_at is where "since the last briefing" starts.
CREATE TABLE IF NOT EXISTS briefings (
    day      DATE PRIMARY KEY,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
