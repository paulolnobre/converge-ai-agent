# ai-agents-reception

A receptionist and secretary that works inside the tools the organization
already pays for. It answers on Telegram, books into the real calendar, works the
real inbox, and sends the real files from the real drive.

Nothing it produces lives only here. Turn the service off and every appointment
is still in the calendar, every reply still in the sent folder, every document
still in the drive.

```
     the public                 this service                  the organization's tools
  ┌──────────────┐        ┌─────────────────────────┐        ┌──────────────────────┐
  │  Telegram    │        │  FastAPI webhook        │        │  Google Workspace    │
  │  text        │───────►│    ↓ (persist, ack)     │        │   Calendar · Gmail   │
  │  voice       │        │  Redis queue            │        │   Drive · Sheets     │
  └──────────────┘        │    ↓                    │───────►│                      │
                          │  worker                 │        │  Microsoft 365       │
  ┌──────────────┐        │    ↓                    │        │   Outlook · OneDrive │
  │  shared      │───────►│  LangGraph triage       │        │   SharePoint · Excel │
  │  inbox       │        │    ↓                    │        └──────────────────────┘
  └──────────────┘        │  PostgreSQL             │
                          └─────────────────────────┘
```

The workspace layer is one interface with two implementations, chosen in
configuration. A client on Microsoft 365 and a client on Google Workspace run the
same image; so does a client whose mail is on one and whose drive is on the
other.

---

## What it does

**Phase 1 — the front desk.** Answers questions, in text or by voice, in the
organization's own words. Knows what it does not know and says so instead of
inventing a price or an address. Takes a message and escalates to a person.

**Phase 2 — the diary.** Reads free/busy across every calendar that matters,
offers three real times, books the one the person picks, reschedules, cancels,
and reminds them the day before — exactly once.

**Phase 3 — the inbox and the drive.** Triages the shared mailbox by category and
urgency, drafts replies for a person to approve, finds and sends documents — and
refuses to send anything outside the folder that was explicitly marked shareable.

**Phase 4 — the secretary.** Meeting minutes with decisions and action items, a
morning briefing, and follow-ups on the threads nobody answered.

**Testing as a customer.** Someone in `content/staff.json` sends `/teste` (or
`/teste antigo`, to start as a returning customer) and is served on the real
path — calendar, Inbox, team group — with everything marked TESTE and left out
of the numbers. `/sair`, or `TEST_IDLE_MINUTES` without a message, cancels the
test's calendar events and deletes its data. `SAIR` without the slash stays the
opt-out.

The full step-by-step plan, with what "done" means for each step, is in
[docs/ROADMAP.md](docs/ROADMAP.md).

---

## Running it locally

```bash
cp .env.example .env          # then fill in at least TELEGRAM_BOT_TOKEN,
                              # OPENAI_API_KEY and DB_PASSWORD
docker compose up -d postgres redis
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest pytest-asyncio ruff
.venv/bin/python -m pytest
```

The whole stack:

```bash
docker compose up --build
curl localhost:8000/health
curl localhost:8000/ready     # also reports which workspace suite is configured
```

Telegram needs a public URL. With a tunnel running:

```bash
python cli/set_webhook.py            # registers PUBLIC_BASE_URL
python cli/set_webhook.py --show
```

### Connecting the workspace

**Google, with a Workspace admin.** Create a service account, enable domain-wide
delegation, and have the admin authorize its client id for the scopes in
`GOOGLE_SCOPES`. Set `GOOGLE_AUTH_MODE=service_account` and
`GOOGLE_DELEGATED_SUBJECT` to the account the agent acts as.

**Google, without one.** Create an OAuth client of type "Desktop app", then:

```bash
python cli/authorize_google.py       # sign in once as the shared account
```

**Microsoft 365.** Register an application in Entra ID with *application*
permissions — `Calendars.ReadWrite`, `Mail.ReadWrite`, `Mail.Send`,
`Files.ReadWrite.All` — and have an admin consent to them.

Then, before the first real message:

```bash
python cli/smoke_workspace.py        # read-only; also prints the times it would offer
python cli/triage_inbox.py --list    # what the mailbox looks like to the agent
```

### Loading the organization's content

The desk answers from `content/faq.md` and nothing else, so it can answer
nothing until this has run:

```bash
python cli/load_content.py           # the FAQ, chunked and embedded, plus the team
python cli/load_content.py --check   # what is loaded now; changes nothing
```

Re-run it whenever the FAQ changes. The document is replaced wholesale, which is
what makes a deleted question stop being answerable. What goes in these files,
and why the facts in them must not be AI-generated, is in
[content/README.md](content/README.md).

---

## Layout

```
content/                     the organization's own words — see content/README.md
  faq.md                     the only facts the desk may state
  messages.toml              every sentence it says, and what it must never say
  staff.json                 the team, their calendars, who covers for whom
  email_templates.toml       inbox categories and the draft for each
core/
  config.py                  every setting, validated; refuses a wrong startup
  content.py                 loads the above; refuses a message it cannot fill
  schedule.py                when the desk is open — weekday and hour, both
  web/app.py                 webhook, /health, /ready, /metrics
  agent.py                   the worker that drains the queue
  handlers/message.py        one message, start to finish
  services/
    graph.py                 LangGraph: triage → answer, or defer to scheduling
    knowledge.py             the FAQ, chunked and retrieved — the only facts it states
    scheduling.py            free/busy → offer → book → cancel → reschedule
    slot_choice.py           which of the offered times the reply just picked
    reminders.py             one reminder per appointment, exactly once
    mailbox.py               triage the inbox, draft, send only on approval
    documents.py             find and send files, within a shareable folder
    secretarial.py           minutes, briefing, follow-ups       (phase 4)
  integrations/
    workspace/               Google Workspace and Microsoft 365, one interface
      base.py                the four ports: calendar, mail, files, sheets
      google.py              Calendar · Gmail · Drive · Sheets
      microsoft.py           Graph: Outlook · OneDrive/SharePoint · Excel
      oauth.py               three grants, tokens cached in memory only
      slots.py               busy periods → offerable times
    telegram.py  llm.py  cache.py  update_queue.py
  repositories/              SQL, one module per aggregate
infra/init.sql               the schema, with its reasoning in comments
cli/                         webhook, OAuth, smoke test, inbox triage, content load
docs/ROADMAP.md              the development plan, phase by phase
```

---

## Decisions worth knowing about

**It states no fact that is not in the organization's FAQ.** The answering node
is given the retrieved chunks and told that anything outside them does not
exist. Retrieve nothing and the model is not called at all — the desk says it
does not know and hands the question to a person. This is the one guardrail that
cannot be softened: a model given an empty context does not decline, it invents,
fluently, in the organization's voice and with its credentials behind it.

**What the desk must never say is configuration, not code.** For a clinic that
list — no diagnosis, no promised walk-in, no closed price on an invasive
procedure — is the difference between a receptionist and a liability. It lives
in `content/messages.toml`, and an empty list refuses to start.

**This bot talks to strangers.** Anyone who finds the handle can write to it. So
the rate limit comes before anything else, nothing is stored about a person
before they have said something, and no message is ever treated as an instruction
to the model.

**The calendar is the source of truth, every time.** Availability is read from
it on every request, never from the appointments table — the person whose diary
it is books things directly in it, and an agent working from a stale mirror
double-books them.

**A slot is offered, not taken.** The times go out; the person picks one. Booking
on first mention is how a diary fills with appointments nobody confirmed.

**The offer is written down.** `offers` is the only conversational state the
agent keeps — everything else it re-reads from the calendar. Without it the
reply "a terca" has nothing to attach to, and the desk offers the same three
times forever. One live offer per chat, so that reply is never ambiguous between
two sets of times.

**The database decides the race, not the calendar read.** Availability is
re-checked at the moment of booking, but two people can both read a slot as free
in the same second. A partial unique index on `(calendar_id, starts_at)` is what
makes the second one lose — and the booking that loses has the calendar event it
already created withdrawn again, so no slot is left blocked by an appointment no
table knows about.

**Reschedule books before it cancels.** The original is released only once the
replacement exists. The other order can leave someone with neither.

**Write to the calendar first, then the database.** If the calendar write fails,
there must be no row claiming an appointment exists. The reverse order produces a
confirmed booking that nobody's diary knows about.

**The agent does not send e-mail.** It drafts. `AUTOSEND_REPLIES` exists and is
off, and the drafts table records who approved what — so turning it on is a
decision someone made, not a default they inherited.

**A file leaves only from the shareable folder.** A drive search for "contrato"
will happily find someone else's contract. Anything outside `SHAREABLE_FOLDER_ID`
needs a person to say yes, and the audit row records that they did.

**Every write into the calendar, mailbox or drive is audited.** The agent acts
with the organization's own credentials, so Google and Microsoft record its
actions as the service account's. `workspace_actions` is the only place that
answers "did the bot send that, or did someone here?" — and it is written on
failure too.
