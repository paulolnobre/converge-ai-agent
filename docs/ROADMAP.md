# Development roadmap

Four phases to a pilot. Each step names what it changes, and what has to be true
before it counts as done.

Ordering principle throughout: **the agent earns each permission.** It reads
before it writes, drafts before it sends, and offers before it books. Every step
that lets it act without a person is a step someone chooses, not one it arrives
with.

What already exists in the repository is marked ✅. Everything else is the work.

---

## Phase 0 — foundation (done)

| | |
|---|---|
| ✅ | Config with startup validation: a model pointed at the wrong provider, a production deploy without a webhook secret, an office open on no day of the week — all refused before the first request |
| ✅ | FastAPI webhook, `/health`, `/ready`, `/metrics`; durable Redis queue with dedupe, retry and dead-letter; worker with per-update timeout |
| ✅ | Workspace layer: one interface, Google and Microsoft implementations, three OAuth grants, per-capability provider override |
| ✅ | Free/busy → offerable slots, with office hours, working days, a notice window and a fixed grid |
| ✅ | Schema for all four phases; `workspace_actions` audits every outbound write |
| ✅ | Tests (441 today), ruff and pyright green in CI on every branch and PR (`ci.yml`), image build gated on them |
| ✅ | `/painel` sign-in: one account per person, Argon2id password + authenticator code (TOTP, secret encrypted at rest with `PANEL_SECRET_KEY`, a code accepted once), recovery codes, invite links (`cli/panel_users.py`), HttpOnly/Secure/SameSite=Lax sessions (a link from Telegram opens signed in; every change is origin-checked) with idle and absolute expiry, lockout after 5 failures, CSP and a sign-in log (`panel_auth_events`) |
| ✅ | CD on main (`ci-cd.yml`): the same CI, then the image to GHCR tagged by commit, then the VPS — migrations applied once each (`cli/migrate.py`), health-checked, rolled back to the previous image if it never turns healthy (`deploy/`) |
| ⏳ | Backups ([docs/BACKUP.md](BACKUP.md)) with restic: daily and before every migration, `pg_dump` streamed into an encrypted snapshot plus the server-only files, kept 30 days in S3 São Paulo — versioned, under Object Lock, with a key that cannot destroy a backup — and watched by healthchecks.io. Built and rehearsed end to end, including recovery from a deleted repository; live once the bucket and the timer are set up and one restore from the bucket has passed |

**Done means:** `docker compose up` answers `/ready`, and
`python cli/smoke_workspace.py` reads the real calendar, the real mailbox and the
real drive of the client's tenant.

---

## Phase 1 — the front desk

The phase that has to feel like a person. Everything after it is leverage.

### 1.1 Triage ✅ → answers from the organization ✅ (built) → calibration
Triage works: intent, the person asked for, a preferred day, a confidence the
agent refuses to act below.

The knowledge is now built. `content/faq.md` — or the same document in the
client's own drive, which wins — is chunked one question per `##` heading,
embedded, and stored in `knowledge_chunks`. The answering node is given the
retrieved chunks and told that anything outside them does not exist. Retrieve
nothing and it does not call the model at all: it says `reply.unknown` and hands
the question to a person, because a model given an empty context does not
decline, it invents.

The organization's voice and its four prohibitions — no diagnosis, no promised
walk-in, no closed price on an invasive procedure, no unexplained jargon — live
in `content/messages.toml` and are read into the answering prompt.

**Calibrated, on a written set:** `python -m cli.calibrate_knowledge` runs
`content/calibration.toml` — 25 questions the FAQ covers, phrased the way
people type, and 7 it does not — through the real embedding and, with
`--answers`, the real triage and answer.

What it showed: similarity cannot be the filter. Covered questions score 0.33
to 0.70, uncovered ones 0.32 to 0.46 — the ranges overlap, so any floor high
enough to stop "vocês integram com SAP?" (0.46) also drops "o que vcs
fazem?" (0.33). The floor stays at 0.25, a guard against noise only; what
keeps the desk honest is the answering model's refusal when the retrieved
text does not say it, and on this set none of the uncovered questions got an
answer. The run also found two content problems, both fixed in the FAQ, not
the code: "vcs usam chatgpt?" could not find the technology entry (the FAQ
never used the words people type), and an entry opening with a bare "Não."
made the desk answer "minha equipe consegue usar?" with "Não".

**Still open:** the set is written, not real. Replace it with real questions
as they arrive and re-run — the bar below is about real ones.

**Done means:** on 20 real questions, it never states a fact that is not in the
FAQ document.

### 1.2 Voice ✅
A caller can dictate. The audio is transcribed and never stored, so a spoken
message leaves the trace a typed one would.

### 1.3 Taking a message and escalating ✅
An escalation — asked for, or offered as a button under `reply.unknown` —
opens a session and posts one line in the team group (`TEAM_CHAT_ID`) with a
button to the conversation in the panel's **Inbox**. The group never carries
the conversation itself: a new conversation (the first message, or the first
in 24h) is one line, a request for a person is one line, and so is who took
it. Anyone in the team takes a conversation in the Inbox — one someone asked
a person for, or one the bot is having — and answers there; only whoever
holds it can send, and the bot stays quiet meanwhile. Whoever holds it gets a
private Telegram notice when the customer writes (never the words, at most
every 5 minutes). "Devolver ao bot", the customer's `/bot`, or
`HANDOFF_TIMEOUT_MINUTES` of silence hand it back, with both sides told
(`core/services/handoff.py`, `core/services/inbox.py`). Without a team
group, no person is promised.

Each contact has a channel (`contacts.source`: Telegram today) and an origin
(ad on Instagram or Facebook, site, Google, referral, other) — from a
`t.me/<bot>?start=<origin>` link, or set in the panel.

**Done means:** an escalated message reaches a human within a minute, and the
sender knows it did.

### 1.4 Contact details, asked for once ✅
E-mail and phone are asked for *before* a booking or a handoff to a person —
by request or by the team button — and only if missing; never for an FAQ
question. What the person wanted waits in `contacts.pending_action`, and the
message with the details picks it up: the times (or the period question
first), or the team card, which then carries the details. Asked once
(`details_requested_at`): whoever would rather not give them changes the
subject, that message is answered on its own terms, and the next booking just
books. They are read by pattern in `core/services/contact_details.py`, never by
the model: a phone number is not something to round, and a date, a time, a
CPF or a price must never become one. Details sent unprompted get a thank-you
without a model call. The name a person gives is never overwritten by a
Telegram display name.

### 1.5 After hours ✅ → the answer that fits
Out-of-hours messages are deferred and released when the desk opens. Remaining:
make the out-of-hours reply the organization's own words, and hold the promise —
what arrived overnight is answered first thing.

**Acceptance for the phase:** a week of real messages where nobody at the
organization has to answer a question the bot should have answered, and nothing
it said was wrong.

---

## Phase 2 — the diary

### 2.1 Offering times ✅
Free/busy across every calendar in scope, office hours and working days
respected, a notice window so nothing is offered for twenty minutes from now, and
a fixed grid so nobody is offered 14:07.

### 2.2 Taking the booking ✅
The turn after the offer. The offered times are written down (`offers`), so the
reply that picks one has something to attach to — without that row "a terca" is
re-triaged as a fresh request and the desk offers the same three times again.
`slot_choice` reads the reply against the times actually offered, and refuses to
decide anything it cannot read cleanly.

Three guards, in order: the slot is re-checked against the calendar at the
moment of booking, the calendar write happens before the database row, and a
partial unique index on `(calendar_id, starts_at)` decides the race the calendar
read cannot — the booking that loses has its event withdrawn again.

**Done means:** two people racing for the last slot produce one appointment and
one apology, never two appointments. ✅

### 2.3 Rescheduling and cancelling ✅
Cancel works, calendar first. Reschedule books the replacement before releasing
the original, so a failure leaves the person with the time they already had
rather than with neither.

**Done means:** a failed reschedule leaves the original appointment intact. ✅

### 2.4 Reminders ✅ → confirmation ✅
The sweep sends one reminder per appointment and claims it before sending, so a
restart cannot double it. The reminder carries three buttons — confirm,
reschedule, cancel — and cancelling asks once more. Reschedule runs the same
path as a typed request: the old time is released only once the new one is
booked. The team group hears about every confirmation and cancellation.

A no-show is a person's call, not the bot's: once a meeting's time has passed,
the team group is asked, once, whether it happened, and the first tap records
`done` or `no_show` along with who answered (`core/services/attendance.py`).

### 2.5 Who to book with
`person_asked_for` is already extracted. Route to that staff member's calendar,
and where nobody was named, to whoever is free first.

**Acceptance for the phase:** a month where every appointment in the calendar was
booked by the bot, and no staff member was double-booked.

---

## Phase 3 — the inbox and the drive

### 3.1 Triage ✅ (spine) → the poller
Urgency 1–5, and a cautious default: anything about money, a deadline or a
complaint goes to a human. The categories are the organization's own, from
`content/email_templates.toml` — given a list a model classifies into it, given
none it invents one, and a queue whose categories change per message is a queue
nobody can work. `cli/triage_inbox.py` runs it once by hand.
Remaining: run it on `INBOX_POLL_MINUTES`, and never re-triage a thread already
handled.

**Done means:** a week of real mail triaged, and every genuinely urgent message
was in the top of the queue.

### 3.2 Drafting
A reply written in the organization's voice, from the FAQ corpus, saved as a
draft. A person approves it in one message and it sends — the approval is claimed
atomically, so two people approving send one e-mail.

**Done means:** half the inbox is answered by approving a draft rather than
writing one.

### 3.3 Autosend, deliberately
Only after 3.2 has run long enough for someone to say "these are all fine".
Turned on per category, never globally: an appointment confirmation is not a
contract question.

**Done means:** the categories it sends on are named in `.env`, and the audit
table shows who decided that.

### 3.4 Documents out ✅ (the rule) → the flow
The shareable-folder rule is implemented and tested. Remaining: the conversation
around it — finding the file the person means, confirming it is the right one,
and asking a human whenever it sits anywhere else.

### 3.5 Documents in
A file someone sends lands in the drive under the contact it came from, and the
team is told it arrived.

**Acceptance for the phase:** the shared inbox is worked to zero every day, and
nobody searches the drive by hand.

---

## Phase 4 — the secretary

### 4.1 Minutes
From a recording or a dictated summary: decisions and action items, separated,
written into the client's own drive and linked to the appointment.

**Done means:** the action items become tasks, and the tasks are what phase 4.3
chases.

### 4.2 The morning briefing ✅
One message in the team group on office workdays, from `DAILY_BRIEFING_HOUR`
for three hours (a service down all morning says nothing rather than a late
"bom dia"): today's appointments and who confirmed, new contacts and messages
since the last briefing, open handoffs, meetings with no recorded outcome, and
proposals idle past `FOLLOWUP_AFTER_DAYS`. Read from the database, never
written by a model. The `briefings` row claims the day, so a restart sends it
once; `/resumo` in the team group posts it on demand
(`core/services/secretarial.py`). The team's own chats are left out by
`staff.chat_id`.

**Done means:** the person who used to compile it stops.

### 4.3 Follow-ups ✅ (conversations) → tasks
Threads the organization answered that nobody replied to, past
`FOLLOWUP_AFTER_DAYS`, and tasks past their due date. Surfaced, not chased
automatically — a follow-up sent by a bot to a client who is simply thinking is
worse than none.

Conversations are done: the morning briefing lists open leads (respondido,
proposta) where our message was the last one, 3 to 30 days ago — past that it
is a decision ("perdido") for a person in the panel, not a nudge. Anyone with a
meeting ahead, a live handoff or a staff chat is left out, and a proposal
already listed as idle is not named twice. Remaining: tasks past due, once
something writes to `tasks` (phase 4.1's action items).

### 4.5 Automations ✅ (Telegram)
The panel's Automações tab: reactivate who went quiet (60 days), ask why
someone stopped mid-funnel (reason buttons, counted in the tab), offer a new
time after a no-show, and tell the team when a meeting did not become a
proposal. Each starts off and in "approve" mode — the automation builds its
list, a person approves in the panel — and "auto" is granted per rule. Sends
happen in office hours only; one automated message per person every 30 days,
one per automation per silence; never to the team, to someone a person is
already talking to, or to someone with a meeting ahead. Every message offers
SAIR, read by code (`core/services/automations.py`).

**For WhatsApp:** a message the business starts more than 24h after the
person's last one needs a Meta-approved template and is paid per message —
each automation will map to one template when the channel exists.

### 4.4 Reporting
Volume by channel and category, response times, no-show rate, which questions
come up often enough to belong in the FAQ. Written into a spreadsheet in their
own drive, so it outlives the deployment.

**Acceptance for the phase:** the organization can say what its front desk did
last month without anyone counting anything.

---

## Phase 5 — other channels

Telegram is the whole surface today, and not by abstraction — by shortcut.
`chat_id VARCHAR(20)` is the identity column on `contacts`, `staff`, `sessions`
and `offers`; `core/integrations/telegram.py` is called directly from the
webhook, the handlers and the reminder worker; the webhook in `core/web/app.py`
verifies the secret the way only Telegram sends it. None of that is a channel
choice baked into the product — it is what "build one channel first" leaves
behind, and phase 5 is paying it back before it blocks a second one.

The triage → FAQ-answer → offer → book pipeline in `core/services/graph.py` and
`core/services/scheduling.py` does not know Telegram exists — it takes a
`chat_id` and text. That is the part that does not need to change.

### 5.1 `ChannelPort`
The same move already made for the workspace suite in
`core/integrations/workspace/base.py`: a `Protocol` (`send_text`,
`send_typing`, `download_media`) with one implementation per channel, chosen in
configuration rather than imported by name. `telegram.py` becomes the first
implementation, close to unchanged — this step is a lift-and-shift, not a
rewrite, and changes no behavior on its own.

### 5.2 Identity beyond `chat_id`
`contacts`, `staff`, `sessions` and `offers` move from a bare `chat_id` to
`(channel, external_id)`, unique on the pair. `VARCHAR(20)` only ever fit a
Telegram numeric id — an e-mail address and a Meta-scoped user id do not.

**Done means:** the same person messaging on two channels is two rows, not a
collision or a silent overwrite — full identity merge across channels is a
later, harder problem than this phase solves.

### 5.3 WhatsApp Business Cloud API
The channel to build the abstraction against for real, not the second one to
assume it fits. Same push-webhook shape as Telegram, official and stable, and
it reuses the audio transcription this agent already has. It is also what the
organization already runs, in Converge CRM — the number and the credibility are
already there.

**Done means:** `cli/smoke_workspace.py`'s WhatsApp equivalent sends and
receives a real message, and a voice note transcribes the same way it does on
Telegram today.

The Inbox's `send()` in `core/services/inbox.py` is where the channel plugs in
for replies; a Meta ad's `referral` fills the contact's origin.

### 5.4 Instagram Direct
Runs on the same Meta App, Graph API and webhook infrastructure as 5.3 once
that exists — mostly a second payload shape to normalize into the same
`ChannelPort`, not a second integration from scratch. Cheap only because 5.3
came first.

### 5.5 E-mail — a different shape, not just another channel
The mailbox integration already exists, but for the organization's *own*
inbox, in the phase 3 model: triage → draft → a person approves → send,
deliberately asynchronous and supervised (`AUTOSEND_REPLIES=false`). Wiring
e-mail into this phase's live, instant-reply loop means deciding — per
category, the way 3.3 already frames it — whether some replies autosend or
every e-mail lead waits for a person. That decision is explicit content, not
an accident of reusing the same code path as WhatsApp.

### 5.6 The organization's own site — deliberately not scheduled
No push webhook to build against: a chat widget and a transport (WebSocket or
polling) would be new engineering, not "add a channel" like 5.3–5.5 are. Revisit
once 5.3–5.5 are live and there is a real request for it.

**Acceptance for the phase:** a person messaging on WhatsApp or Instagram gets
the same desk — the same FAQ answers, the same offered times, the same
guardrails — that Telegram gives today, and adding the channel after that
changed no behavior for the channels already live.

---

## Ideas for later

### The panel's dashboards, made to measure

Today the Painel shows a fixed set of tiles, the funnel by stage, recent
conversations and where contacts come from. Later: let each organization —
or each person — choose what the Painel shows and how (which numbers, which
periods, which filters, the order of the cards), saved per account. Not
scheduled: it waits for the observability work (the Saúde tab and the
Prometheus/Grafana/Loki stack) and for real requests from people using the
panel.

### Closing a sale without the team

The owner's goal: when a customer is ready, the desk closes the sale on its
own — scope, quote, a negotiated price within the organization's limits, the
charge, and the payment confirmed — and the team only hears about it. The
pieces exist but are not wired into the conversation:

- `content/pricing.toml` and `core/services/pricing.py`: the price table, the
  negotiation limits (discounts, installments, floors, validity) and the code
  that turns a scope into a quote. The model never prices; the table does.
- Asaas billing (`docs/ASAAS.md`): charges created from the panel, payment
  status by webhook.

**Blocked on:** Asaas live in production for customer charges. **Needs, when
unblocked:** its own design (brainstorm → spec → plan) — how the desk
collects a scope it can price, when a sale is simple enough to close alone and
when it must go to the consultation, the `voice.never` rule that today forbids
any price being stated, and what the team is told. Consulting stays the
default path for anything custom: the 30-minute conversation is where a
specialist sees automations the customer does not.

---

## What is deliberately not here

- **No autonomous sending, out of the box.** Drafts and offers. Sending and
  booking are permissions someone grants.
- **No parallel system of record.** The calendar, mailbox and drive stay the
  client's. This database holds decisions and audit, not copies.
- **No cloud lock-in.** Postgres, Redis and one container. It runs on a VPS, and
  the only external dependencies are the LLM provider and the client's own
  workspace suite.
