"""The conversation the desk could not finish before: offer, then book.

These drive `process_message` with the database and the network faked, but with
triage's routing, the offer store, the slot interpreter and the booking rules
all running for real — because the bug they exist to prevent lived in the seams
between them, not inside any one of them.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import content
from core.config import settings
from core.handlers import message as handler
from core.integrations import telegram, whatsapp, workspace
from core.integrations.whatsapp_updates import to_updates
from core.integrations.workspace.models import CalendarEvent, TimeSlot
from core.models import AgentReply, Appointment, Contact, HandoffSession, Intent, Offer, Triage
from core.repositories import appointments as appointments_repo
from core.repositories import audit
from core.repositories import automations as automations_repo
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import media as media_repo
from core.repositories import offers as offers_repo
from core.repositories import proposal_briefings as proposal_repo
from core.repositories.handoffs import StaleSession
from core.services import attendance, handoff, scheduling

ZONE = ZoneInfo(settings.office_timezone)
class _FakeConn:
    """Stands in for a connection: only transaction() is used directly."""

    def transaction(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def block():
            yield None
        return block()


CONN = _FakeConn()
CONTACT = Contact(id=7, chat_id="42", name="Ana")


def slot(day: int, hour: int) -> TimeSlot:
    start = datetime(2026, 9, day, hour, 0, tzinfo=ZONE)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


OFFERED = [slot(14, 10), slot(15, 14), slot(16, 9)]
_AHEAD = TimeSlot(start=datetime.now(ZONE).replace(microsecond=0) + timedelta(days=60),
                  end=datetime.now(ZONE).replace(microsecond=0) + timedelta(days=60, minutes=30))


def update(text: str) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": 42}, "text": text,
                                        "from": {"first_name": "Ana"}}}


def tap(data: str, callback_id: str = "cb1") -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": callback_id, "data": data, "from": {"first_name": "Ana"},
            "message": {"chat": {"id": 42}},
        },
    }


def buttons(keyboard: dict | None) -> list[str]:
    """Every callback_data on a keyboard, in order — what a test taps."""
    if not keyboard:
        return []
    return [
        button["callback_data"]
        for row in keyboard["inline_keyboard"]
        for button in row
    ]


class Desk:
    """One chat, with everything below the handler stubbed but honest."""

    def __init__(self):
        self.sent: list[str] = []
        self.keyboards: list[dict | None] = []
        self.offers: dict[str, Offer] = {}
        self.appointments: list[Appointment] = []
        self.created_events: list[CalendarEvent] = []
        self.busy: list[TimeSlot] = []
        self.offer_calls = 0
        self._next_id = 1
        self.stages: list[str] = []  # every stage the funnel moved to
        self.details: dict = {}  # e-mail/phone saved on the contact
        # What the contact already had. Most tests are about booking, not
        # intake, so by default the desk knows how to reach Ana; the intake
        # tests clear it.
        self.known: dict = {"email": "ana@example.com", "phone": "+55 91 90000-0000"}
        self.pending: str | None = None      # contacts.pending_action
        self.requested: datetime | None = None  # contacts.details_requested_at
        self.posted: list[tuple[str, str]] = []  # (chat_id, text) sent as plain posts
        self.sessions: dict[int, dict] = {}       # session id -> fields
        self.session_by_chat: dict[str, int] = {} # open/claimed session per chat
        self.post_markups: list[dict | None] = []  # reply_markup of each post
        self.recorded: list[tuple] = []           # (role, message, intent)
        self.new_conversation = False             # what the next notice claim returns
        self.ping_ok = True                       # what the next owner-ping claim returns
        self.owner_chats: dict[int, str] = {}     # panel user id -> Telegram chat
        self.origins: list[str] = []              # origins recorded by /start
        self.events: list[str] = []               # lock / who-holds-it reads, in order
        self.next_session_id = 1
        self.inbound: list[datetime | None] = []  # when the customer wrote, per update

    # the calendar
    async def busy_periods(self, calendar_ids, start, end):
        return {name: list(self.busy) for name in calendar_ids}

    async def create_event(self, calendar_id, event: CalendarEvent) -> CalendarEvent:
        self.created_events.append(event)
        link = f"https://meet.google.com/evt-{len(self.created_events)}" if event.online_meeting else None
        return event.model_copy(update={"id": f"evt{len(self.created_events)}", "meeting_link": link})

    async def cancel_event(self, calendar_id, event_id):
        return True

    @property
    def replies(self) -> str:
        return " || ".join(self.sent)


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch, offline_cache):
    state = Desk()
    monkeypatch.setattr(settings, "reception_calendar_id", "desk@example.com")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    monkeypatch.setattr(
        workspace, "configuration_report",
        lambda: {"capabilities": {"calendar": "google"}},
    )
    monkeypatch.setattr(workspace, "calendar", lambda: state)

    async def send_message(chat_id, text, reply_markup=None, **kwargs):
        state.sent.append(text)
        state.keyboards.append(reply_markup)
        return True

    monkeypatch.setattr(telegram, "send_message", send_message)
    monkeypatch.setattr(telegram, "answer_callback_query", lambda *a, **k: _value(True))

    async def post_message(chat_id, text, reply_markup=None, **kwargs):
        state.posted.append((str(chat_id), text))
        state.post_markups.append(reply_markup)
        return 1000 + len(state.posted)

    monkeypatch.setattr(telegram, "post_message", post_message)

    # handoff sessions, in memory — same shape as core.repositories.handoffs
    async def open_session(conn, *, contact_id, chat_id, team_chat_id, reason):
        existing = state.session_by_chat.get(str(chat_id))
        if existing is not None:
            return HandoffSession(**state.sessions[existing])
        sid = state.next_session_id
        state.next_session_id += 1
        row = {"id": sid, "contact_id": contact_id, "chat_id": str(chat_id),
               "team_chat_id": str(team_chat_id), "reason": reason, "status": "open",
               "claimed_by_id": None, "claimed_by_name": None, "claimed_by_user_id": None,
               "relay_chat_id": str(team_chat_id)}
        state.sessions[sid] = row
        state.session_by_chat[str(chat_id)] = sid
        return HandoffSession(**row)

    async def active_session(conn, chat_id):
        state.events.append("read")
        sid = state.session_by_chat.get(str(chat_id))
        return HandoffSession(**state.sessions[sid]) if sid is not None else None

    async def lock_chat(conn, chat_id):
        state.events.append("lock")

    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)

    async def touch_session(conn, session_id):
        pass

    async def close_session(conn, session_id):
        row = state.sessions.get(session_id)
        if not row or row["status"] == "closed":
            return False
        row["status"] = "closed"
        state.session_by_chat.pop(row["chat_id"], None)
        return True

    monkeypatch.setattr(handoffs_repo, "open_session", open_session)
    monkeypatch.setattr(handoffs_repo, "active_session", active_session)
    monkeypatch.setattr(handoffs_repo, "touch", touch_session)
    monkeypatch.setattr(handoffs_repo, "take_reopen_pending", lambda conn, chat_id: _value(None))
    monkeypatch.setattr(handoffs_repo, "close", close_session)

    async def claim_by_user(conn, session_id, user_id, name, expected_owner):
        row = state.sessions.get(session_id)
        if (not row or row["status"] not in ("open", "claimed")
                or row.get("claimed_by_user_id") != expected_owner):
            return False
        row.update(status="claimed", claimed_by_user_id=user_id, claimed_by_name=name,
                   claimed_by_id=None)
        return True

    monkeypatch.setattr(handoffs_repo, "claim_by_user", claim_by_user)

    async def set_origin_if_empty(conn, contact_id, origin):
        state.origins.append(origin)
        return True

    monkeypatch.setattr(inbox_repo, "claim_conversation_notice",
                        lambda conn, contact_id: _value(state.new_conversation))
    monkeypatch.setattr(inbox_repo, "claim_owner_ping",
                        lambda conn, session_id, minutes: _value(state.ping_ok))
    monkeypatch.setattr(inbox_repo, "owner_chat_id",
                        lambda conn, user_id: _value(state.owner_chats.get(user_id)))
    monkeypatch.setattr(inbox_repo, "set_origin_if_empty", set_origin_if_empty)
    monkeypatch.setattr(handoffs_repo, "stage_of", lambda conn, contact_id: _value("respondido"))
    # No proposal questionnaire running (tests/test_proposal_flow.py fakes one).
    monkeypatch.setattr(proposal_repo, "open_for", lambda conn, contact_id: _value(None))
    # Media updates read their mark (media_events) before anything else.
    monkeypatch.setattr(media_repo, "event_seen", lambda conn, update_id: _value(False))
    monkeypatch.setattr(media_repo, "mark_event", lambda conn, update_id: _none())
    monkeypatch.setattr(telegram, "send_typing", lambda chat_id: _none())

    # contacts
    async def get_or_create(conn, chat_id, name=None, inbound_at=None):
        state.inbound.append(inbound_at)
        return CONTACT.model_copy(update={
            **state.known, **state.details,
            "pending_action": state.pending, "details_requested_at": state.requested,
        })

    monkeypatch.setattr(contacts_repo, "get_or_create", get_or_create)

    async def set_pending_action(conn, contact_id, action):
        state.pending = action
        if action and state.requested is None:
            state.requested = datetime.now(ZONE)

    monkeypatch.setattr(contacts_repo, "set_pending_action", set_pending_action)

    async def set_waiting(conn, contact_id, action):
        state.pending = action

    monkeypatch.setattr(contacts_repo, "set_waiting", set_waiting)
    monkeypatch.setattr(automations_repo, "mark_replied", lambda conn, contact_id: _none())
    monkeypatch.setattr(contacts_repo, "get_by_id",
                        lambda conn, contact_id: _value(CONTACT))
    monkeypatch.setattr(contacts_repo, "recent_history",
                        lambda conn, chat_id, limit=10: _value([]))
    # Never talked before: a hello is a new contact.
    monkeypatch.setattr(contacts_repo, "last_message_at", lambda conn, chat_id: _value(None))

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        state.recorded.append((role, message, intent))

    monkeypatch.setattr(contacts_repo, "record_message", record_message)

    async def update_details(conn, contact_id, **fields):
        state.details.update(fields)

    monkeypatch.setattr(contacts_repo, "update_details", update_details)

    # the offer store, in memory but with the same rules
    async def save(conn, offer: Offer, ttl_minutes: int) -> Offer:
        state.offer_calls += 1
        stored = offer.model_copy(update={"id": state.offer_calls, "status": "pending"})
        state.offers = {offer.chat_id: stored}  # a new offer supersedes the old
        return stored

    async def pending_for_chat(conn, chat_id):
        found = state.offers.get(str(chat_id))
        return found if found and found.status == "pending" else None

    async def claim(conn, offer_id):
        for stored in state.offers.values():
            if stored.id == offer_id and stored.status == "pending":
                state.offers[stored.chat_id] = stored.model_copy(
                    update={"status": "taken"}
                )
                return True
        return False

    async def release(conn, offer_id):
        for stored in state.offers.values():
            if stored.id == offer_id:
                state.offers[stored.chat_id] = stored.model_copy(
                    update={"status": "pending"}
                )

    async def expire(conn, chat_id):
        state.offers.pop(str(chat_id), None)

    monkeypatch.setattr(offers_repo, "save", save)
    monkeypatch.setattr(offers_repo, "pending_for_chat", pending_for_chat)
    monkeypatch.setattr(offers_repo, "claim", claim)
    monkeypatch.setattr(offers_repo, "release", release)
    monkeypatch.setattr(offers_repo, "expire", expire)

    # No proposal went out to this chat unless a test says so (proposal_contract).
    from core.repositories import proposal_briefings as briefings_repo

    async def no_proposal_sent(conn, contact_id):
        return None

    monkeypatch.setattr(briefings_repo, "proposal_sent_for", no_proposal_sent)

    # appointments
    async def record(conn, appointment: Appointment):
        stored = appointment.model_copy(update={"id": len(state.appointments) + 1})
        state.appointments.append(stored)
        return stored

    monkeypatch.setattr(appointments_repo, "record", record)
    monkeypatch.setattr(appointments_repo, "upcoming_for_contact",
                        lambda conn, contact_id: _value(list(state.appointments)))
    # No fresh booking unless a test says so (tests/test_booking_ack.py).
    monkeypatch.setattr(appointments_repo, "just_booked",
                        lambda conn, contact_id, minutes=120: _value(None))
    # The booking's per-contact lock and count (MAX_UPCOMING_APPOINTMENTS).
    monkeypatch.setattr(appointments_repo, "lock_contact", lambda conn, contact_id: _none())
    monkeypatch.setattr(appointments_repo, "lock_event",
                        lambda conn, calendar_id, event_id: _none())

    async def count_upcoming(conn, contact_id, *, excluding=None):
        return sum(1 for a in state.appointments if a.id != excluding)
    monkeypatch.setattr(appointments_repo, "count_upcoming", count_upcoming)
    monkeypatch.setattr(audit, "record", lambda conn, **kwargs: _none())

    # the funnel: record where each event moves the contact
    async def move_stage(conn, contact_id, to, allowed_from):
        current = state.stages[-1] if state.stages else "novo"
        if current not in allowed_from:
            return False
        state.stages.append(to)
        return True

    monkeypatch.setattr(funnel_repo, "move_stage", move_stage)

    # the times on offer are fixed, so the assertions are about the conversation
    monkeypatch.setattr(scheduling, "offer_times", lambda **kwargs: _value(OFFERED))
    return state


async def _none():
    return None


async def _value(value):
    return value


def _triaged_as(monkeypatch: pytest.MonkeyPatch, *intents: Intent):
    """Triage answers in sequence, so a two-turn conversation can be driven.

    Booking and reschedule come back with preferred_period="any" — as if the
    period question had already been answered — so these tests keep exercising
    the offer-then-pick loop in two turns. The period question itself, asked
    when nothing says which period, has its own tests below.
    """
    answers: list[Intent] = list(intents)

    async def fake_route(body, chat_id, history_text="", organization="",
                         retrieve=None):
        intent: Intent = answers.pop(0) if answers else "other"
        # Scheduling intents leave the text empty; the desk fills it in.
        text = "" if intent in ("booking", "reschedule", "cancel", "appointments") else "Claro."
        period = "any" if intent in ("booking", "reschedule") else None
        return (
            Triage(intent=intent, confidence=0.9, preferred_period=period),
            AgentReply(text=text, intent=intent),
        )

    monkeypatch.setattr(handler, "route", fake_route)


# ── The loop this whole change exists to close ───────────────────────────────

async def test_asking_then_picking_produces_one_appointment(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Before this, "a terca" was re-triaged as a fresh booking and the desk
    offered the same three times again, forever."""
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("queria marcar um horario"))
    await handler.process_message(CONN, update("a terca"))

    assert len(desk.appointments) == 1
    assert desk.appointments[0].starts_at == slot(15, 14).start
    # The confirmation is the organization's own, and it names the time booked.
    assert "está marcada" in desk.sent[1]
    assert "15/09" in desk.sent[1] and "14:00" in desk.sent[1]
    assert content.fact("endereco") in desk.sent[1]


async def test_the_desk_does_not_offer_the_same_times_twice(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("quero agendar"))
    await handler.process_message(CONN, update("pode ser as 14h de terca"))

    assert desk.offer_calls == 1
    assert "tenho" in desk.sent[0] and "tenho" not in desk.sent[1]


async def test_the_calendar_gets_the_appointment_not_just_the_database(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("o primeiro"))

    assert len(desk.created_events) == 1
    assert desk.created_events[0].start == slot(14, 10).start


# ── What it refuses to decide ────────────────────────────────────────────────

async def test_a_day_with_two_offered_times_is_asked_about_not_guessed(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")
    monkeypatch.setattr(
        scheduling, "offer_times",
        lambda **kwargs: _value([slot(15, 9), slot(15, 14), slot(16, 9)]),
    )

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("terca"))

    assert desk.appointments == []
    assert "09:00" in desk.sent[1] and "14:00" in desk.sent[1]


async def test_a_question_mid_booking_is_answered_and_the_offer_survives(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Changing the subject must not throw away the times on the table."""
    _triaged_as(monkeypatch, "booking", "faq", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("quanto custa a consulta?"))
    await handler.process_message(CONN, update("a terca"))

    assert desk.sent[1] == "Claro."          # the question was answered
    assert len(desk.appointments) == 1       # and the offer was still there


async def test_turning_the_offer_down_asks_for_a_day_instead_of_repeating(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("nenhum desses horarios"))

    assert desk.appointments == []
    assert "Qual dia" in desk.sent[1]
    assert await offers_repo.pending_for_chat(CONN, 42) is None


def _ahead(days: int):
    """A day that is always in the future, whenever the tests run."""
    return (datetime.now(ZONE) + timedelta(days=days)).date()


async def test_the_day_after_which_day_is_read_and_asked_about(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The reported case: "quinta" after "Qual dia?" went to a model that did
    not know today's date, and the desk offered today's times again."""
    _triaged_as(monkeypatch, "booking", "other")
    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("nenhum desses"))
    assert desk.pending == "day:none"
    _no_model(monkeypatch, "the day is read in code")

    day = _ahead(10)
    await handler.process_message(CONN, update(f"pode ser dia {day:%d/%m}"))

    assert desk.sent[-1] == content.text("scheduling.period_question")
    assert buttons(desk.keyboards[-1])[0] == f"period:morning:{day.isoformat()}:none"
    assert desk.pending is None


async def test_a_reschedule_is_still_a_reschedule_after_which_day(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Without the appointment travelling with the wait, "quinta" after a
    declined reschedule became a second booking and the old one stayed."""
    desk.offers["42"] = Offer(id=5, chat_id="42", slots=OFFERED,
                              calendar_id="desk@example.com", replaces_id=9)
    _triaged_as(monkeypatch, "reschedule")
    await handler.process_message(CONN, update("nenhum desses"))
    assert desk.pending == "day:9"

    day = _ahead(10)
    await handler.process_message(CONN, update(f"{day:%d/%m}"))

    assert buttons(desk.keyboards[-1])[0] == f"period:morning:{day.isoformat()}:9"


async def test_an_unreadable_day_is_asked_once_more_then_let_go(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other", "faq")
    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("nenhum desses"))

    await handler.process_message(CONN, update("hmm deixa eu ver"))
    assert desk.sent[-1] == content.text("scheduling.which_day_again")
    assert desk.pending == "day2:none"

    await handler.process_message(CONN, update("quanto custa o plano pro?"))
    assert desk.sent[-1] == "Claro.", "the second miss is answered on its own terms"
    assert desk.pending is None


async def test_a_day_already_gone_is_refused_and_still_awaited(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")
    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("nenhum desses"))

    gone = _ahead(-3)
    await handler.process_message(CONN, update(f"{gone:%d/%m}"))

    assert desk.sent[-1] == content.text("scheduling.day_past")
    assert desk.pending == "day:none"


async def test_refusing_while_naming_another_day_goes_to_that_day(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """ "eu falei na quinta": turned down and named the day in one message."""
    _triaged_as(monkeypatch, "booking", "other")
    await handler.process_message(CONN, update("marcar"))

    day = _ahead(40)   # never one of the fixed offered days
    await handler.process_message(CONN, update(f"nao, prefiro dia {day:%d/%m}"))

    assert desk.sent[-1] == content.text("scheduling.period_question")
    assert buttons(desk.keyboards[-1])[0] == f"period:morning:{day.isoformat()}:none"


async def test_refusing_the_offered_day_by_its_date_asks_which_day(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """ "nao consigo dia 28/09" names the day being refused, not the one wanted —
    the reported case, where all three offered times fell on that one day."""
    day = _ahead(5)
    same_day = [
        TimeSlot(start=start, end=start + timedelta(minutes=30))
        for start in (datetime(day.year, day.month, day.day, hour, 30, tzinfo=ZONE)
                      for hour in (13, 15, 17))
    ]
    desk.offers["42"] = Offer(id=5, chat_id="42", slots=same_day,
                              calendar_id="desk@example.com")
    _triaged_as(monkeypatch, "other")

    await handler.process_message(CONN, update(f"eu n consigo dia {day:%d/%m}"))

    assert desk.sent[-1] == content.text("scheduling.which_day")
    assert desk.pending == "day:none"


async def test_a_day_in_the_request_reaches_the_calendar_search(
    monkeypatch: pytest.MonkeyPatch, desk
):
    asked: list = []

    async def offer_times(**kwargs):
        asked.append(kwargs.get("preferred_day"))
        return OFFERED

    monkeypatch.setattr(scheduling, "offer_times", offer_times)
    _triaged_as(monkeypatch, "booking")
    day = _ahead(10)

    await handler.process_message(CONN, update(f"quero marcar dia {day:%d/%m}"))

    assert asked == [day]
    # The offered times (fixed in this fixture) are not on that day: said so.
    assert desk.sent[-1].startswith("Em ")
    assert "não encontrei horário livre" in desk.sent[-1]


async def test_cancelling_with_nothing_booked_drops_the_offer(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """"cancela" with times on the table and no appointment means the offer."""
    _triaged_as(monkeypatch, "booking", "cancel")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("deixa pra la, cancela"))

    assert "deixei de lado" in desk.sent[1]
    assert await offers_repo.pending_for_chat(CONN, 42) is None


# ── The slot going while the offer sat unanswered ────────────────────────────

async def test_a_slot_taken_meanwhile_is_explained_and_re_offered(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")
    await handler.process_message(CONN, update("marcar"))

    desk.busy = [slot(15, 14)]  # somebody booked it directly in the diary
    await handler.process_message(CONN, update("a terca"))

    assert desk.appointments == []
    assert "acabou de ser preenchido" in desk.sent[1]
    assert "tenho" in desk.sent[1]  # and here are others


async def test_the_same_answer_sent_twice_books_once(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Someone who presses send again because the first looked slow."""
    _triaged_as(monkeypatch, "booking", "other", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))
    await handler.process_message(CONN, update("a terca"))

    assert len(desk.appointments) == 1
    assert len(desk.created_events) == 1


# ── Manhã, tarde, ou um toque — the button path ──────────────────────────────

async def test_booking_without_a_stated_period_asks_first(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The desk asks manhã/tarde before offering anything — three tappable
    buttons, not a free-text guess."""
    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        return (
            Triage(intent="booking", confidence=0.9, preferred_period=None),
            AgentReply(text="", intent="booking"),
        )
    monkeypatch.setattr(handler, "route", fake_route)

    await handler.process_message(CONN, update("queria marcar uma conversa"))

    assert desk.offer_calls == 0
    assert "manhã" in desk.sent[0].lower() and "tarde" in desk.sent[0].lower()
    assert buttons(desk.keyboards[0]) == [
        "period:morning:none:none", "period:afternoon:none:none", "period:any:none:none",
    ]


async def test_a_stated_period_skips_the_question(monkeypatch: pytest.MonkeyPatch, desk):
    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        return (
            Triage(intent="booking", confidence=0.9, preferred_period="afternoon"),
            AgentReply(text="", intent="booking"),
        )
    monkeypatch.setattr(handler, "route", fake_route)

    await handler.process_message(CONN, update("marcar de tarde"))

    assert desk.offer_calls == 1
    assert "tenho" in desk.sent[0]


async def test_tapping_a_period_offers_times_as_buttons(
    monkeypatch: pytest.MonkeyPatch, desk
):
    await handler.process_callback(CONN, tap("period:any:none:none"))

    assert desk.offer_calls == 1
    assert desk.appointments == []
    assert buttons(desk.keyboards[-1]) == ["slot:1:0", "slot:1:1", "slot:1:2"]


async def test_tapping_a_slot_books_it(monkeypatch: pytest.MonkeyPatch, desk):
    await handler.process_callback(CONN, tap("period:any:none:none"))
    await handler.process_callback(CONN, tap("slot:1:1"))

    assert len(desk.appointments) == 1
    assert desk.appointments[0].starts_at == slot(15, 14).start
    assert "está marcada" in desk.sent[-1]


async def test_a_tap_already_acknowledged_by_the_webhook_is_not_acked_again(
    monkeypatch: pytest.MonkeyPatch, desk
):
    acked: list[str] = []

    async def answer_callback_query(callback_id, text=None, base_url=None):
        acked.append(callback_id)
        return True

    monkeypatch.setattr(telegram, "answer_callback_query", answer_callback_query)
    acknowledged = tap("period:any:none:none")
    acknowledged["callback_query"]["_acked"] = True

    await handler.process_callback(CONN, acknowledged)
    assert acked == []

    await handler.process_callback(CONN, tap("period:any:none:none", callback_id="cb2"))
    assert acked == ["cb2"], "a tap that reached the worker unacknowledged is still acked"


async def test_a_stale_slot_tap_does_not_book_against_a_superseded_offer(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """A button from an offer that was replaced must not book blind against
    whichever offer happens to be live when it is finally tapped."""
    await handler.process_callback(CONN, tap("period:any:none:none"))  # offer 1
    await handler.process_callback(CONN, tap("period:any:none:none"))  # supersedes it: offer 2

    await handler.process_callback(CONN, tap("slot:1:0"))  # the stale, first offer

    assert desk.appointments == []


async def test_a_period_tap_filters_the_offered_times(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(
        scheduling, "offer_times",
        lambda **kwargs: _value(
            [slot(14, 9)] if kwargs.get("preferred_period") == "morning" else OFFERED
        ),
    )
    await handler.process_callback(CONN, tap("period:morning:none:none"))
    assert "09:00" in desk.sent[-1]


async def test_reschedule_keeps_what_it_replaces_through_the_period_question(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The appointment being replaced is not lost just because the desk had to
    ask manhã/tarde first — it travels in the button, since nothing else
    remembers it between the question and the tap that answers it."""
    desk.appointments.append(Appointment(
        id=9, contact_id=CONTACT.id, calendar_id="desk@example.com",
        provider="google", title="x",
        # Still ahead: only an appointment that is still to come can be moved.
        starts_at=_AHEAD.start, ends_at=_AHEAD.end,
    ))
    monkeypatch.setattr(
        appointments_repo, "get_by_id",
        lambda conn, appointment_id: _value(
            next((a for a in desk.appointments if a.id == appointment_id), None)
        ),
    )

    async def cancel_in_repo(conn, appointment_id):
        before = len(desk.appointments)
        desk.appointments[:] = [a for a in desk.appointments if a.id != appointment_id]
        return len(desk.appointments) < before

    monkeypatch.setattr(appointments_repo, "cancel", cancel_in_repo)

    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        return (
            Triage(intent="reschedule", confidence=0.9, preferred_period=None),
            AgentReply(text="", intent="reschedule"),
        )
    monkeypatch.setattr(handler, "route", fake_route)

    await handler.process_message(CONN, update("preciso remarcar"))
    period_buttons = buttons(desk.keyboards[0])
    assert period_buttons == [
        "period:morning:none:9", "period:afternoon:none:9", "period:any:none:9",
    ]

    await handler.process_callback(CONN, tap(period_buttons[2]))  # "Tanto faz"
    slot_buttons = buttons(desk.keyboards[-1])
    await handler.process_callback(CONN, tap(slot_buttons[0]))

    # The old appointment is gone and the new one is the only one left.
    assert len(desk.appointments) == 1
    assert desk.appointments[0].id != 9


# ── The funnel follows the conversation ──────────────────────────────────────

async def test_a_booking_moves_the_card_to_agendado(monkeypatch: pytest.MonkeyPatch, desk):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))

    assert desk.stages == ["respondido", "agendado"]


async def test_a_cancellation_moves_it_back_to_respondido(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    desk.stages.append("agendado")
    _triaged_as(monkeypatch, "cancel")

    await handler.process_message(CONN, update("quero cancelar"))
    await handler.process_callback(CONN, tap(f"appt:cancel_yes:{reminded.id}"))

    assert desk.stages == ["agendado", "respondido"]


# ── E-mail and phone: before a booking or a handoff, never for a question ────

def _unknown(desk):
    desk.known = {}


async def test_booking_without_details_asks_for_them_before_any_time(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _unknown(desk)
    _triaged_as(monkeypatch, "booking")

    await handler.process_message(CONN, update("quero marcar uma conversa"))

    assert desk.sent == [content.render(
        "contact.ask_before_booking", faltando=content.text("contact.need_both")
    )]
    assert desk.keyboards == [None] and desk.offer_calls == 0, "no times before the details"
    assert desk.pending == "booking:none:any"


async def test_the_details_pick_the_booking_up_where_it_stopped(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _unknown(desk)
    _triaged_as(monkeypatch, "booking")
    await handler.process_message(CONN, update("quero marcar"))

    async def no_model(*args, **kwargs):
        raise AssertionError("the details are read by pattern, and the booking resumes")
    monkeypatch.setattr(handler, "route", no_model)

    await handler.process_message(CONN, update("ana@x.com 91 90000-0001"))

    assert desk.details == {"email": "ana@x.com", "phone": "+55 91 90000-0001"}
    assert desk.sent[-1].startswith(content.text("contact.noted"))
    assert "tenho" in desk.sent[-1] and buttons(desk.keyboards[-1])[0].startswith("slot:")
    assert desk.pending is None


async def test_only_what_is_missing_is_asked_for(monkeypatch: pytest.MonkeyPatch, desk):
    desk.known = {"email": "ana@x.com"}
    _triaged_as(monkeypatch, "booking")

    await handler.process_message(CONN, update("marcar"))

    assert desk.sent == [content.render(
        "contact.ask_before_booking", faltando=content.text("contact.need_phone")
    )]


async def test_a_known_contact_goes_straight_to_the_times(monkeypatch: pytest.MonkeyPatch, desk):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))

    assert "tenho" in desk.sent[0]
    assert "está marcada" in desk.sent[1]
    assert desk.pending is None and desk.requested is None


async def test_asked_once_then_never_again(monkeypatch: pytest.MonkeyPatch, desk):
    """Whoever would rather not give the details changes the subject — that
    message is answered on its own terms, and the next booking just books."""
    _unknown(desk)
    _triaged_as(monkeypatch, "booking", "faq", "booking")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("prefiro não passar, quanto custa?"))
    await handler.process_message(CONN, update("marcar então"))

    assert desk.sent[1] == "Claro."
    assert desk.pending is None
    assert "tenho" in desk.sent[2], "not asked a second time"


async def test_a_question_never_asks_for_details(monkeypatch: pytest.MonkeyPatch, desk):
    _unknown(desk)
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update("quanto custa o plano pro?"))

    assert desk.sent == ["Claro."]
    assert desk.pending is None and desk.requested is None


async def test_details_sent_unprompted_are_saved_without_the_model(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _unknown(desk)

    async def no_model(*args, **kwargs):
        raise AssertionError("an e-mail and a phone need no model to be read")

    monkeypatch.setattr(handler, "route", no_model)

    await handler.process_message(CONN, update("ana@x.com, 91 90000-0001"))

    assert desk.details == {"email": "ana@x.com", "phone": "+55 91 90000-0001"}
    assert desk.sent == [content.text("contact.thanks")]


async def test_half_the_details_asks_for_the_other_half(monkeypatch: pytest.MonkeyPatch, desk):
    _unknown(desk)
    await handler.process_message(CONN, update("meu email é ana@x.com"))

    assert desk.details == {"email": "ana@x.com"}
    assert desk.sent == [content.text("contact.ask_phone")]


async def test_details_with_a_question_are_saved_and_the_question_answered(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _unknown(desk)
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update("ana@x.com — quanto custa o plano pro?"))

    assert desk.details == {"email": "ana@x.com"}
    assert desk.sent == ["Claro."]


# ── A person, when a person is needed ────────────────────────────────────────

TEAM = "-1001234567890"


def _needs_human(monkeypatch: pytest.MonkeyPatch, intent: Intent, text: str):
    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        return (
            Triage(intent=intent, confidence=0.9),
            AgentReply(text=text, intent=intent, needs_human=True),
        )
    monkeypatch.setattr(handler, "route", fake_route)


def group_message(chat_id: str, text: str, reply_to: int | None = None,
                  user_id: int = 777, name: str = "Victor") -> dict:
    message = {"chat": {"id": int(chat_id), "type": "supergroup"}, "text": text,
               "from": {"id": user_id, "first_name": name}}
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 2, "message": message}


def _no_model(monkeypatch: pytest.MonkeyPatch, complaint: str):
    async def no_model(*args, **kwargs):
        raise AssertionError(complaint)
    monkeypatch.setattr(handler, "route", no_model)


async def _escalate(monkeypatch: pytest.MonkeyPatch, desk) -> None:
    """Ask for a person, with a team group to tell."""
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _needs_human(monkeypatch, "escalation", content.text("reply.escalation"))
    await handler.process_message(CONN, update("quero falar com uma pessoa"))


def _held_in_the_panel(desk, user_id: int = 2, name: str = "Victor") -> None:
    """Ana's conversation, taken in the panel by `name`."""
    desk.sessions[1] = {
        "id": 1, "contact_id": 7, "chat_id": "42", "team_chat_id": TEAM, "reason": "painel",
        "status": "claimed", "claimed_by_id": None, "claimed_by_name": name,
        "claimed_by_user_id": user_id, "relay_chat_id": TEAM,
    }
    desk.session_by_chat["42"] = 1


async def test_asking_for_a_person_tells_the_team_in_one_line(monkeypatch: pytest.MonkeyPatch, desk):
    await _escalate(monkeypatch, desk)

    assert desk.posted == [(TEAM, content.render(
        "inbox.needs_person", nome="Ana", canal="Telegram",
        motivo=content.text("inbox.reason_pedido"),
    ))], "one line — never the customer's words in the group"
    assert desk.sent == [content.text("reply.escalation")]


async def test_the_request_opens_the_conversation_in_the_panel(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")

    await _escalate(monkeypatch, desk)

    assert desk.post_markups[-1] == {"inline_keyboard": [[{
        "text": content.text("inbox.open_button"),
        "url": "https://bot.example.com/painel#inbox/7",
    }]]}


async def test_a_conversation_someone_holds_never_reaches_the_bot(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"
    _no_model(monkeypatch, "a conversation a person holds must never reach triage")

    await handler.process_message(CONN, update("quanto custa o plano pro?"))

    assert desk.sent == [], "the bot says nothing while a person holds it"
    assert desk.recorded == [("user", "quanto custa o plano pro?", "handoff")], "recorded once, for the panel"
    assert desk.posted == [("777", content.render("inbox.owner_ping", nome="Ana"))], (
        "whoever holds it hears that she wrote, not what"
    )


async def test_the_private_notice_waits_between_messages(monkeypatch: pytest.MonkeyPatch, desk):
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"
    desk.ping_ok = False
    _no_model(monkeypatch, "a conversation a person holds must never reach triage")

    await handler.process_message(CONN, update("oi?"))

    assert desk.posted == []


@pytest.mark.parametrize(("data", "label"), [
    ("slot:1:0", "Qua 14/09 10:00"), ("period:any:none:none", "Tanto faz"),
    ("appt:cancel:5", "Cancelar"), ("book:start", "Marcar"), ("handoff", "Falar com a equipe"),
])
async def test_an_old_button_tapped_while_a_person_holds_it_does_nothing(
    monkeypatch: pytest.MonkeyPatch, desk, data, label
):
    """A person is answering: a time, a cancel or a period tapped from an
    earlier message must not book, cancel or answer behind their back."""
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"
    desk.offers["42"] = Offer(id=1, chat_id="42", contact_id=7, slots=OFFERED,
                              calendar_id="desk@example.com", status="pending",
                              expires_at=datetime.now(ZONE) + timedelta(hours=1))
    update_ = tap(data)
    update_["callback_query"]["message"]["reply_markup"] = {
        "inline_keyboard": [[{"text": label, "callback_data": data}]]
    }

    assert await handler.process_callback(CONN, update_) == "handoff"

    assert desk.sent == [] and desk.appointments == [] and desk.created_events == []
    assert desk.recorded == [("user", f"(tocou no botão) {label}", "handoff")]
    assert desk.posted == [("777", content.render("inbox.owner_ping", nome="Ana"))]


async def test_something_the_desk_cannot_read_goes_to_whoever_holds_it(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """A photo, or a voice note it could not transcribe: the bot must not
    answer "Não consegui ler" while a person holds the conversation."""
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"
    photo = {"update_id": 1, "message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}],
                                         "from": {"first_name": "Ana"}}}

    assert await handler.process_message(CONN, photo) == "handoff"

    assert desk.sent == []
    assert desk.recorded == [("user", content.text("inbox.unreadable"), "handoff")]
    assert desk.posted == [("777", content.render("inbox.owner_ping", nome="Ana"))]


async def test_the_message_limit_does_not_speak_for_the_bot_while_a_person_holds_it(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"

    async def flooded(chat_id):
        return False, True

    monkeypatch.setattr(handler, "_allowed", flooded)

    assert await handler.process_message(CONN, update("oi?")) == "handoff"

    assert desk.sent == []
    assert desk.events[:2] == ["lock", "read"], "decided under the lock, like every turn"
    assert desk.recorded == [("user", "oi?", "handoff")]
    assert desk.posted == [("777", content.render("inbox.owner_ping", nome="Ana"))]


async def test_limited_voice_is_recorded_without_download(monkeypatch: pytest.MonkeyPatch, desk):
    _held_in_the_panel(desk)
    monkeypatch.setattr(handler, "_allowed", lambda chat_id: _value((False, False)))

    async def unexpected_download(file_id):
        pytest.fail("rate-limited voice must not be downloaded")

    monkeypatch.setattr(telegram, "download_voice", unexpected_download)
    incoming = {"message": {"chat": {"id": 42}, "voice": {"file_id": "x", "duration": 2},
                            "from": {"first_name": "Ana"}}}

    assert await handler.process_message(CONN, incoming) == "handoff"
    assert desk.recorded == [("user", content.text("inbox.unreadable"), "handoff")]


async def test_a_limited_button_still_reaches_the_owner(monkeypatch: pytest.MonkeyPatch, desk):
    _held_in_the_panel(desk)
    desk.owner_chats[2] = "777"
    monkeypatch.setattr(handler, "_allowed", lambda chat_id: _value((False, False)))
    incoming = tap("appt:cancel:5")
    incoming["callback_query"]["message"]["reply_markup"] = {
        "inline_keyboard": [[{"text": "Cancelar", "callback_data": "appt:cancel:5"}]]
    }

    assert await handler.process_callback(CONN, incoming) == "handoff"

    assert desk.sent == []
    assert desk.recorded == [("user", "(tocou no botão) Cancelar", "handoff")]
    assert desk.posted == [("777", content.render("inbox.owner_ping", nome="Ana"))]


async def test_the_bot_does_not_type_during_a_human_handoff(monkeypatch: pytest.MonkeyPatch, desk):
    _held_in_the_panel(desk)
    typing = []

    async def record_typing(chat_id):
        typing.append(chat_id)

    monkeypatch.setattr(telegram, "send_typing", record_typing)

    assert await handler.process_message(CONN, update("Oi")) == "handoff"
    assert typing == []


@pytest.mark.parametrize("incoming", [update("quanto custa?"), tap("period:any:none:none")])
async def test_the_conversation_is_locked_before_the_bot_decides_anything(
    monkeypatch: pytest.MonkeyPatch, desk, incoming
):
    """Someone taking the conversation in the panel waits for this lock: a
    claim that lands mid-turn happens after the bot's turn, never inside it."""
    _triaged_as(monkeypatch, "faq")
    if "callback_query" in incoming:
        await handler.process_callback(CONN, incoming)
    else:
        await handler.process_message(CONN, incoming)

    assert desk.events and desk.events[0] == "lock", desk.events


async def test_while_nobody_has_taken_it_the_bot_answers_and_the_group_hears_nothing(
    monkeypatch: pytest.MonkeyPatch, desk
):
    await _escalate(monkeypatch, desk)
    posted_before, desk.recorded = len(desk.posted), []
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update("como vai ser a conversa?"))

    assert desk.sent[-1] == "Claro.", "the bot answers while nobody has taken it"
    assert len(desk.posted) == posted_before, "no copy of the conversation in the group"
    assert [r for r in desk.recorded if r[0] == "user"] == [("user", "como vai ser a conversa?", "faq")], (
        "the customer's message is recorded once, not twice"
    )


async def test_the_team_group_no_longer_reaches_customers(monkeypatch: pytest.MonkeyPatch, desk):
    await _escalate(monkeypatch, desk)
    sent_before = len(desk.sent)

    await handler.process_message(CONN, group_message(TEAM, "Oi Ana!", reply_to=1001))
    await handler.process_message(CONN, group_message(TEAM, "/assumir", reply_to=1001))

    assert len(desk.sent) == sent_before, "answering happens in the panel now"


async def test_a_new_conversation_is_announced_once(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _triaged_as(monkeypatch, "faq", "faq")
    desk.new_conversation = True

    await handler.process_message(CONN, update("vocês atendem em Belém?"))
    desk.new_conversation = False    # the claim is spent
    await handler.process_message(CONN, update("e no interior?"))

    assert desk.posted == [(TEAM, content.render("inbox.new_conversation", nome="Ana", canal="Telegram"))]


async def test_without_a_team_group_nothing_is_announced(monkeypatch: pytest.MonkeyPatch, desk):
    desk.new_conversation = True
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update("quanto custa o CRM?"))

    assert desk.posted == []


async def test_a_start_link_records_where_the_person_came_from(monkeypatch: pytest.MonkeyPatch, desk):
    await handler.process_message(CONN, update("/start site"))

    assert desk.origins == ["site"]
    assert desk.sent == [content.text("reply.welcome")], "and the desk still introduces itself"


async def test_a_start_link_with_anything_else_records_nothing(monkeypatch: pytest.MonkeyPatch, desk):
    await handler.process_message(CONN, update("/start qualquercoisa"))

    assert desk.origins == []
    assert desk.sent == [content.text("reply.welcome")]


async def test_without_a_team_group_no_person_is_promised(monkeypatch: pytest.MonkeyPatch, desk):
    _needs_human(monkeypatch, "escalation", content.text("reply.escalation"))

    await handler.process_message(CONN, update("quero falar com uma pessoa"))

    assert desk.posted == []
    assert desk.sent == [content.text("reply.escalation_failed")]


async def test_an_unanswerable_question_offers_the_team_with_a_button(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _needs_human(monkeypatch, "faq", content.text("reply.unknown"))

    await handler.process_message(CONN, update("vocês fazem integração com SAP?"))

    assert desk.sent == [content.text("reply.unknown")]
    assert buttons(desk.keyboards[0]) == ["handoff"]

    await handler.process_callback(CONN, tap("handoff"))
    assert [chat for chat, _ in desk.posted] == [TEAM]
    assert desk.sent[-1] == content.text("reply.escalation")


async def test_the_customers_own_bot_command_returns_control(
    monkeypatch: pytest.MonkeyPatch, desk
):
    await _escalate(monkeypatch, desk)

    await handler.process_message(CONN, update("/bot"))

    assert desk.sent[-1] == content.text("handoff.back_to_bot")
    assert (TEAM, content.render("handoff.closed_notice", nome="Ana")) in desk.posted

    _triaged_as(monkeypatch, "faq")
    await handler.process_message(CONN, update("quanto custa o plano pro?"))
    assert desk.sent[-1] == "Claro."


async def test_team_chatter_that_is_not_a_reply_stays_in_the_group(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)

    await handler.process_message(CONN, group_message(TEAM, "alguém viu o lead de ontem?"))
    await handler.process_message(CONN, group_message(TEAM, "respondendo outra coisa", 999))

    assert desk.posted == [] and desk.sent == []


async def test_a_group_is_never_treated_as_a_customer(monkeypatch: pytest.MonkeyPatch, desk):
    _no_model(monkeypatch, "a group message must never reach triage")

    await handler.process_message(CONN, group_message("-100555", "oi bot, quanto custa?"))

    assert desk.sent == [] and desk.posted == []


async def test_id_tells_a_group_its_own_id(monkeypatch: pytest.MonkeyPatch, desk):
    await handler.process_message(CONN, group_message("-100555", "/id@ai_converge_bot"))

    assert desk.posted == [("-100555", "ID deste grupo: -100555")]


async def test_an_inactive_claimed_session_closes_itself_and_tells_both_sides(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    stale = StaleSession(
        session=HandoffSession(
            id=1, contact_id=7, chat_id="42", team_chat_id=TEAM, reason="pedido",
            status="claimed", claimed_by_id=777, claimed_by_name="Victor", relay_chat_id="777",
        ),
        contact_name="Ana",
    )
    monkeypatch.setattr(handoffs_repo, "stale_sessions", lambda conn, minutes: _value([stale]))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, session_id, minutes: _value(True))

    closed = await handoff.sweep_timeouts(CONN)

    assert closed == 1
    assert desk.sent == [content.text("handoff.timeout_customer")]
    assert (TEAM, content.render("handoff.timeout_claimed", nome="Ana", atendente="Victor")) in desk.posted
    assert desk.events[0] == "lock", "closed under the conversation's lock"


async def test_a_session_that_came_back_to_life_is_not_closed(monkeypatch: pytest.MonkeyPatch, desk):
    """Picked as idle, then the customer wrote before the close: it stays."""
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    stale = StaleSession(
        session=HandoffSession(
            id=1, contact_id=7, chat_id="42", team_chat_id=TEAM, reason="painel",
            status="claimed", claimed_by_name="Victor", claimed_by_user_id=2, relay_chat_id=TEAM,
        ),
        contact_name="Ana",
    )
    monkeypatch.setattr(handoffs_repo, "stale_sessions", lambda conn, minutes: _value([stale]))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, session_id, minutes: _value(False))

    assert await handoff.sweep_timeouts(CONN) == 0
    assert desk.sent == [] and desk.posted == []


async def test_timeout_sweep_releases_each_chat_lock_before_the_next(monkeypatch: pytest.MonkeyPatch, desk):
    stale = [
        StaleSession(
            session=HandoffSession(
                id=n, contact_id=7, chat_id=str(40 + n), team_chat_id=TEAM,
                reason="painel", status="claimed", claimed_by_name="Victor",
                claimed_by_user_id=2, relay_chat_id=TEAM,
            ),
            contact_name="Ana",
        )
        for n in (1, 2)
    ]
    monkeypatch.setattr(handoffs_repo, "stale_sessions", lambda conn, minutes: _value(stale))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, sid, minutes: _value(False))

    class Pool:
        def __init__(self):
            self.connections = []

        @asynccontextmanager
        async def connection(self):
            conn = object()
            self.connections.append(conn)
            yield conn

    pool = Pool()
    assert await handoff.sweep_timeouts_in_pool(pool) == 0
    assert len(pool.connections) == 3  # selection, then one transaction per chat
    assert desk.events == ["lock", "lock"]


async def test_an_inactive_unclaimed_session_closes_with_a_different_notice(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Nobody ever answered — the team is told the request itself went
    unanswered, not that some named person let it go quiet."""
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    stale = StaleSession(
        session=HandoffSession(
            id=1, contact_id=7, chat_id="42", team_chat_id=TEAM, reason="pedido",
            status="open", claimed_by_id=None, claimed_by_name=None, relay_chat_id=TEAM,
        ),
        contact_name="Ana",
    )
    monkeypatch.setattr(handoffs_repo, "stale_sessions", lambda conn, minutes: _value([stale]))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, session_id, minutes: _value(True))

    closed = await handoff.sweep_timeouts(CONN)

    assert closed == 1
    assert (TEAM, content.render("handoff.timeout_open", nome="Ana")) in desk.posted


# ── Answering the reminder, and the team answering for the meeting ──────────

def _upcoming(desk, appointment_id: int = 5, contact_id: int | None = 7,
              days: int = 1) -> Appointment:
    start = (datetime.now(ZONE) + timedelta(days=days)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    appointment = Appointment(
        id=appointment_id, contact_id=contact_id, calendar_id="desk@example.com",
        provider="google", provider_event_id="evt-r", title="x",
        starts_at=start, ends_at=start + timedelta(minutes=30),
    )
    desk.appointments.append(appointment)
    return appointment


@pytest.fixture
def reminded(monkeypatch: pytest.MonkeyPatch, desk):
    """One appointment the reminder was about, and the repo calls it touches."""
    appointment = _upcoming(desk)
    confirmed: list[int] = []
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    monkeypatch.setattr(
        appointments_repo, "get_by_id",
        lambda conn, appointment_id: _value(
            next((a for a in desk.appointments if a.id == appointment_id), None)
        ),
    )

    async def confirm_attendance(conn, appointment_id):
        confirmed.append(appointment_id)
        return True

    async def cancel_in_repo(conn, appointment_id):
        before = len(desk.appointments)
        desk.appointments[:] = [a for a in desk.appointments if a.id != appointment_id]
        return len(desk.appointments) < before

    monkeypatch.setattr(appointments_repo, "confirm_attendance", confirm_attendance)
    monkeypatch.setattr(appointments_repo, "cancel", cancel_in_repo)
    desk.confirmed = confirmed
    return appointment


def _parts(appointment: Appointment) -> dict[str, str]:
    return scheduling.when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))


async def test_the_reminder_carries_confirm_reschedule_and_cancel(monkeypatch, desk, reminded):
    keyboard = attendance.reminder_keyboard(reminded)
    assert _plain(buttons(keyboard)) == [f"appt:ok:{reminded.id}", f"appt:move:{reminded.id}",
                                         f"appt:cancel:{reminded.id}"]


async def test_confirming_from_the_reminder_is_recorded_and_the_team_told(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    await handler.process_callback(CONN, tap("appt:ok:5"))

    assert desk.confirmed == [5]
    assert desk.sent == [content.render("scheduling.attendance_confirmed", **_parts(reminded))]
    parts = _parts(reminded)
    assert (TEAM, content.render(
        "attendance.confirmed_notice", nome="Ana", data=parts["data"], horario=parts["horario"]
    )) in desk.posted


async def test_cancel_from_the_reminder_asks_before_cancelling(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    await handler.process_callback(CONN, tap("appt:cancel:5"))

    assert len(desk.appointments) == 1, "one tap must not cancel"
    assert _plain(buttons(desk.keyboards[-1])) == ["appt:cancel_yes:5", "appt:keep:5"]

    await handler.process_callback(CONN, tap("appt:cancel_yes:5"))

    assert desk.appointments == []
    assert desk.sent[-1] == content.render("scheduling.cancellation", **_parts(reminded))
    assert any("cancelou" in text for chat, text in desk.posted if chat == TEAM)


async def test_keeping_it_cancels_nothing(monkeypatch: pytest.MonkeyPatch, desk, reminded):
    await handler.process_callback(CONN, tap("appt:cancel:5"))
    await handler.process_callback(CONN, tap("appt:keep:5"))

    assert len(desk.appointments) == 1
    assert desk.sent[-1] == content.text("scheduling.kept")


async def test_reschedule_from_the_reminder_keeps_the_old_time_until_a_new_one(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    await handler.process_callback(CONN, tap("appt:move:5"))

    assert desk.sent == [content.text("scheduling.period_question")]
    assert buttons(desk.keyboards[-1])[0] == "period:morning:none:5"
    assert len(desk.appointments) == 1


async def test_a_button_for_someone_elses_appointment_does_nothing(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    _upcoming(desk, appointment_id=6, contact_id=99)

    await handler.process_callback(CONN, tap("appt:cancel_yes:6"))

    assert len(desk.appointments) == 2
    assert desk.sent == [content.text("scheduling.reminder_stale")]


def group_tap(data: str, chat_id: str, name: str = "Victor") -> dict:
    return {
        "update_id": 4,
        "callback_query": {
            "id": "cb-team", "data": data, "from": {"id": 777, "first_name": name},
            "message": {"chat": {"id": int(chat_id), "type": "supergroup"}},
        },
    }


async def test_the_team_is_asked_once_whether_a_finished_meeting_happened(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    finished = _upcoming(desk, appointment_id=8, days=-1)
    asked: list[int] = []
    monkeypatch.setattr(appointments_repo, "due_outcomes", lambda conn: _value([finished]))

    async def claim_outcome(conn, appointment_id, starts_at):
        asked.append(appointment_id)
        return finished if starts_at == finished.starts_at else None

    monkeypatch.setattr(appointments_repo, "claim_outcome", claim_outcome)

    assert await attendance.ask_outcomes(CONN) == 1
    assert asked == [8]
    assert "aconteceu" in desk.posted[-1][1]


async def test_the_teams_answer_records_a_no_show(monkeypatch: pytest.MonkeyPatch, desk, reminded):
    _upcoming(desk, appointment_id=8, days=-1)
    outcomes: list[tuple[int, str, str]] = []

    async def set_outcome(conn, appointment_id, status, by):
        outcomes.append((appointment_id, status, by))
        return True

    monkeypatch.setattr(appointments_repo, "set_outcome", set_outcome)
    desk.stages.append("agendado")

    await handler.process_callback(CONN, group_tap("outcome:no_show:8", TEAM))

    assert outcomes == [(8, "no_show", "Victor")]
    assert desk.stages[-1] == "respondido"
    assert desk.posted[-1] == (TEAM, content.render(
        "attendance.recorded_no_show", nome="Ana", atendente="Victor"
    ))
    assert desk.sent == [], "a group tap never answers as if to a customer"


async def test_an_outcome_tap_from_another_group_is_ignored(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    monkeypatch.setattr(
        appointments_repo, "set_outcome",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not record")),
    )

    await handler.process_callback(CONN, group_tap("outcome:done:5", "-100999"))

    assert desk.posted == []


async def test_id_in_a_private_chat_answers_with_the_persons_own_id(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _no_model(monkeypatch, "/id must never reach triage")

    await handler.process_message(CONN, update("/id"))

    assert desk.sent == ["Seu ID: 42"]
    assert desk.session_by_chat == {}, "no handoff opened"


async def test_asking_for_a_person_without_details_asks_first(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _unknown(desk)
    _needs_human(monkeypatch, "escalation", content.text("reply.escalation"))

    await handler.process_message(CONN, update("quero falar com uma pessoa"))

    assert desk.sent == [content.render(
        "contact.ask_before_handoff", faltando=content.text("contact.need_both")
    )]
    assert desk.posted == [], "the team is called once the details arrive"

    await handler.process_message(CONN, update("ana@x.com 91 90000-0001"))

    # The details go to the contact (read in the panel), not into the group.
    assert [text for chat, text in desk.posted if chat == TEAM] == [content.render(
        "inbox.needs_person", nome="Ana", canal="Telegram", motivo=content.text("inbox.reason_pedido")
    )]
    assert desk.details["phone"] == "+55 91 90000-0001"
    assert desk.sent[-1] == f"{content.text('contact.noted')}\n\n{content.text('reply.escalation')}"


async def test_the_team_button_asks_for_details_first_too(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _unknown(desk)

    await handler.process_callback(CONN, tap("handoff"))

    assert desk.sent == [content.render(
        "contact.ask_before_handoff", faltando=content.text("contact.need_both")
    )]
    assert desk.pending == "escalation:botao" and desk.posted == []


# ── Holding a conversation, not only answering the FAQ ───────────────────────

@pytest.mark.parametrize("hello", [
    "ola", "Olá!", "oi", "oiii", "Bom dia", "boa tarde, tudo bem?", "e aí", "Oi, Converge",
])
async def test_a_greeting_is_answered_without_the_model(
    monkeypatch: pytest.MonkeyPatch, desk, hello
):
    """ "ola" once reached triage, was read as a question, and got "não tenho
    essa informação". A hello never needs a model."""
    _no_model(monkeypatch, "a greeting needs no model")

    await handler.process_message(CONN, update(hello))

    assert desk.sent == [content.text("reply.welcome")]


@pytest.mark.parametrize("not_only_hello", ["oi, quanto custa o plano pro?", "olá quero marcar"])
async def test_a_greeting_with_a_request_goes_to_triage(
    monkeypatch: pytest.MonkeyPatch, desk, not_only_hello
):
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update(not_only_hello))

    assert desk.sent == ["Claro."]


async def test_someone_back_after_a_day_is_greeted_by_name(monkeypatch: pytest.MonkeyPatch, desk):
    _no_model(monkeypatch, "a greeting needs no model")
    monkeypatch.setattr(contacts_repo, "last_message_at", lambda conn, chat_id: _value(
        datetime.now(UTC) - timedelta(hours=25)))

    await handler.process_message(CONN, update("ola"))

    assert desk.sent == [content.render("reply.welcome_back", nome="Ana")]


def _mid_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desk has just answered (an ad's message, say): a minute ago."""
    monkeypatch.setattr(contacts_repo, "recent_history", lambda conn, chat_id, limit=10: _value([
        ("user", "Quero saber mais sobre o Assistente Jurídico"),
        ("assistant", "Quer marcar uma conversa ou prefere que eu anote os detalhes?"),
    ]))
    monkeypatch.setattr(contacts_repo, "last_message_at", lambda conn, chat_id: _value(
        datetime.now(UTC) - timedelta(minutes=1)))


async def test_a_hello_mid_conversation_picks_up_where_it_stopped(
        monkeypatch: pytest.MonkeyPatch, desk):
    """Ivan came from Instagram, got the answer, then wrote "Oi, boa noite":
    it was answered "que bom falar com você de novo", as if he had come back."""
    _no_model(monkeypatch, "no triage for a hello")
    _mid_conversation(monkeypatch)
    seen: list[tuple[str, str]] = []

    async def greet(message, history_text):
        seen.append((message, history_text))
        return "Boa noite, Ana! Prefere marcar a conversa ou que eu anote os detalhes?"

    monkeypatch.setattr(handler, "greet_in_conversation", greet)

    await handler.process_message(CONN, update("Oi, boa noite."))

    assert desk.sent == ["Boa noite, Ana! Prefere marcar a conversa ou que eu anote os detalhes?"]
    assert desk.keyboards == [None], "not the welcome's buttons again"
    assert "Quer marcar uma conversa" in seen[0][1], "the model reads what came before"


async def test_a_silent_model_still_greets_mid_conversation(
        monkeypatch: pytest.MonkeyPatch, desk):
    _no_model(monkeypatch, "no triage for a hello")
    _mid_conversation(monkeypatch)
    monkeypatch.setattr(handler, "greet_in_conversation", lambda message, history: _value(""))

    await handler.process_message(CONN, update("oi"))

    assert desk.sent == [content.text("reply.hello_ongoing")]
    assert "de novo" not in desk.sent[0]


async def test_start_always_introduces_the_desk(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(contacts_repo, "recent_history",
                        lambda conn, chat_id, limit=10: _value([("user", "oi")]))

    await handler.process_message(CONN, update("/start"))

    assert desk.sent == [content.text("reply.welcome")]


async def test_asking_what_is_booked_lists_it_from_the_database(
    monkeypatch: pytest.MonkeyPatch, desk
):
    desk.appointments.append(Appointment(
        id=3, contact_id=CONTACT.id, calendar_id="desk@example.com", provider="google",
        title="x", starts_at=slot(28, 12).start, ends_at=slot(28, 12).end,
    ))
    _triaged_as(monkeypatch, "appointments")

    await handler.process_message(CONN, update("quais consultas eu tenho marcadas?"))

    assert desk.sent == [content.render(
        "scheduling.upcoming_one", quando=scheduling.when(slot(28, 12))
    )]


async def test_several_bookings_are_all_listed(monkeypatch: pytest.MonkeyPatch, desk):
    for i, day in enumerate((28, 30), start=1):
        desk.appointments.append(Appointment(
            id=i, contact_id=CONTACT.id, calendar_id="desk@example.com", provider="google",
            title="x", starts_at=slot(day, 10).start, ends_at=slot(day, 10).end,
        ))
    _triaged_as(monkeypatch, "appointments")

    await handler.process_message(CONN, update("o que eu tenho marcado?"))

    lista = f"{scheduling.when(slot(28, 10))} e {scheduling.when(slot(30, 10))}"
    assert desk.sent == [content.render("scheduling.upcoming_many", lista=lista)]


async def test_nothing_booked_says_so_and_offers_to_book(monkeypatch: pytest.MonkeyPatch, desk):
    _triaged_as(monkeypatch, "appointments")

    await handler.process_message(CONN, update("tenho algo marcado?"))

    assert desk.sent == [content.text("scheduling.none_upcoming")]


# ── Cancelling asks first; several bookings are chosen by button ─────────────

async def test_a_typed_cancel_asks_before_cancelling(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    """ "quero cancelar" once cancelled on the spot — and "cancela" said in the
    middle of a reschedule cancelled the meeting itself."""
    _triaged_as(monkeypatch, "cancel")

    await handler.process_message(CONN, update("quero cancelar"))

    assert len(desk.appointments) == 1, "nothing is cancelled on a word"
    assert desk.sent[-1] == content.render("scheduling.cancel_check", **_parts(reminded))
    assert _plain(buttons(desk.keyboards[-1])) == [
        f"appt:cancel_yes:{reminded.id}", f"appt:keep:{reminded.id}",
    ]

    await handler.process_callback(CONN, tap(f"appt:cancel_yes:{reminded.id}"))

    assert desk.appointments == []
    assert desk.sent[-1] == content.render("scheduling.cancellation", **_parts(reminded))
    assert any("cancelou" in text for chat, text in desk.posted if chat == TEAM)


async def test_with_two_bookings_cancel_offers_one_button_each(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    other = _upcoming(desk, appointment_id=6, days=3)
    _triaged_as(monkeypatch, "cancel")

    await handler.process_message(CONN, update("quero cancelar"))

    assert desk.sent[-1].startswith("Você tem estas conversas marcadas:")
    assert _plain(buttons(desk.keyboards[-1])) == [f"appt:cancel:{reminded.id}", f"appt:cancel:{other.id}"]
    assert len(desk.appointments) == 2

    # Choosing one still asks once more before cancelling it.
    await handler.process_callback(CONN, tap(f"appt:cancel:{other.id}"))
    assert _plain(buttons(desk.keyboards[-1])) == [f"appt:cancel_yes:{other.id}", f"appt:keep:{other.id}"]


async def test_with_two_bookings_reschedule_offers_one_button_each(
    monkeypatch: pytest.MonkeyPatch, desk, reminded
):
    """The question used to have no answer the desk could read, and repeated."""
    other = _upcoming(desk, appointment_id=6, days=3)
    _triaged_as(monkeypatch, "reschedule")

    await handler.process_message(CONN, update("quero remarcar"))

    assert _plain(buttons(desk.keyboards[-1])) == [f"appt:move:{reminded.id}", f"appt:move:{other.id}"]

    await handler.process_callback(CONN, tap(f"appt:move:{other.id}"))
    assert desk.sent[-1] == content.text("scheduling.period_question")
    assert buttons(desk.keyboards[-1])[0] == f"period:morning:none:{other.id}"


async def test_the_team_hears_about_a_new_booking(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))

    assert any(chat == TEAM and "marcou uma conversa" in text for chat, text in desk.posted)


async def test_the_team_hears_about_a_reschedule(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    desk.offers["42"] = Offer(id=5, chat_id="42", slots=OFFERED,
                              calendar_id="desk@example.com", replaces_id=9)
    # The appointment being moved still exists, is Ana's and is ahead: a
    # reschedule of anything else is refused (stale_reschedule).
    moving = Appointment(id=9, contact_id=CONTACT.id, calendar_id="desk@example.com",
                         provider="google", title="x", starts_at=_AHEAD.start, ends_at=_AHEAD.end)
    monkeypatch.setattr(appointments_repo, "get_by_id",
                        lambda conn, appointment_id: _value(moving if appointment_id == 9 else None))
    monkeypatch.setattr(appointments_repo, "cancel", lambda conn, appointment_id: _value(True))
    _triaged_as(monkeypatch, "other")

    await handler.process_message(CONN, update("a terca"))

    assert any(chat == TEAM and "remarcou a conversa" in text for chat, text in desk.posted)


# ── The video meeting link ───────────────────────────────────────────────────

async def test_the_confirmation_carries_the_meeting_link(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "online_meetings", True)
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))

    assert desk.created_events[0].online_meeting is True
    assert desk.appointments[0].meeting_url == "https://meet.google.com/evt-1"
    assert desk.sent[-1].endswith(
        content.render("scheduling.meeting_link", link="https://meet.google.com/evt-1")
    )


async def test_without_online_meetings_no_link_is_asked_for_or_said(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _triaged_as(monkeypatch, "booking", "other")

    await handler.process_message(CONN, update("marcar"))
    await handler.process_message(CONN, update("a terca"))

    assert desk.created_events[0].online_meeting is False
    assert "Google Meet" not in desk.sent[-1]


# ── A card nobody has taken yet; /painel for the team ────────────────────────


async def test_painel_in_private_gives_a_team_member_the_address(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")
    monkeypatch.setattr(contacts_repo, "find_staff_by_chat",
                        lambda conn, chat_id: _value(object()))
    _no_model(monkeypatch, "/painel needs no model")

    await handler.process_message(CONN, update("/painel"))

    assert desk.sent == [content.render("reply.panel_link", link="https://bot.example.com/painel")]


async def test_painel_from_a_customer_is_an_ordinary_message(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(contacts_repo, "find_staff_by_chat", lambda conn, chat_id: _value(None))
    _triaged_as(monkeypatch, "other")

    await handler.process_message(CONN, update("/painel"))

    assert "painel" not in desk.sent[-1].lower()


# ── Replies to automated messages ────────────────────────────────────────────

async def test_sair_stops_automated_messages_without_the_model(
    monkeypatch: pytest.MonkeyPatch, desk
):
    stopped: list[int] = []

    async def set_opt_out(conn, contact_id):
        stopped.append(contact_id)

    monkeypatch.setattr(automations_repo, "set_opt_out", set_opt_out)
    _no_model(monkeypatch, "SAIR is read by code")

    await handler.process_message(CONN, update("SAIR"))

    assert stopped == [CONTACT.id]
    assert desk.sent == [content.text("automation.opted_out")]


async def test_a_reason_tap_is_thanked(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(automations_repo, "record_reason", lambda *a: _value(True))

    await handler.process_callback(CONN, tap("why:3:avaliando"))

    assert desk.sent == [content.text("automation.reason_thanks")]


async def test_quero_remarcar_after_a_no_show_asks_the_period(monkeypatch: pytest.MonkeyPatch, desk):
    await handler.process_callback(CONN, tap("book:start"))

    assert desk.sent == [content.text("scheduling.period_question")]
    assert buttons(desk.keyboards[-1])[0] == "period:morning:none:none"


# ── The same conversation on WhatsApp ───────────────────────────────────────

WA_NUMBER = "5591999990000"


def _meta(kind: str, body: dict, mid: str = "wamid.1") -> dict:
    """One update, built the way the webhook builds it from Meta's payload."""
    value = {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PN1"},
        "contacts": [{"profile": {"name": "Ana"}, "wa_id": WA_NUMBER}],
        "messages": [{"from": WA_NUMBER, "id": mid, "type": kind, kind: body,
                      "timestamp": str(int(datetime.now(ZONE).timestamp()))}],
    }
    payload = {"entry": [{"changes": [{"field": "messages", "value": value}]}]}
    return to_updates(payload, "PN1")[0]


def wa_text(body: str, mid: str = "wamid.1") -> dict:
    return _meta("text", {"body": body}, mid)


def wa_tap(data: str, title: str = "x", mid: str = "wamid.2") -> dict:
    return _meta("interactive", {"type": "button_reply",
                                 "button_reply": {"id": data, "title": title}}, mid)


@pytest.fixture
def wa(monkeypatch: pytest.MonkeyPatch, desk):
    """The desk, answering on WhatsApp: what goes out is recorded in the desk's
    lists, with each message's button ids where a test can read them."""
    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v"}.items():
        monkeypatch.setattr(settings, name, value)
    out: dict[str, list] = {"buttons": [], "typing": [], "telegram": []}

    async def send_text(to, text):
        assert to == WA_NUMBER
        desk.sent.append(text)
        out["buttons"].append([])
        return "ok"

    async def send_buttons(to, body, rows):
        assert to == WA_NUMBER
        desk.sent.append(body)
        out["buttons"].append([data for _, data in rows])
        return "ok"

    async def send_list(to, body, label, rows):
        return await send_buttons(to, body, rows)

    async def mark_read_typing(message_id):
        out["typing"].append(message_id)
        return True

    async def telegram_send(chat_id, text, **kwargs):
        out["telegram"].append(chat_id)
        return True

    monkeypatch.setattr(whatsapp, "send_text", send_text)
    monkeypatch.setattr(whatsapp, "send_buttons", send_buttons)
    monkeypatch.setattr(whatsapp, "send_list", send_list)
    monkeypatch.setattr(whatsapp, "mark_read_typing", mark_read_typing)
    monkeypatch.setattr(telegram, "send_message", telegram_send)
    return out


async def test_a_whatsapp_greeting_is_answered_on_whatsapp(monkeypatch: pytest.MonkeyPatch, desk, wa):
    _no_model(monkeypatch, "a greeting needs no model")

    await handler.process_message(CONN, wa_text("oi"))

    assert desk.sent == [content.text("reply.welcome")]
    assert wa["telegram"] == []


_FORM = """Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Qual é a sua função na clínica?: Responsável pelo atendimento
Full name: Dave Fernandes
Phone number: +5591988887777
Qual é o nome da clínica?: Oficina da Mente
Email: dave@example.com
Qual é hoje o principal desafio no atendimento?: Perda de agendamentos"""


async def test_the_lead_form_is_answered_in_code_with_three_ways_forward(
        monkeypatch: pytest.MonkeyPatch, desk, wa):
    """Once answered by the model with "conhecer aplicações?" and a product
    list that included a construction tool, to a clinic."""
    _no_model(monkeypatch, "the lead form needs no model")
    desk.known = {}

    await handler.process_message(CONN, wa_text(_FORM))

    assert desk.sent == [
        f"{content.render('form.hello', nome='Dave')} "
        f"{content.text('form.challenge_bookings')}\n\n{content.text('form.next')}"
    ]
    assert wa["buttons"][-1] == ["info:form", "prop:meet", "prop:start"]
    # The form's e-mail and phone are kept: booking will not ask for them.
    assert desk.details == {"email": "dave@example.com", "phone": "+55 91 98888-7777"}
    assert [intent for _, _, intent in desk.recorded] == ["form", "form"]

    await handler.process_callback(CONN, wa_tap("info:form", "Como funciona"))

    assert desk.sent[-1] == content.text("form.how_it_works")


_LAWYER_FORM = """Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Full name: Carlos Mendes
Phone number: +5591977776666
Qual é o nome do escritório?: Mendes Advocacia
Email: carlos@example.com
Qual é hoje o principal desafio do escritório?: Demora para responder"""


async def test_the_lawyer_form_gets_the_legal_greeting_not_the_clinic(
        monkeypatch: pytest.MonkeyPatch, desk, wa):
    """People from the lawyer ad were greeted as a clinic."""
    _no_model(monkeypatch, "the lead form needs no model")
    desk.known = {}

    await handler.process_message(CONN, wa_text(_LAWYER_FORM))

    assert desk.sent == [
        f"{content.render('form.hello', nome='Carlos')} "
        f"{content.text('form.legal_pitch')}\n\n{content.text('form.legal_next')}"
    ]
    assert "clínica" not in desk.sent[0].lower()
    assert wa["buttons"][-1] == ["info:legal", "prop:meet", "prop:start"]
    assert desk.details == {"email": "carlos@example.com", "phone": "+55 91 97777-6666"}

    await handler.process_callback(CONN, wa_tap("info:legal", "Como funciona"))

    assert desk.sent[-1] == content.text("form.legal_how_it_works")


async def test_a_construction_form_gets_the_construction_manager(
        monkeypatch: pytest.MonkeyPatch, desk, wa):
    _no_model(monkeypatch, "the lead form needs no model")
    desk.known = {}

    await handler.process_message(CONN, wa_text(
        _LAWYER_FORM.replace("Mendes Advocacia", "Mendes Construtora")
                    .replace("escritório", "empresa")))

    assert content.text("form.construction_pitch") in desk.sent[0]
    # Still in development: no proposal offered.
    assert wa["buttons"][-1] == ["info:construction", "prop:meet"]

    await handler.process_callback(CONN, wa_tap("info:construction", "Como funciona"))

    assert desk.sent[-1] == content.text("form.construction_how_it_works")


async def test_an_unclear_form_is_noted_with_a_plain_hello(
        monkeypatch: pytest.MonkeyPatch, desk, wa):
    _no_model(monkeypatch, "the lead form needs no model")
    desk.known = {}

    await handler.process_message(CONN, wa_text(
        _LAWYER_FORM.replace("Mendes Advocacia", "Mendes Varejo")
                    .replace("escritório", "empresa")))

    assert desk.sent == [
        f"{content.render('form.hello', nome='Carlos')} {content.text('form.general')}"]
    assert desk.details == {"email": "carlos@example.com", "phone": "+55 91 97777-6666"}
    assert [intent for _, _, intent in desk.recorded] == ["form", "form"]


async def test_the_proposal_buttons_on_whatsapp(monkeypatch: pytest.MonkeyPatch, desk, wa):
    """Under the proposal PDF: "Tenho dúvidas" is the person writing to the
    assistant; "Aceitar proposta" is answered in code and the team is told."""
    from core.services import inbox as inbox_service
    told: list[str] = []

    async def notify_team(text, contact_id):
        told.append(text)
        return True
    monkeypatch.setattr(inbox_service, "notify_team", notify_team)
    monkeypatch.setattr(inbox_service, "push_to_team", lambda text, contact: None)
    heard: list[str] = []

    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        heard.append(body)
        return (Triage(intent="faq", confidence=0.9), AgentReply(text="Claro.", intent="faq"))
    monkeypatch.setattr(handler, "route", fake_route)

    await handler.process_callback(CONN, wa_tap("propdoc:doubt", "Tenho dúvidas"))
    assert heard == [content.text("proposal_doc.doubt")] and desk.sent[-1] == "Claro."

    await handler.process_callback(CONN, wa_tap("propdoc:accept", "Aceitar proposta", "wamid.3"))
    assert desk.sent[-1] == content.text("proposal_doc.accept_reply")
    assert told == [content.render("proposal_doc.team_accept", nome="Ana")]


async def test_accepting_the_proposal_while_the_sender_holds_the_conversation(
        monkeypatch: pytest.MonkeyPatch, desk, wa):
    """Sending the PDF needs the conversation held, and it stays held: the
    tap still gets its answer and the team its notice, and the person holding
    it sees the tap."""
    from core.services import inbox as inbox_service
    told: list[str] = []

    async def notify_team(text, contact_id):
        told.append(text)
        return True
    monkeypatch.setattr(inbox_service, "notify_team", notify_team)
    monkeypatch.setattr(inbox_service, "push_to_team", lambda text, contact: None)
    relayed: list[str] = []

    async def held(conn, chat_id):
        return True

    async def relay(conn, contact, chat_id, text):
        relayed.append(text)
        return "claimed"
    monkeypatch.setattr(handler, "_held", held)
    monkeypatch.setattr(handoff, "handle_customer_message", relay)

    await handler.process_callback(CONN, wa_tap("propdoc:decline", "Não tenho interesse"))

    assert desk.sent[-1] == content.text("proposal_doc.decline_reply")
    assert told == [content.render("proposal_doc.team_decline", nome="Ana")]
    assert relayed == [f"(tocou no botão) {content.text('proposal_doc.button_decline')}"]


async def test_booking_on_whatsapp_with_reply_buttons(monkeypatch: pytest.MonkeyPatch, desk, wa):
    from core.services import forms

    # Without a published booking Flow: the times as reply buttons.
    monkeypatch.setattr(forms, "_flow_ids", dict)
    forms.reset()

    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        return (Triage(intent="booking", confidence=0.9, preferred_period=None),
                AgentReply(text="", intent="booking"))
    monkeypatch.setattr(handler, "route", fake_route)

    await handler.process_message(CONN, wa_text("queria marcar uma conversa"))
    assert wa["buttons"][-1] == [
        "period:morning:none:none", "period:afternoon:none:none", "period:any:none:none",
    ]

    await handler.process_callback(CONN, wa_tap("period:any:none:none", "Tanto faz"))
    assert wa["buttons"][-1] == ["slot:1:0", "slot:1:1", "slot:1:2"]

    await handler.process_callback(CONN, wa_tap("slot:1:0", "ter 14/10 10:00", mid="wamid.3"))
    assert len(desk.appointments) == 1
    assert "está marcada" in desk.sent[-1]
    assert wa["telegram"] == []


async def test_cancel_on_whatsapp_asks_first(monkeypatch: pytest.MonkeyPatch, desk, wa, reminded):
    _triaged_as(monkeypatch, "cancel")

    await handler.process_message(CONN, wa_text("quero cancelar"))

    assert _plain(wa["buttons"][-1]) == [f"appt:cancel_yes:{reminded.id}", f"appt:keep:{reminded.id}"]


async def test_a_whatsapp_voice_note_is_transcribed(monkeypatch: pytest.MonkeyPatch, desk, wa):
    heard: list[tuple] = []

    async def download_media(media_id, max_bytes):
        heard.append((media_id, max_bytes))
        return b"OGG", "audio/ogg"

    async def transcribe(data, mime=None, prompt=None):
        heard.append((data, mime))
        return "oi"

    monkeypatch.setattr(whatsapp, "download_media", download_media)
    monkeypatch.setattr(handler.audio, "transcribe", transcribe)
    _no_model(monkeypatch, "a greeting needs no model")

    await handler.process_message(CONN, _meta("audio", {"id": "m1", "mime_type": "audio/ogg"}))

    assert heard == [("m1", settings.max_voice_bytes), (b"OGG", "audio/ogg")]
    assert desk.sent == [content.text("reply.welcome")]


async def test_a_big_whatsapp_voice_note_is_unreadable(monkeypatch: pytest.MonkeyPatch, desk, wa):
    async def too_big(media_id, max_bytes):
        return None

    monkeypatch.setattr(whatsapp, "download_media", too_big)

    await handler.process_message(CONN, _meta("audio", {"id": "m1", "mime_type": "audio/ogg"}))

    assert desk.sent == [handler._CANNOT_READ]


async def test_the_bot_types_on_whatsapp_by_marking_read(monkeypatch: pytest.MonkeyPatch, desk, wa):
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, wa_text("quanto custa?", mid="wamid.77"))

    assert wa["typing"] == ["wamid.77"]



# ── WhatsApp: when the customer wrote, and what a late message gets ─────────

def _old(update: dict, hours: float) -> dict:
    """The same update, as if Meta had stamped it `hours` ago."""
    stamp = int((datetime.now(ZONE) - timedelta(hours=hours)).timestamp())
    item = update.get("message") or update.get("callback_query")
    assert item is not None
    item["date"] = stamp
    return update


async def test_every_inbound_update_says_when_the_customer_wrote(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    _no_model(monkeypatch, "a greeting needs no model")

    await handler.process_message(CONN, _old(wa_text("oi"), 2))

    when = desk.inbound[-1]
    assert when is not None
    assert abs((datetime.now(ZONE) - when).total_seconds() - 2 * 3600) < 60


async def test_an_unreadable_whatsapp_message_still_counts_as_the_customer_writing(desk, wa):
    await handler.process_message(CONN, _meta("image", {"id": "img1"}))

    assert desk.inbound and desk.inbound[-1] is not None
    assert ("user", content.text("inbox.unreadable"), "unreadable") in desk.recorded
    assert desk.sent == [handler._CANNOT_READ]


async def test_a_whatsapp_message_older_than_a_day_gets_no_bot_reply(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    _no_model(monkeypatch, "an expired message is not triaged")

    assert await handler.process_message(CONN, _old(wa_text("oi, ainda dá?"), 25)) == "expired"

    assert desk.sent == []
    assert ("user", "oi, ainda dá?", "expired") in desk.recorded
    assert desk.inbound[-1] is not None


async def test_an_old_whatsapp_tap_gets_no_bot_reply(desk, wa):
    assert await handler.process_callback(CONN, _old(wa_tap("period:any:none:none", "Tanto faz"), 25)) == "expired"

    assert desk.sent == [] and desk.offer_calls == 0
    assert ("user", "(tocou no botão) Tanto faz", "expired") in desk.recorded


async def test_an_old_telegram_message_is_still_answered(monkeypatch: pytest.MonkeyPatch, desk):
    _no_model(monkeypatch, "a greeting needs no model")

    await handler.process_message(CONN, _old(update("oi"), 25))

    assert desk.sent == [content.text("reply.welcome")]


async def test_the_panel_address_goes_to_telegram_directly(monkeypatch: pytest.MonkeyPatch, desk):
    """A team member's /painel is team business: it does not go through the
    door to customers."""
    from core import channels

    async def not_for_the_team(*args, **kwargs):
        raise AssertionError("the panel address is not a message to a customer")

    monkeypatch.setattr(channels, "send", not_for_the_team)
    monkeypatch.setattr(contacts_repo, "find_staff_by_chat", lambda conn, chat_id: _value(object()))
    _no_model(monkeypatch, "/painel needs no model")

    assert await handler.process_message(CONN, update("/painel")) == "panel_link"
    assert desk.sent == [handler._panel_address()]


def _stale_on_whatsapp() -> StaleSession:
    return StaleSession(
        session=HandoffSession(
            id=1, contact_id=7, chat_id=f"wa:{WA_NUMBER}", team_chat_id=TEAM, reason="painel",
            status="claimed", claimed_by_name="Victor", claimed_by_user_id=2, relay_chat_id=TEAM,
        ),
        contact_name="Ana",
    )


async def test_a_timeout_past_the_whatsapp_window_only_tells_the_team(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    monkeypatch.setattr(handoffs_repo, "stale_sessions",
                        lambda conn, minutes: _value([_stale_on_whatsapp()]))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, sid, minutes: _value(True))
    monkeypatch.setattr(inbox_repo, "window_open", lambda conn, contact_id: _value(False))

    assert await handoff.sweep_timeouts(CONN) == 1

    assert desk.sent == [] and desk.recorded == []
    assert (TEAM, content.render("handoff.timeout_claimed", nome="Ana", atendente="Victor")) in desk.posted


async def test_a_timeout_notice_that_did_not_go_out_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    stale = StaleSession(
        session=HandoffSession(
            id=1, contact_id=7, chat_id="42", team_chat_id=TEAM, reason="pedido",
            status="claimed", claimed_by_id=777, claimed_by_name="Victor", relay_chat_id="777",
        ),
        contact_name="Ana",
    )
    monkeypatch.setattr(handoffs_repo, "stale_sessions", lambda conn, minutes: _value([stale]))
    monkeypatch.setattr(handoffs_repo, "close_if_idle", lambda conn, sid, minutes: _value(True))
    monkeypatch.setattr(telegram, "send_message", lambda *a, **k: _value(False))

    assert await handoff.sweep_timeouts(CONN) == 1

    assert desk.recorded == []


async def test_expired_messages_do_not_use_up_the_message_limit(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    """After an outage, old messages must not spend the limit a current one
    then runs into."""
    counted: list = []

    async def allowed(chat_id):
        counted.append(chat_id)
        return True, False

    monkeypatch.setattr(handler, "_allowed", allowed)

    await handler.process_message(CONN, _old(wa_text("antiga"), 25))
    await handler.process_callback(CONN, _old(wa_tap("period:any:none:none", "Tanto faz"), 25))

    assert counted == []


async def test_a_rate_limited_whatsapp_message_still_moves_the_window(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    monkeypatch.setattr(handler, "_allowed", lambda chat_id: _value((False, True)))

    await handler.process_message(CONN, wa_text("oi de novo"))
    await handler.process_callback(CONN, wa_tap("period:any:none:none", "Tanto faz"))

    assert len(desk.inbound) == 2 and all(desk.inbound)


async def test_id_on_whatsapp_still_moves_the_window(desk, wa):
    assert await handler.process_message(CONN, wa_text("/id")) == "own_id"

    assert desk.inbound and desk.inbound[-1] is not None


async def test_a_first_unreadable_message_still_announces_the_conversation(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """A new customer whose first message is a photo: the team hears of the
    conversation then — the message is on record, so a later one would not."""
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    desk.new_conversation = True
    photo = {"update_id": 1, "message": {"chat": {"id": 42}, "photo": [{"file_id": "x"}],
                                         "from": {"first_name": "Ana"}}}

    await handler.process_message(CONN, photo)

    assert desk.posted == [(TEAM, content.render("inbox.new_conversation", nome="Ana", canal="Telegram"))]


async def test_the_panel_address_answers_on_whatsapp_to_a_team_member(
    monkeypatch: pytest.MonkeyPatch, desk, wa
):
    """A team member who asks for /painel on WhatsApp is answered there: the
    Telegram-only send would go nowhere for a WhatsApp number."""
    monkeypatch.setattr(contacts_repo, "find_staff_by_chat", lambda conn, chat_id: _value(object()))
    _no_model(monkeypatch, "/painel needs no model")

    assert await handler.process_message(CONN, wa_text("/painel")) == "panel_link"

    assert desk.sent == [handler._panel_address()]
    assert wa["telegram"] == []


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


# ── Blocked in the panel ────────────────────────────────────────────────────

def _blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contacts_repo, "is_blocked", lambda conn, chat_id: _value(True))
    _no_model(monkeypatch, "a blocked contact reaches no model")


async def test_a_blocked_contact_gets_no_answer_and_nobody_is_told(
        monkeypatch: pytest.MonkeyPatch, desk):
    _blocked(monkeypatch)
    monkeypatch.setattr(settings, "team_chat_id", TEAM)
    desk.new_conversation = True

    assert await handler.process_message(CONN, update("me responde!")) == "blocked"

    assert desk.sent == [] and desk.posted == []
    assert ("user", "me responde!", "blocked") in desk.recorded, "kept under Bloqueadas"


async def test_a_blocked_contacts_tap_does_nothing(monkeypatch: pytest.MonkeyPatch, desk):
    _blocked(monkeypatch)

    assert await handler.process_callback(CONN, tap("prop:start")) == "blocked"

    assert desk.sent == [] and desk.recorded == []
