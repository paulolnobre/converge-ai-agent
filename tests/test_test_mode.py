"""The team's test mode: a staff member is served as a customer, marked TESTE,
and everything is wiped at the end. The SQL is checked by what it says; the
real cascade runs in test_test_mode_pg.py against a throwaway Postgres."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from core import channels, content
from core.config import Settings, settings
from core.handlers import message as handler
from core.integrations import telegram
from core.models import Appointment, Contact, HandoffSession, StaffMember
from core.repositories import appointments as appointments_repo
from core.repositories import automations as automations_repo
from core.repositories import briefing as briefing_repo
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import test_mode as test_repo
from core.services import attendance, automations, handoff, inbox, scheduling, test_marks, test_mode
from core.web import app as web


class _Cursor:
    def __init__(self, log: list[tuple], row: tuple | None):
        self.log = log
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.log.append((sql, params))

    async def fetchall(self):
        return []

    async def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row: tuple | None = None):
        self.calls: list[tuple] = []
        self.row = row

    def cursor(self, **kwargs):
        return _Cursor(self.calls, self.row)

    @property
    def sql(self) -> str:
        return " ".join(sql for sql, _ in self.calls)


async def _recorded(call, row: tuple | None = None) -> _Conn:
    conn = _Conn(row)
    await call(conn)
    return conn


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


# ── Where a contact in test shows up ─────────────────────────────────────────

@pytest.mark.parametrize("name, call", [
    ("inbox list", lambda c: inbox_repo.list_conversations(c, user_id=1)),
    ("inbox waiting count", lambda c: inbox_repo.count_waiting(c)),
    ("new-conversation notice", lambda c: inbox_repo.claim_conversation_notice(c, 1)),
    ("funnel and contacts list", lambda c: funnel_repo.list_leads(c)),
])
async def test_customer_lists_hide_staff_but_show_tests(name, call):
    sql = _flat((await _recorded(call)).sql)

    assert "whatsapp_ids" in sql, name
    # Staff are hidden only when they are not in a test: the test check sits
    # inside the same NOT (...) as the staff check.
    assert re.search(r"NOT \(EXISTS \(SELECT 1 FROM staff .*AND NOT EXISTS \(SELECT 1 FROM "
                     r"contacts tm WHERE tm\.chat_id = .* AND tm\.test_mode IS NOT NULL\)\)",
                     sql), name


@pytest.mark.parametrize("name, call", [
    ("new contacts", lambda c: briefing_repo.new_contacts_since(c, datetime.now())),
    ("inbound", lambda c: briefing_repo.inbound_since(c, datetime.now())),
    ("silent leads", lambda c: briefing_repo.silent_leads(c, 3)),
    ("agenda", lambda c: briefing_repo.appointments_between(c, datetime.now(), datetime.now())),
    ("open handoffs", lambda c: briefing_repo.open_handoffs(c)),
    ("unrecorded outcomes", lambda c: briefing_repo.unrecorded_outcomes(c)),
    ("stale proposals", lambda c: briefing_repo.stale_proposals(c, 7)),
])
async def test_metrics_leave_out_staff_and_tests(name, call):
    sql = _flat((await _recorded(call)).sql)

    assert "whatsapp_ids" in sql, name
    assert "test_mode IS NOT NULL" in sql, name


def test_automations_leave_out_staff_and_tests():
    assert "whatsapp_ids" in automations_repo._CLIENT_OK
    assert "test_mode IS NOT NULL" in automations_repo._CLIENT_OK


@pytest.mark.parametrize("name, call", [
    ("inbox list", lambda c: inbox_repo.list_conversations(c, user_id=1)),
    ("conversation", lambda c: inbox_repo.conversation(c, 1, 1)),
    ("leads", lambda c: funnel_repo.list_leads(c)),
    ("lead", lambda c: funnel_repo.lead(c, 1)),
])
async def test_panel_rows_carry_is_test(name, call):
    assert "AS is_test" in (await _recorded(call)).sql, name


def _contact_row(contact_id, chat_id, name=None, email=None, phone=None, *,
                 source="whatsapp", test_mode=None):
    """A contacts row as _COLUMNS reads it: each encrypted field as its pair,
    the text in the old column (a row not converted yet)."""
    pairs = (name, None, email, None, phone, None, None, None, None, None)
    return (contact_id, chat_id, *pairs, None, None, source, test_mode, None, None)


def test_contact_reads_test_mode():
    row = _contact_row(1, "wa:5591980000002", "Ana", test_mode="antigo")

    assert contacts_repo._to_contact(row).test_mode == "antigo"


def test_predicates_take_the_chat_column():
    hidden = contacts_repo.hidden_from_customer_lists_sql("x.chat_id")
    left_out = contacts_repo.left_out_of_metrics_sql("x.chat_id")

    assert "x.chat_id" in hidden and "x.chat_id" in left_out
    assert " OR " in left_out.split("test_mode")[0]  # staff OR in test
    assert "AND NOT EXISTS" in hidden                 # staff AND NOT in test


# ── The TESTE mark on what the team reads ────────────────────────────────────


PREFIX = "🧪 TESTE · "


def test_team_text_marks_only_tests():
    assert test_marks.team_text("Nova conversa", Contact(test_mode="novo")) == PREFIX + "Nova conversa"
    assert test_marks.team_text("Nova conversa", Contact()) == "Nova conversa"
    assert test_marks.team_text("Nova conversa", None) == "Nova conversa"


def test_event_title_marks_only_tests():
    assert test_marks.event_title("Atendimento: Ana", Contact(test_mode="antigo")) == \
        "[TESTE] Atendimento: Ana"
    assert test_marks.event_title("Atendimento: Ana", Contact()) == "Atendimento: Ana"


def test_idle_minutes_default():
    assert Settings().test_idle_minutes == 120


@pytest.fixture
def team_lines(monkeypatch):
    """Every line meant for the team group, whichever way it is posted."""
    lines: list[str] = []
    monkeypatch.setattr(settings, "team_chat_id", "-100")

    async def notify_team(text, contact_id):
        lines.append(text)
        return True

    async def post_message(chat_id, text, **kwargs):
        lines.append(text)
        return {"message_id": 1}

    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(telegram, "post_message", post_message)
    return lines


@pytest.mark.parametrize("test_mode", ["novo", "antigo"])
async def test_a_test_contact_is_not_announced_in_the_team_group(monkeypatch, team_lines,
                                                                  test_mode):
    ana = Contact(id=7, chat_id="wa:5511900000000", name="Ana", channel="whatsapp",
                  test_mode=test_mode)
    meeting = await _meeting_and_repos(monkeypatch)

    told = await handoff.start(object(), ana, ana.chat_id, "pedido")
    await inbox.notify_new_conversation(object(), ana)
    await attendance.notify_team(meeting, ana, "booked_notice")

    assert told is True, "the person is told the team was called: for a test, nothing to send"
    assert team_lines == []


async def test_a_real_contact_is_announced_unmarked(monkeypatch, team_lines):
    ana = Contact(id=7, chat_id="wa:5511900000000", name="Ana", channel="whatsapp")
    meeting = await _meeting_and_repos(monkeypatch)

    await handoff.start(object(), ana, ana.chat_id, "pedido")
    await inbox.notify_new_conversation(object(), ana)
    await attendance.notify_team(meeting, ana, "booked_notice")

    assert len(team_lines) == 3
    assert not any(line.startswith(PREFIX) for line in team_lines), team_lines


async def _meeting_and_repos(monkeypatch) -> Appointment:
    async def open_session(conn, **kwargs):
        return HandoffSession(id=1, contact_id=7, chat_id="wa:5511900000000", team_chat_id="-100",
                              status="open", reason="pedido",
                              relay_chat_id="-100")

    async def notice_free(conn, contact_id):
        return True

    monkeypatch.setattr(handoffs_repo, "open_session", open_session)
    monkeypatch.setattr(inbox.inbox_repo, "claim_conversation_notice", notice_free)
    start = datetime(2026, 10, 15, 15, 30, tzinfo=ZoneInfo(settings.office_timezone))
    return Appointment(id=3, contact_id=7, calendar_id="c", provider="google",
                       title="Atendimento: Ana", starts_at=start,
                       ends_at=start + timedelta(minutes=30))


# ── The SQL behind a test: create, seed, delete, find the idle ones ──────────


async def test_delete_is_scoped_in_sql():
    conn = await _recorded(lambda c: test_repo.delete(c, 7, staff_own=True), row=(7,))
    sql, params = conn.calls[0]
    target = _flat(sql).split("), s AS")[0]

    assert "c.test_mode IS NOT NULL" in target
    assert "whatsapp_ids" in target            # the staff's own chat, at the start only
    assert "%(staff_own)s" in target and "True" not in sql
    assert params == {"id": 7, "staff_own": True, "idle_minutes": None, "keep": []}


async def test_delete_reports_whether_it_deleted():
    assert await test_repo.delete(_Conn(row=(7,)), 7, staff_own=False) is True
    assert await test_repo.delete(_Conn(row=None), 7, staff_own=False) is False


async def test_create_keeps_the_whatsapp_window():
    at = datetime(2026, 10, 8, 18, 47)
    row = _contact_row(9, "wa:5591980000002", test_mode="novo")
    conn = await _recorded(lambda c: test_repo.create(
        c, "wa:5591980000002", "novo", name=None, email=None, phone=None, inbound_at=at), row=row)
    sql, params = conn.calls[0]

    assert "last_inbound_at" in sql and "test_started_at" in sql
    assert at in params.values() if isinstance(params, dict) else at in params


async def test_create_returns_the_contact():
    row = _contact_row(9, "wa:5591980000002", "Paulo", "p@x.com", "+5591980000002",
                       test_mode="antigo")
    contact = await test_repo.create(_Conn(row=row), "wa:5591980000002", "antigo", name="Paulo",
                                     email="p@x.com", phone="+5591980000002",
                                     inbound_at=datetime(2026, 10, 8))

    assert contact.id == 9 and contact.test_mode == "antigo"


async def test_seed_yesterday_writes_two_lines_dated_a_day_ago():
    conn = await _recorded(lambda c: test_repo.seed_yesterday(c, 9, "42", "oi", "Olá!"))
    sql = _flat(conn.sql)

    assert "INTERVAL '1 day'" in sql
    params = [value for _, call in conn.calls for value in call]
    # The two lines go sealed: envelopes that open to the texts, never the texts.
    from core.crypto import open_sealed

    opened = [open_sealed("sessions.message", v) for v in params if isinstance(v, bytes)]
    assert opened == ["oi", "Olá!"]
    assert "oi" not in params and "Olá!" not in params
    assert "'user'" in sql


async def test_idle_uses_the_minutes_given():
    conn = await _recorded(lambda c: test_repo.idle(c, 120))
    sql, params = conn.calls[0]

    assert "test_mode IS NOT NULL" in sql and "GREATEST" in sql
    assert 120 in params


async def test_still_idle_checks_one_contact():
    conn = _Conn(row=(1,))
    assert await test_repo.still_idle(conn, 7, 120) is True
    sql, params = conn.calls[0]
    assert "test_mode IS NOT NULL" in sql and 7 in params and 120 in params
    assert await test_repo.still_idle(_Conn(row=None), 7, 120) is False


# ── The flow: /teste, /sair, and the end after a quiet while ─────────────────


WA = "wa:5591980000002"
AT = datetime(2026, 10, 8, 21, 47, tzinfo=ZoneInfo("UTC"))


def _meeting(day: int, id_: int) -> Appointment:
    start = datetime(2026, 10, day, 15, 30, tzinfo=ZoneInfo(settings.office_timezone))
    return Appointment(id=id_, contact_id=7, calendar_id="c", provider="google",
                       provider_event_id=f"ev{id_}", title="x", starts_at=start,
                       ends_at=start + timedelta(minutes=30))


class World:
    """The chat as the flow sees it: who is staff, the contact, the calendar."""

    def __init__(self, monkeypatch, *, staff=None, contact=None):
        self.staff = staff
        self.contact = contact or Contact(id=7, chat_id=WA, channel="whatsapp")
        self.upcoming: list[Appointment] = []
        self.cancel_ok: dict[int, bool] = {}
        self.log: list[tuple] = []
        self.sent: list[tuple[str, str]] = []
        self.idle_ids: list[tuple[int, str]] = []
        self.still = True
        self.send_raises = False
        self.orphans: list[Appointment] = []
        self.kept = None
        w = self

        async def find_staff_by_chat(conn, chat_id):
            return w.staff

        async def get_or_create(conn, chat_id, name=None, inbound_at=None):
            return w.contact

        async def get_by_id(conn, contact_id):
            return w.contact

        async def upcoming_for_contact(conn, contact_id):
            return list(w.upcoming)

        async def cancel(conn, appointment):
            w.log.append(("cancel", appointment.id))
            return w.cancel_ok.get(appointment.id, True)

        async def delete(conn, contact_id, *, staff_own, idle_minutes=None, keep_ids=()):
            w.log.append(("delete", contact_id, staff_own))
            w.kept = list(keep_ids)
            return True

        async def orphans(conn):
            return list(w.orphans)

        async def forget(conn, appointment_id):
            w.log.append(("forget", appointment_id))

        async def create(conn, chat_id, mode, *, name, email, phone, inbound_at):
            w.log.append(("create", chat_id, mode, name, email, phone, inbound_at))
            return Contact(id=9, chat_id=chat_id, name=name, test_mode=mode)

        async def seed_yesterday(conn, contact_id, chat_id, user_text, assistant_text):
            w.log.append(("seed", user_text, assistant_text))

        async def idle(conn, minutes):
            w.log.append(("idle", minutes))
            return list(w.idle_ids)

        async def still_idle(conn, contact_id, minutes):
            return w.still

        async def lock_chat(conn, chat_id):
            w.log.append(("lock", chat_id))

        async def send(chat_id, text, **kwargs):
            if w.send_raises:
                raise RuntimeError("meta fora do ar")
            w.sent.append((chat_id, text))
            return True

        monkeypatch.setattr(contacts_repo, "find_staff_by_chat", find_staff_by_chat)
        monkeypatch.setattr(contacts_repo, "get_or_create", get_or_create)
        monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
        monkeypatch.setattr(appointments_repo, "upcoming_for_contact", upcoming_for_contact)
        monkeypatch.setattr(scheduling, "cancel", cancel)
        monkeypatch.setattr(test_repo, "delete", delete)
        monkeypatch.setattr(test_repo, "create", create)
        monkeypatch.setattr(test_repo, "seed_yesterday", seed_yesterday)
        monkeypatch.setattr(test_repo, "idle", idle)
        monkeypatch.setattr(test_repo, "still_idle", still_idle)
        monkeypatch.setattr(test_repo, "orphan_appointments", orphans)
        monkeypatch.setattr(test_repo, "forget_appointment", forget)
        monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)
        monkeypatch.setattr(channels, "send", send)

    def did(self, kind):
        return [entry for entry in self.log if entry[0] == kind]


PAULO = StaffMember(id=1, chat_id="111", name="Paulo Nobre", email="paulo@exemplo.com.br")


@pytest.mark.parametrize("text, expected", [
    ("/teste", "novo"), (" /Teste ", "novo"), ("/teste@ConvergeBot", "novo"),
    ("/TESTE ANTIGO", "antigo"), ("/teste antigo", "antigo"),
    ("/sair", "sair"), ("/SAIR", "sair"), ("/sair@ConvergeBot", "sair"),
    ("/teste xyz", None), ("teste", None), ("SAIR", None), ("sair", None), ("", None),
    (None, None),
])
def test_parse_command(text, expected):
    assert test_mode.parse_command(text) == expected


async def test_staff_starts_a_new_test(monkeypatch):
    w = World(monkeypatch, staff=PAULO)

    assert await test_mode.handle_command(object(), WA, "/teste", AT) == "test_started"

    assert w.did("delete") == [("delete", 7, True)]
    assert w.did("create") == [("create", WA, "novo", None, None, None, AT)]
    assert w.did("seed") == []
    assert w.sent == [(WA, content.text("teste.started_new"))]


async def test_staff_starts_a_returning_test(monkeypatch):
    w = World(monkeypatch, staff=PAULO)

    assert await test_mode.handle_command(object(), WA, "/teste antigo", AT) == "test_started"

    assert w.did("create") == [("create", WA, "antigo", "Paulo Nobre", "paulo@exemplo.com.br",
                                "+5591980000002", AT)]
    assert w.did("seed") == [("seed", "oi", content.text("reply.welcome"))]
    assert w.sent == [(WA, content.text("teste.started_returning"))]


async def test_returning_test_on_telegram_has_no_phone(monkeypatch):
    w = World(monkeypatch, staff=PAULO, contact=Contact(id=7, chat_id="111"))

    await test_mode.handle_command(object(), "111", "/teste antigo", AT)

    assert w.did("create")[0][5] is None


async def test_customer_teste_is_an_ordinary_message(monkeypatch):
    w = World(monkeypatch, staff=None)

    assert await test_mode.handle_command(object(), WA, "/teste", AT) is None
    assert w.log == [] and w.sent == []


async def test_sair_ends_a_test_even_after_leaving_staff(monkeypatch):
    w = World(monkeypatch, staff=None,
              contact=Contact(id=7, chat_id=WA, channel="whatsapp", test_mode="novo"))

    assert await test_mode.handle_command(object(), WA, "/sair", AT) == "test_ended"

    assert w.did("delete") == [("delete", 7, False)]
    assert w.sent == [(WA, content.text("teste.ended"))]


async def test_sair_outside_a_test(monkeypatch):
    w = World(monkeypatch, staff=PAULO)
    assert await test_mode.handle_command(object(), WA, "/sair", AT) == "not_in_test"
    assert w.sent == [(WA, content.text("teste.not_in_test"))] and w.did("delete") == []

    w = World(monkeypatch, staff=None)
    assert await test_mode.handle_command(object(), WA, "/sair", AT) is None
    assert w.sent == [] and w.did("delete") == []


async def test_cleanup_cancels_future_events_and_reports_failures(monkeypatch):
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.upcoming = [_meeting(14, 1), _meeting(15, 2)]
    w.cancel_ok = {1: True, 2: False}

    deleted, failures = await test_mode.cleanup(object(), w.contact, staff_own=False)

    assert deleted and w.did("cancel") == [("cancel", 1), ("cancel", 2)]
    assert w.did("delete") == [("delete", 7, False)]
    assert len(failures) == 1 and "15/10" in failures[0] and "15:30" in failures[0]


async def test_restarting_a_test_cancels_the_old_booking(monkeypatch):
    w = World(monkeypatch, staff=PAULO,
              contact=Contact(id=7, chat_id=WA, channel="whatsapp", test_mode="novo"))
    w.upcoming = [_meeting(15, 4)]

    await test_mode.handle_command(object(), WA, "/teste", AT)

    kinds = [entry[0] for entry in w.log]
    assert kinds.index("cancel") < kinds.index("delete") < kinds.index("create")


class _Pool:
    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def test_sweep_skips_a_contact_that_wrote_meanwhile(monkeypatch):
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]
    w.still = False

    assert await test_mode.sweep_in_pool(_Pool()) == 0
    assert w.did("lock") == [("lock", WA)]
    assert w.did("delete") == [] and w.sent == []


async def test_sweep_expires_and_tells_the_chat(monkeypatch):
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]

    assert await test_mode.sweep_in_pool(_Pool()) == 1
    assert w.did("idle") == [("idle", 120)]
    assert w.did("delete") == [("delete", 7, False)]
    assert w.sent == [(WA, content.text("teste.expired"))]


async def test_sweep_cleanup_stands_when_the_notice_fails(monkeypatch):
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]
    w.send_raises = True

    assert await test_mode.sweep_in_pool(_Pool()) == 1
    assert w.did("delete") == [("delete", 7, False)]


def _update(text: str) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": WA, "type": "private"}, "text": text,
                                        "date": 1791458820, "from": {"first_name": "Paulo"}}}


async def test_process_message_reads_the_command_before_the_rate_limit(monkeypatch):
    seen = []

    async def lock_chat(conn, chat_id):
        return None

    async def note_inbound(conn, chat_id, name, item):
        return None

    async def flooded(chat_id):
        return False, False

    async def handle_command(conn, chat_id, text, inbound_at):
        seen.append((chat_id, text))
        return "test_started"

    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)
    monkeypatch.setattr(handler.channels, "expired", lambda chat_id, message: False)
    monkeypatch.setattr(handler, "_note_inbound", note_inbound)
    monkeypatch.setattr(handler, "_allowed", flooded)
    monkeypatch.setattr(test_mode, "handle_command", handle_command)

    assert await handler.process_message(object(), _update("/teste")) == "test_started"
    assert seen == [(WA, "/teste")]


async def test_sair_without_slash_is_still_opt_out(monkeypatch):
    opted = []

    async def nothing(*args, **kwargs):
        return None

    async def allowed(chat_id):
        return True, False

    async def text_of(message):
        return message.get("text")

    async def no_command(conn, chat_id, text, inbound_at):
        assert text == "SAIR"
        return None

    async def get_or_create(conn, chat_id, name=None, inbound_at=None):
        return Contact(id=7, chat_id=WA, channel="whatsapp", test_mode="novo")

    async def opt_out(conn, contact):
        opted.append(contact.id)
        return "Pronto, sem mensagens automáticas."

    async def send(chat_id, text, **kwargs):
        return True

    monkeypatch.setattr(handoffs_repo, "lock_chat", nothing)
    monkeypatch.setattr(handler.channels, "expired", lambda chat_id, message: False)
    monkeypatch.setattr(handler, "_note_inbound", nothing)
    monkeypatch.setattr(handler, "_allowed", allowed)
    monkeypatch.setattr(handler, "_text_of", text_of)
    monkeypatch.setattr(test_mode, "handle_command", no_command)
    monkeypatch.setattr(contacts_repo, "find_staff_by_chat", nothing)
    monkeypatch.setattr(contacts_repo, "get_or_create", get_or_create)
    monkeypatch.setattr(contacts_repo, "record_message", nothing)
    monkeypatch.setattr(handler.automations_repo, "mark_replied", nothing)
    monkeypatch.setattr(handler.inbox, "notify_new_conversation", nothing)
    monkeypatch.setattr(handler.handoff, "handle_customer_message", nothing)
    monkeypatch.setattr(handler.channels, "typing", nothing)
    monkeypatch.setattr(handler.channels, "send", send)
    monkeypatch.setattr(automations, "opt_out", opt_out)

    assert await handler.process_message(object(), _update("SAIR")) == "opt_out"
    assert opted == [7]


# ── Review fixes (Codex round 1 and the branch review) ───────────────────────



async def test_cleanup_survives_a_calendar_that_crashes(monkeypatch):
    """C1: an OAuth refresh timeout is not a WorkspaceError — it used to escape
    cleanup and leave the test's data in place."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.upcoming = [_meeting(15, 2)]

    async def crash(conn, appointment):
        raise httpx.ConnectTimeout("oauth2.googleapis.com")

    monkeypatch.setattr(scheduling, "cancel", crash)

    deleted, failures = await test_mode.cleanup(object(), w.contact, staff_own=False)

    assert deleted and w.did("delete") == [("delete", 7, False)]
    assert len(failures) == 1 and "15/10" in failures[0]


async def test_the_expiry_deletes_only_if_still_idle():
    """C2 without holding a lock through the calendar calls: the delete itself
    re-checks idleness, so a message noted meanwhile keeps the test alive."""
    conn = await _recorded(lambda c: test_repo.delete(c, 7, staff_own=False, idle_minutes=120),
                           row=None)
    sql, params = conn.calls[0]
    target = _flat(sql).split("), s AS")[0]

    assert "GREATEST(c.last_inbound_at, c.test_started_at)" in target
    assert params["idle_minutes"] == 120


async def test_still_idle_takes_no_lock():
    """The decision before the calendar calls must not hold the contact row:
    a WhatsApp message's inbound note would wait behind slow Google calls."""
    conn = _Conn(row=(1,))
    await test_repo.still_idle(conn, 7, 120)

    assert "FOR UPDATE" not in conn.calls[0][0]


async def test_an_expiry_interrupted_by_a_message_is_not_announced(monkeypatch):
    """The tester wrote between the decision and the delete: nothing is wiped,
    nothing is cancelled in the calendar, nothing is said."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]
    w.upcoming = [_meeting(15, 2)]
    cancelled = []

    async def in_calendar(appointment):
        cancelled.append(appointment.id)
        return True

    monkeypatch.setattr(test_mode, "_cancel_in_calendar", in_calendar)

    async def delete(conn, contact_id, *, staff_own, idle_minutes=None, keep_ids=()):
        w.log.append(("delete", contact_id, staff_own, idle_minutes))
        return False  # the tester wrote between the decision and the delete

    monkeypatch.setattr(test_repo, "delete", delete)

    assert await test_mode.sweep_in_pool(_Pool()) == 0
    assert w.did("delete") == [("delete", 7, False, 120)] and w.sent == []
    assert cancelled == [] and w.did("cancel") == []


async def test_the_expiry_cancels_in_the_calendar_after_the_wipe(monkeypatch):
    """Two phases: the wipe is decided and committed first, in a short
    transaction; the calendar is cleaned afterwards, holding no lock, from the
    list kept before the wipe. A refused event is reported as before."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]
    w.upcoming = [_meeting(14, 1), _meeting(15, 2)]

    async def in_calendar(appointment):
        w.log.append(("calendar", appointment.id))
        return appointment.id == 1

    monkeypatch.setattr(test_mode, "_cancel_in_calendar", in_calendar)

    assert await test_mode.sweep_in_pool(_Pool()) == 1
    kinds = [entry[0] for entry in w.log]
    assert kinds.index("delete") < kinds.index("calendar")
    assert w.did("calendar") == [("calendar", 1), ("calendar", 2)] and w.did("cancel") == []
    assert len(w.sent) == 1 and "15/10" in w.sent[0][1]


async def test_cancelling_in_the_calendar_never_raises(monkeypatch):
    class Calendar:
        async def cancel_event(self, calendar_id, event_id):
            raise httpx.ConnectTimeout("oauth2.googleapis.com")

    monkeypatch.setattr(test_mode.workspace, "calendar", lambda: Calendar())

    assert await test_mode._cancel_in_calendar(_meeting(15, 2)) is False


async def test_the_after_hours_list_is_loaded_before_the_app_serves(monkeypatch):
    """Restarted at night, the app must not take a /teste before it knows the
    team: the list is loaded before the first request."""
    order = []

    async def refresh(**kwargs):
        assert kwargs == {"required": True}
        order.append("refresh")

    async def pool():
        order.append("pool")

    async def idle_worker(*args, **kwargs):
        return None

    async def closed():
        return None

    monkeypatch.setattr(web, "validate_startup_config", lambda: None)
    monkeypatch.setattr(web.workspace, "configuration_report", lambda: {"status": "configured"})
    monkeypatch.setattr(web, "get_pool", pool)
    monkeypatch.setattr(web, "refresh_never_deferred", refresh)
    monkeypatch.setattr(web.data_format, "raise_markers", closed)  # no real pool here
    for name in ("run_update_worker", "run_deferred_worker", "run_reminder_worker",
                 "run_handoff_timeout_worker", "run_briefing_worker", "run_proposal_worker",
                 "run_never_deferred_worker",
                 "run_media_cleanup_worker", "run_calendar_sync_worker"):
        monkeypatch.setattr(web, name, idle_worker)
    monkeypatch.setattr(web, "close_pool", closed)
    for module in (web.update_queue, web.cache, web.llm, web.telegram, web.whatsapp,
                   web.audio, web.workspace):
        monkeypatch.setattr(module, "close", closed)

    async with web.lifespan(web.app):
        assert order == ["pool", "refresh"]


async def test_a_failed_cancel_reaches_the_team_and_the_log(monkeypatch, caplog):
    """C3: the 'remove it by hand' line also goes to the team group and the
    log, so a lost chat message does not lose the event."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, name="Paulo", test_mode="novo"))
    w.upcoming = [_meeting(15, 2)]
    w.cancel_ok = {2: False}
    posted = []
    monkeypatch.setattr(settings, "team_chat_id", "-100")

    async def post_message(chat_id, text, **kwargs):
        posted.append((chat_id, text))
        return {"message_id": 1}

    monkeypatch.setattr(telegram, "post_message", post_message)

    with caplog.at_level("ERROR"):
        await test_mode.cleanup(object(), w.contact, staff_own=False)

    assert len(posted) == 1 and posted[0][0] == "-100"
    assert posted[0][1].startswith(PREFIX) and "15/10" in posted[0][1]
    assert any("ev2" in record.getMessage() for record in caplog.records)


async def test_a_notice_that_did_not_go_out_is_logged(monkeypatch, caplog):
    """C3: channels.send reports failure by returning False, not by raising."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]

    async def undelivered(chat_id, text, **kwargs):
        return False

    monkeypatch.setattr(channels, "send", undelivered)

    with caplog.at_level("ERROR"):
        assert await test_mode.sweep_in_pool(_Pool()) == 1

    assert any("not delivered" in record.getMessage() for record in caplog.records)


async def test_one_broken_expiry_does_not_block_the_others(monkeypatch):
    """R5: a contact whose cleanup raises is logged and skipped; the next
    ones still expire."""
    w = World(monkeypatch, contact=Contact(id=8, chat_id="wa:2", test_mode="novo"))
    w.idle_ids = [(7, "wa:1"), (8, "wa:2")]
    calls = []

    async def get_by_id(conn, contact_id):
        calls.append(contact_id)
        if contact_id == 7:
            raise RuntimeError("banco fora do ar")
        return w.contact

    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)

    assert await test_mode.sweep_in_pool(_Pool()) == 1
    assert calls == [7, 8] and w.did("delete") == [("delete", 8, False)]


@pytest.mark.parametrize("known, deferred", [(True, False), (False, True)])
async def test_team_and_tests_are_not_deferred_after_hours(monkeypatch, known, deferred):
    """R8: at night every update waits for the morning — /teste included. The
    team's chats and chats in a test are answered at once. The webhook reads a
    set kept in memory: it never waits on the database."""
    queued = []

    async def enqueue_update(update, *, deferred):
        queued.append(deferred)
        return True

    async def nothing(*args, **kwargs):
        return True

    async def no_database():
        raise AssertionError("the webhook must not touch the database")

    monkeypatch.setattr(web.update_queue, "enqueue_update", enqueue_update)
    monkeypatch.setattr(web, "is_open", lambda: False)
    monkeypatch.setattr(web, "get_pool", no_database)
    monkeypatch.setattr(web, "_never_deferred", {"wa:5591980000002"} if known else set())
    monkeypatch.setattr(web.channels, "send", nothing)
    monkeypatch.setattr(web.channels, "expired", lambda chat_id, item: False)

    await web._accept({"update_id": 1, "message": {"chat": {"id": "wa:5591980000002", "type": "private"},
                                                   "text": "/teste", "date": 1791458820}})

    assert queued == [deferred]


async def test_the_never_deferred_set_is_read_from_staff_and_tests(monkeypatch):
    class Rows(_Conn):
        pass

    conn = Rows()
    conn.row = None

    async def fetchall_rows(self=None):
        return [("111",), ("wa:5591980000002",), ("wa:559180000002",)]

    calls = []

    class Cur(_Cursor):
        async def fetchall(self):
            return await fetchall_rows()

    conn.cursor = lambda **kw: Cur(calls, None)
    chats = await contacts_repo.never_deferred_chats(conn)

    assert chats == {"111", "wa:5591980000002", "wa:559180000002"}
    sql = _flat(calls[0][0])
    assert "whatsapp_ids" in sql and "test_mode IS NOT NULL" in sql


async def test_refreshing_the_set_keeps_the_last_one_when_the_database_fails(monkeypatch):
    monkeypatch.setattr(web, "_never_deferred", {"111"})

    async def broken():
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(web, "get_pool", broken)
    await web.refresh_never_deferred()

    assert web._never_deferred == {"111"}



async def test_the_expiry_keeps_the_rows_until_the_calendar_is_clean(monkeypatch):
    """R4: the wipe leaves the test's appointments in the database (no owner,
    title [TESTE]) and each row goes only after its calendar event is handled —
    a process killed halfway leaves the rest for the next sweep."""
    w = World(monkeypatch, contact=Contact(id=7, chat_id=WA, test_mode="novo"))
    w.idle_ids = [(7, WA)]
    w.upcoming = [_meeting(14, 1), _meeting(15, 2)]

    async def in_calendar(appointment):
        w.log.append(("calendar", appointment.id))
        return True

    monkeypatch.setattr(test_mode, "_cancel_in_calendar", in_calendar)

    assert await test_mode.sweep_in_pool(_Pool()) == 1
    assert w.kept == [1, 2]   # only the events still to cancel in the calendar
    after_delete = [e for e in w.log[[k[0] for k in w.log].index("delete") + 1:]
                    if e[0] in ("calendar", "forget")]
    assert after_delete == [("calendar", 1), ("forget", 1), ("calendar", 2), ("forget", 2)]


async def test_an_interrupted_expiry_is_finished_by_the_next_sweep(monkeypatch):
    """Appointments a killed sweep left behind are cancelled and removed; a
    refused one is reported to the team, since the chat is gone with the test."""
    w = World(monkeypatch)
    w.orphans = [_meeting(14, 1), _meeting(15, 2)]
    posted = []
    monkeypatch.setattr(settings, "team_chat_id", "-100")

    async def in_calendar(appointment):
        w.log.append(("calendar", appointment.id))
        return appointment.id == 1

    async def post_message(chat_id, text, **kwargs):
        posted.append(text)
        return {"message_id": 1}

    monkeypatch.setattr(test_mode, "_cancel_in_calendar", in_calendar)
    monkeypatch.setattr(telegram, "post_message", post_message)

    assert await test_mode.sweep_in_pool(_Pool()) == 0
    assert w.did("forget") == [("forget", 1), ("forget", 2)]
    assert len(posted) == 1 and posted[0].startswith(PREFIX) and "15/10" in posted[0]
    assert w.sent == []


async def test_orphan_appointments_are_only_the_tests():
    """Found whatever the time: a leftover past its start is still a leftover."""
    conn = await _recorded(lambda c: test_repo.orphan_appointments(c))
    sql, params = conn.calls[0]

    # By the mark, not the title: the title is encrypted and cannot be matched.
    assert "contact_id IS NULL" in sql and "test_mode" in sql and "LIKE" not in sql
    assert "NOW()" not in sql and "confirmed" not in sql


async def test_delete_keeps_only_the_appointments_still_to_cancel():
    """Only the events still to cancel stay, marked cancelled so no reminder,
    no 'aconteceu?' and no count sees them; every other one goes."""
    conn = await _recorded(lambda c: test_repo.delete(c, 7, staff_own=False, idle_minutes=120,
                                                      keep_ids=[5, 6]))
    sql, params = conn.calls[0]
    flat = _flat(sql)

    assert "NOT (id = ANY(%(keep)s))" in flat and "SET status = 'cancelled'" in flat
    assert params["keep"] == [5, 6]


async def test_the_app_does_not_serve_without_the_after_hours_list(monkeypatch):
    """R4: a first load that fails stops the start-up — the deploy then keeps
    the previous image — instead of serving with an empty list."""
    async def pool():
        return _Pool()

    async def broken(conn):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(web, "validate_startup_config", lambda: None)
    monkeypatch.setattr(web.workspace, "configuration_report", lambda: {"status": "configured"})
    monkeypatch.setattr(web, "get_pool", pool)
    monkeypatch.setattr(web.contacts_repo, "never_deferred_chats", broken)

    async def no_markers():
        return None
    monkeypatch.setattr(web.data_format, "raise_markers", no_markers)  # no real pool here

    with pytest.raises(RuntimeError):
        async with web.lifespan(web.app):
            pass
