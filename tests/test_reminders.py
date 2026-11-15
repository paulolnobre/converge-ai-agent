"""Reminding someone once, and only once."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from core.models import Appointment
from core.services import reminders

ZONE = ZoneInfo(settings.office_timezone)


def _appointment() -> Appointment:
    start = datetime(2026, 9, 15, 14, 0, tzinfo=ZONE)
    return Appointment(
        id=1,
        contact_id=1,
        calendar_id="desk@example.com",
        provider="google",
        provider_event_id="e1",
        title="Consulta",
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
    )


def test_the_reminder_names_the_day_and_the_time():
    text = reminders.compose(_appointment())
    assert "15/09" in text and "14:00" in text


def test_the_reminder_uses_the_patients_own_name():
    assert "Ana" in reminders.compose(_appointment(), "Ana")


def test_the_reminder_offers_a_way_out():
    """The organization's template tells the person how to reschedule."""
    text = reminders.compose(_appointment())
    assert "remarcar" in text and "cancelar" in text



def test_the_reminder_carries_the_meeting_link_when_there_is_one():
    with_link = _appointment().model_copy(update={"meeting_url": "https://meet.google.com/abc"})
    assert reminders.compose(with_link).endswith("https://meet.google.com/abc")
    assert "Google Meet" not in reminders.compose(_appointment())


# ── Sending: Telegram as before, WhatsApp by approved template ──────────────

import pytest  # noqa: E402

from core import content  # noqa: E402
from core.integrations import telegram, whatsapp, workspace  # noqa: E402
from core.integrations.workspace import WorkspaceError  # noqa: E402
from core.models import Contact  # noqa: E402
from core.repositories import appointments as appointments_repo  # noqa: E402
from core.repositories import contacts as contacts_repo  # noqa: E402
from core.repositories import handoffs as handoffs_repo  # noqa: E402
from core.services import attendance  # noqa: E402


class _Conn:
    """Only what send_due asks of a connection itself: a commit per reminder,
    a rollback for one that failed."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


CONN = _Conn()


class _Mail:
    """The shared mailbox, in memory."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.fail: Exception | None = None

    async def send_message(self, mailbox, message):
        if self.fail is not None:
            raise self.fail
        self.sent.append((mailbox, message))
        return "m1"


@pytest.fixture
def due(monkeypatch: pytest.MonkeyPatch):
    """One reminder due, for a contact whose chat the test sets."""
    state: dict = {"contact": Contact(id=1, chat_id="42", name="Ana"), "telegram": [],
                   "templates": [], "reminded": [], "recorded": [], "mail": _Mail(),
                   "telegram_ok": True, "whatsapp_result": "ok"}
    monkeypatch.setattr(settings, "reception_mailbox", "team@getconverge.me")
    monkeypatch.setattr(workspace, "mail", lambda: state["mail"])
    appointment = _appointment().model_copy(update={"meeting_url": "https://meet.google.com/abc"})
    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v",
                        "whatsapp_reminder_template": "lembrete_conversa"}.items():
        monkeypatch.setattr(settings, name, value)

    async def due_reminders(conn, hours, late):
        return [state["appointment"]]

    async def claim_reminder(conn, appointment_id, starts_at):
        state["reminded"].append(appointment_id)
        return state["appointment"] if starts_at == state["appointment"].starts_at else None

    async def nothing(*args, **kwargs):
        return None

    async def get_by_id(conn, contact_id):
        return state["contact"]

    async def record_message(conn, contact_id, chat_id, role, message, intent=None, **kwargs):
        state["recorded"].append((chat_id, message, intent))

    async def send_message(chat_id, text, **kwargs):
        state["telegram"].append((chat_id, text, kwargs.get("reply_markup")))
        return state["telegram_ok"]

    async def send_template(to, name, language, body_params, button_payloads):
        state["templates"].append((to, name, language, body_params, button_payloads))
        return state["whatsapp_result"]

    monkeypatch.setattr(appointments_repo, "due_reminders", due_reminders)
    monkeypatch.setattr(appointments_repo, "claim_reminder", claim_reminder)
    monkeypatch.setattr(appointments_repo, "lock_contact", nothing)
    monkeypatch.setattr(appointments_repo, "lock_event", nothing)
    monkeypatch.setattr(handoffs_repo, "lock_chat", nothing)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(telegram, "send_message", send_message)
    monkeypatch.setattr(whatsapp, "send_template", send_template)
    CONN.commits = CONN.rollbacks = 0
    state["appointment"] = appointment
    return state


async def test_the_telegram_reminder_is_unchanged(due):
    assert await reminders.send_due(CONN) == 1

    assert due["telegram"] == [("42", reminders.compose(due["appointment"], "Ana"),
                                attendance.reminder_keyboard(due["appointment"]))]
    assert due["templates"] == []


async def test_a_whatsapp_reminder_goes_by_template(due):
    due["contact"] = Contact(id=1, chat_id="wa:5591999990000", name="Ana", channel="whatsapp")

    assert await reminders.send_due(CONN) == 1

    (sent,) = due["templates"]
    assert sent[:4] == ("5591999990000", "lembrete_conversa", "pt_BR",
                        ["Ana", "terça-feira, 15/09", "14:00", "https://meet.google.com/abc"])
    assert _plain(sent[4]) == ["appt:ok:1", "appt:move:1", "appt:cancel:1"]
    assert due["telegram"] == []
    assert due["recorded"] == [("wa:5591999990000",
                                reminders.compose(due["appointment"], "Ana"), "reminder")]


async def test_a_whatsapp_reminder_without_a_link_says_where_it_is(due):
    due["contact"] = Contact(id=1, chat_id="wa:5591999990000", name="Ana", channel="whatsapp")
    due["appointment"] = due["appointment"].model_copy(update={"meeting_url": None})

    await reminders.send_due(CONN)

    assert due["templates"][0][3][3] == content.text("whatsapp.no_meet_link")


async def test_without_a_template_the_whatsapp_reminder_is_skipped(
    due, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setattr(settings, "whatsapp_reminder_template", None)
    due["contact"] = Contact(id=1, chat_id="wa:5591999990000", name="Ana", channel="whatsapp")

    assert await reminders.send_due(CONN) == 0

    assert due["templates"] == [] and due["telegram"] == []
    assert due["reminded"] == [1]
    assert "WHATSAPP_REMINDER_TEMPLATE" in caplog.text



async def test_one_reminder_that_fails_does_not_undo_the_others(due, monkeypatch):
    """A /teste wipe committed while the batch runs makes that reminder's
    history insert fail. Each reminder is its own transaction, committed as it
    goes out: only the failed one is undone, nothing waits for the batch, and a
    crash mid-batch never sends the ones already sent again."""
    second = due["appointment"].model_copy(update={"id": 2, "contact_id": 2})
    contacts = {1: Contact(id=1, chat_id="41", name="Teste"), 2: Contact(id=2, chat_id="42", name="Ana")}

    async def due_reminders(conn, hours, late):
        return [due["appointment"], second]

    async def get_by_id(conn, contact_id):
        return contacts[contact_id]

    async def record_message(conn, contact_id, chat_id, role, message, intent=None, **kwargs):
        if contact_id == 1:
            raise RuntimeError("insert or update on table sessions violates foreign key")
        due["recorded"].append((chat_id, message, intent))

    monkeypatch.setattr(appointments_repo, "due_reminders", due_reminders)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)

    assert await reminders.send_due(CONN) == 1
    assert [chat for chat, _, _ in due["recorded"]] == ["42"]
    assert (CONN.commits, CONN.rollbacks) == (2, 1)


def _plain(datas):
    """Buttons as their action and appointment, without the time stamp of the
    new format: appt2:move:1:29889694 → appt:move:1."""
    from core.services import appointment_taps

    out = []
    for data in datas:
        if data.startswith(appointment_taps.APPOINTMENT_PREFIXES):
            action, appointment_id, _ = appointment_taps.parse(data)
            out.append(f"appt:{action}:{appointment_id}")
        else:
            out.append(data)
    return out


# ── The e-mail takes over when the channel did not ──────────────────────────

def _wa_contact(email="ana@example.com"):
    return Contact(id=1, chat_id="wa:5591999990000", name="Ana", email=email,
                   channel="whatsapp")


async def test_a_whatsapp_reminder_the_channel_refused_goes_by_email(due):
    due["contact"] = _wa_contact()
    due["whatsapp_result"] = "failed"

    assert await reminders.send_due(CONN) == 1

    ((mailbox, mail),) = due["mail"].sent
    assert mailbox == "team@getconverge.me"
    assert mail.to == ["ana@example.com"]
    assert "terça-feira, 15/09" in mail.subject
    assert "Ana" in mail.body_text and "15/09" in mail.body_text and "14:00" in mail.body_text
    assert "https://meet.google.com/abc" in mail.body_text
    assert "responder este e-mail" in mail.body_text
    assert "botão" not in mail.body_text  # an e-mail has no buttons
    assert due["recorded"] == [("wa:5591999990000", mail.body_text, "reminder")]


async def test_without_the_template_a_whatsapp_reminder_goes_by_email(
    due, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "whatsapp_reminder_template", None)
    due["contact"] = _wa_contact()

    assert await reminders.send_due(CONN) == 1

    assert due["templates"] == [] and len(due["mail"].sent) == 1


async def test_a_telegram_reminder_the_channel_refused_goes_by_email(due):
    due["contact"] = Contact(id=1, chat_id="42", name="Ana", email="ana@example.com")
    due["telegram_ok"] = False

    assert await reminders.send_due(CONN) == 1

    assert len(due["mail"].sent) == 1


async def test_a_reminder_the_channel_took_is_not_also_sent_by_email(due):
    due["contact"] = _wa_contact()

    assert await reminders.send_due(CONN) == 1

    assert due["mail"].sent == []


async def test_the_email_says_nothing_about_a_link_when_there_is_none(due):
    due["contact"] = _wa_contact()
    due["whatsapp_result"] = "failed"
    due["appointment"] = due["appointment"].model_copy(update={"meeting_url": None})

    await reminders.send_due(CONN)

    assert "Meet" not in due["mail"].sent[0][1].body_text


@pytest.mark.parametrize("email", [None, ""])
async def test_without_an_email_on_file_nothing_more_is_tried(due, email):
    due["contact"] = _wa_contact(email)
    due["whatsapp_result"] = "failed"

    assert await reminders.send_due(CONN) == 0

    assert due["mail"].sent == [] and due["recorded"] == []


async def test_without_a_mailbox_nothing_more_is_tried(due, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "reception_mailbox", None)
    due["contact"] = _wa_contact()
    due["whatsapp_result"] = "failed"

    assert await reminders.send_due(CONN) == 0

    assert due["mail"].sent == []


async def test_a_mailbox_that_fails_sends_nothing_and_is_not_counted(due, caplog):
    due["contact"] = _wa_contact()
    due["whatsapp_result"] = "failed"
    due["mail"].fail = WorkspaceError("mail.send_message failed", 502)

    assert await reminders.send_due(CONN) == 0

    assert due["recorded"] == []
    assert "ana@example.com" not in caplog.text


async def test_a_blocked_contact_gets_no_reminder(due, monkeypatch):
    monkeypatch.setattr(contacts_repo, "is_blocked", lambda conn, chat_id: _blocked())
    assert await reminders.send_due(CONN) == 0
    assert due["telegram"] == [] and due["templates"] == []


async def _blocked():
    return True
