"""BOOKING_MODE=choose_day: the list of days, the day's times, and every way in.

Driven through process_message and process_callback with the Desk of
tests/test_desk_flow.py: the offer store, the slot interpreter and the booking
rules run for real; the full availability is a fixed list relative to today."""
# The Desk fixture is imported from test_desk_flow and taken as a parameter:
# that is how pytest shares it, and ruff reads each use as a redefinition.
# ruff: noqa: F811
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import content
from core.config import settings
from core.handlers import message as handler
from core.integrations import workspace
from core.integrations.workspace.models import TimeSlot
from core.models import AgentReply, Appointment, Intent, Triage
from core.services import booking_lists as lists
from core.services import scheduling
from tests.test_desk_flow import CONN, _triaged_as, buttons, desk, tap, update  # noqa: F401

ZONE = ZoneInfo(settings.office_timezone)
TODAY = datetime.now(ZONE).date()



def _waiting(desk) -> str | None:
    """The wait for a typed day without the minute it began."""
    if desk.pending and desk.pending.startswith("pickday:"):
        return desk.pending.rsplit(":", 1)[0]
    return desk.pending

def _ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def _next(weekday: int) -> date:
    """The next given ISO weekday after today, as the desk reads "quinta"."""
    return TODAY + timedelta(days=(weekday - TODAY.isoweekday()) % 7 or 7)


def _third_day() -> date:
    """A near day other than the next Thursday and Friday: on a Tuesday or
    Wednesday, two days ahead is one of them, and that day would get six times."""
    named = {_next(4), _next(5)}
    return next(_ahead(n) for n in (2, 3, 4, 1) if _ahead(n) not in named)


def _at(day: date, hour: int, minute: int = 0) -> TimeSlot:
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


class Availability:
    def __init__(self):
        self.starts: list[TimeSlot] = []
        self.periods: list[str | None] = []
        self.fail = False

    async def __call__(self, *, period=None, calendars=None, now=None):
        if self.fail:
            raise workspace.WorkspaceError("Google GET falhou (503)", status=503)
        self.periods.append(period)
        def keep(slot: TimeSlot) -> bool:
            hour = slot.start.astimezone(ZONE).hour
            return {"morning": hour < 12, "afternoon": hour >= 12}.get(period or "", True)
        return [s for s in self.starts if keep(s)]


@pytest.fixture
def choose(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "booking_mode", "choose_day")
    available = Availability()
    for day in (_next(4), _next(5), _third_day()):
        available.starts += [_at(day, 9), _at(day, 10), _at(day, 15)]
    available.starts.sort(key=lambda s: s.start)
    monkeypatch.setattr(scheduling, "available_starts", available)
    return available


def _days_on(keyboard) -> list[date]:
    found = [lists.parse(b) for b in buttons(keyboard)]
    return [nav.day for nav in found if nav and nav.kind == "day"]


def _route(monkeypatch, intent: Intent = "booking", period=None, preferred_day=None):
    async def route(body, chat_id, history_text="", organization="", retrieve=None):
        text = "" if intent in ("booking", "reschedule") else "Claro."
        return (Triage(intent=intent, confidence=0.9, preferred_period=period,
                       preferred_day=preferred_day),
                AgentReply(text=text, intent=intent))
    monkeypatch.setattr(handler, "route", route)


# ── The ways in ──────────────────────────────────────────────────────────────

async def test_a_booking_shows_the_days_without_asking_the_period(monkeypatch, desk, choose):
    _route(monkeypatch, "booking", period=None)
    await handler.process_message(CONN, update("quero marcar uma conversa"))

    assert desk.sent[-1] == content.text("booking.days_prompt")
    assert sorted(_days_on(desk.keyboards[-1])) == sorted({s.start.astimezone(ZONE).date()
                                                          for s in choose.starts})
    assert _waiting(desk) == "pickday:-:-"


async def test_a_stated_period_filters_the_days(monkeypatch, desk, choose):
    _route(monkeypatch, "booking", period="afternoon")
    await handler.process_message(CONN, update("quero marcar à tarde"))
    assert choose.periods[-1] == "afternoon" and _waiting(desk) == "pickday:t:-"


async def test_a_named_day_goes_straight_to_its_times(monkeypatch, desk, choose):
    _route(monkeypatch, "booking", period=None)
    await handler.process_message(CONN, update("quero marcar quinta"))

    thursday = _next(4)
    assert desk.sent[-1] == content.render("booking.times_prompt",
                                           **_day_parts(thursday))
    offer = desk.offers["42"]
    assert [s.start.astimezone(ZONE).date() for s in offer.slots] == [thursday] * 3


async def test_a_refused_day_after_triage_is_never_the_preferred_day(monkeypatch, desk, choose):
    _route(monkeypatch, "booking", period=None)
    await handler.process_message(CONN, update("quero marcar, mas quinta não posso"))
    assert desk.sent[-1] == content.text("booking.days_prompt")


async def test_book_start_and_reschedule_taps_go_to_the_days(monkeypatch, desk, choose):
    from core.repositories import appointments as appointments_repo

    async def get_by_id(conn, appointment_id):
        return next((a for a in desk.appointments if a.id == appointment_id), None)
    monkeypatch.setattr(appointments_repo, "get_by_id", get_by_id)
    await handler.process_callback(CONN, tap("book:start"))
    assert desk.sent[-1] == content.text("booking.days_prompt")

    desk.appointments.append(Appointment(
        id=5, contact_id=7, calendar_id="desk@example.com", provider="google",
        provider_event_id="old", title="x", starts_at=_at(_ahead(9), 9).start,
        ends_at=_at(_ahead(9), 9).end))
    await handler.process_callback(CONN, tap("appt:move:5"))
    assert desk.sent[-1] == content.text("booking.days_prompt")
    assert _waiting(desk) == "pickday:-:5"
    navs = [lists.parse(b) for b in buttons(desk.keyboards[-1])]
    assert all(n.replaces == 5 for n in navs if n)


async def test_an_old_period_tap_goes_to_the_days_with_that_period(desk, choose):
    await handler.process_callback(CONN, tap("period:morning:none:none"))
    assert desk.sent[-1] == content.text("booking.days_prompt")
    assert _waiting(desk) == "pickday:m:-"


# ── Days, times, and what an old list does ───────────────────────────────────

async def test_a_day_tap_offers_its_times_and_a_time_tap_books(desk, choose):
    thursday = _next(4)
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", thursday, None, None, None))))

    offer = desk.offers["42"]
    assert len(offer.slots) == 3 and offer.period is None
    assert buttons(desk.keyboards[-1])[:3] == [f"slot:{offer.id}:{i}" for i in range(3)]
    assert desk.pending is None
    await handler.process_callback(CONN, tap(f"slot:{offer.id}:1"))
    assert len(desk.appointments) == 1
    assert desk.appointments[0].starts_at == _at(thursday, 10).start


async def test_other_day_expires_the_times_and_an_old_time_does_not_book(desk, choose):
    thursday = _next(4)
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", thursday, None, None, None))))
    offer_id = desk.offers["42"].id
    other_day = [b for b in buttons(desk.keyboards[-1]) if b.startswith("days:")][-1]
    await handler.process_callback(CONN, tap(other_day))
    assert "42" not in desk.offers
    await handler.process_callback(CONN, tap(f"slot:{offer_id}:0"))
    assert desk.appointments == [] and desk.sent[-1] == content.text("scheduling.slot_taken")


@pytest.mark.parametrize("day", [TODAY - timedelta(days=1), TODAY + timedelta(days=400)])
async def test_a_day_outside_the_horizon_gives_the_days_of_today(desk, choose, day):
    await handler.process_callback(CONN, tap(lists.callback(lists.Nav("day", day, None, None,
                                                                      None))))
    assert desk.sent[-1] == content.text("booking.days_prompt")


async def test_a_day_that_filled_up_says_so_and_lists_the_days(desk, choose):
    thursday = _next(4)
    choose.starts = [s for s in choose.starts if s.start.astimezone(ZONE).date() != thursday]
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", thursday, None, None, None))))
    assert desk.sent[-1].startswith(content.text("booking.day_full"))
    assert thursday not in _days_on(desk.keyboards[-1])


async def test_a_calendar_failure_on_a_day_says_it_could_not(desk, choose):
    """Review Focus 2: not "no times"."""
    choose.fail = True
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(4), None, None, None))))
    assert desk.sent[-1] == content.text("scheduling.could_not_book")


@pytest.mark.parametrize("data", ["day:lixo", "days:2026-99-99:-:-", "more:x"])
async def test_a_malformed_tap_gets_the_days_never_an_error(desk, choose, data):
    """Review Focus 4."""
    await handler.process_callback(CONN, tap(data))
    assert desk.sent[-1] == content.text("booking.days_prompt")


async def test_more_times_pages_the_day(desk, choose):
    thursday = _next(4)
    choose.starts = [TimeSlot(start=_at(thursday, 9).start + timedelta(minutes=30 * i),
                              end=_at(thursday, 9).end + timedelta(minutes=30 * i))
                     for i in range(12)]
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", thursday, None, None, None))))
    first = desk.keyboards[-1]
    assert len([b for b in buttons(first) if b.startswith("slot:")]) == 8
    more = next(b for b in buttons(first) if b.startswith("more:"))
    await handler.process_callback(CONN, tap(more))
    assert len(desk.offers["42"].slots) == 4


# ── Typing while the days are open ───────────────────────────────────────────

async def _open_days(monkeypatch, replaces_tap: str | None = None):
    _route(monkeypatch, "booking", period=None)
    await handler.process_message(CONN, update("quero marcar"))


async def test_typing_a_day_shows_its_times(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    await handler.process_message(CONN, update("quinta"))
    assert [s.start.astimezone(ZONE).date() for s in desk.offers["42"].slots] == [_next(4)] * 3


async def test_typing_two_days_lists_only_those(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    await handler.process_message(CONN, update("quinta ou sexta"))
    assert desk.sent[-1] == content.text("booking.days_which")
    assert sorted(_days_on(desk.keyboards[-1])) == sorted([_next(4), _next(5)])


async def test_a_mixed_answer_asks_for_a_tap(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    await handler.process_message(CONN, update("quinta não posso"))
    assert desk.sent[-1].startswith(content.text("booking.days_tap"))
    assert _waiting(desk) == "pickday:-:-" and "42" not in desk.offers


async def test_only_a_period_changes_the_filter_and_keeps_the_wait(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    await handler.process_message(CONN, update("de tarde"))
    assert _waiting(desk) == "pickday:t:-" and choose.periods[-1] == "afternoon"


async def test_anything_else_ends_the_wait_and_goes_to_triage(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    _route(monkeypatch, "faq")
    await handler.process_message(CONN, update("obrigado"))
    assert desk.pending is None and desk.sent[-1] == "Claro."


async def test_a_typed_time_books_from_the_current_page(monkeypatch, desk, choose):
    await _open_days(monkeypatch)
    await handler.process_message(CONN, update("quinta"))
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("às 10h"))
    assert len(desk.appointments) == 1
    assert desk.appointments[0].starts_at == _at(_next(4), 10).start


async def test_a_time_taken_meanwhile_goes_back_to_the_days_with_the_context(monkeypatch, desk,
                                                                             choose):
    thursday = _next(4)
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", thursday, "morning", None, None))))
    offer = desk.offers["42"]
    desk.busy = [offer.slots[0]]  # someone else took it before the tap
    await handler.process_callback(CONN, tap(f"slot:{offer.id}:0"))
    assert desk.appointments == []
    assert _waiting(desk) == "pickday:m:-"


def _day_parts(day: date) -> dict:
    title = lists.day_title(day)
    weekday, _, when = title.partition(", ")
    return {"dia_semana": weekday.lower(), "data": when}


# ── The limit, in either mode ────────────────────────────────────────────────

def _upcoming(desk, *ids: int):
    for appointment_id in ids:
        start = datetime.now(ZONE) + timedelta(days=3 + appointment_id)
        desk.appointments.append(Appointment(
            id=appointment_id, contact_id=7, calendar_id="desk@example.com", provider="google",
            provider_event_id=f"ev{appointment_id}", title="x", starts_at=start,
            ends_at=start + timedelta(minutes=30)))


async def test_at_the_limit_a_new_booking_offers_to_move_the_one_held(monkeypatch, desk):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 1)
    _upcoming(desk, 1)
    _triaged_as(monkeypatch, "booking")
    await handler.process_message(CONN, update("quero marcar"))

    assert desk.sent[-1].startswith("Você já tem uma conversa marcada para")
    assert _plain(buttons(desk.keyboards[-1])) == ["appt:move:1", "limit:keep"]
    await handler.process_callback(CONN, tap("limit:keep"))
    assert desk.sent[-1] == content.text("booking.limit_kept")


async def test_at_the_limit_with_two_held_it_asks_which_to_move(monkeypatch, desk):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 2)
    _upcoming(desk, 1, 2)
    _triaged_as(monkeypatch, "booking")
    await handler.process_message(CONN, update("quero marcar"))

    assert desk.sent[-1].startswith("Você já tem 2 conversas marcadas")
    assert _plain(buttons(desk.keyboards[-1])) == ["appt:move:1", "appt:move:2", "limit:keep"]


async def test_below_the_limit_the_offer_goes_on(monkeypatch, desk):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 2)
    _upcoming(desk, 1)
    _triaged_as(monkeypatch, "booking")
    await handler.process_message(CONN, update("quero marcar"))
    assert not desk.sent[-1].startswith("Você já tem")


# ── Review fixes ─────────────────────────────────────────────────────────────

async def test_a_failed_switch_of_day_leaves_no_old_times_to_book(monkeypatch, desk, choose):
    """Codex r1: Thursday's times open, a tap on Friday fails at the calendar,
    then "às 10h" must not book Thursday."""
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(4), None, None, None))))
    choose.fail = True
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(5), None, None, None))))
    assert "42" not in desk.offers
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("às 10h"))
    assert desk.appointments == []


async def test_the_named_days_survive_the_contact_details_question(monkeypatch, desk, choose):
    """Codex r1: asked for e-mail and phone first, the list after them is
    still only the days the person named."""
    desk.known = {}
    choose.starts.append(_at(_next(1), 9))  # a Monday the person did not name
    _route(monkeypatch, "booking", period=None)
    await handler.process_message(CONN, update("quero marcar quinta ou sexta"))
    assert desk.pending and desk.pending.startswith("booking:")
    await handler.process_message(CONN, update("ana@example.com 91 98888-7777"))
    assert sorted(_days_on(desk.keyboards[-1])) == sorted([_next(4), _next(5)])


async def test_an_old_wait_for_a_day_lets_the_message_through(monkeypatch, desk, choose):
    """Reviewer: days later, "hoje não vou conseguir ir" is not an answer to
    the list of days."""
    old = int(datetime.now(ZONE).timestamp() // 60) - settings.offer_ttl_minutes - 5
    desk.pending = f"pickday:-:-:{old}"
    _route(monkeypatch, "faq")
    await handler.process_message(CONN, update("hoje não vou conseguir ir"))
    assert desk.sent[-1] == "Claro." and desk.pending is None


async def test_writing_another_day_with_times_open_goes_to_that_day(monkeypatch, desk, choose):
    """Reviewer: with Thursday's times open, "sexta" shows Friday's times."""
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(4), None, None, None))))
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("sexta"))
    assert {s.start.astimezone(ZONE).date() for s in desk.offers["42"].slots} == {_next(5)}


async def test_suggest_mode_takes_the_first_of_several_named_days(monkeypatch, desk):
    """Reviewer: in suggest mode the list of named days is not used; the first
    one is the preferred day, as before."""
    offered: list = []

    async def offer_times(**kwargs):
        offered.append(kwargs.get("preferred_day"))
        return []

    monkeypatch.setattr(scheduling, "offer_times", offer_times)
    _route(monkeypatch, "booking", period="any")
    await handler.process_message(CONN, update("quero marcar quinta ou sexta"))
    assert offered == [_next(4)]


async def test_four_named_days_survive_the_contact_details_question(monkeypatch, desk, choose):
    """Codex r2: the restriction is not dropped above three days."""
    desk.known = {}
    named = [_ahead(n) for n in (8, 9, 10, 11)]
    for day in [*named, _ahead(12)]:
        choose.starts.append(_at(day, 9))
    _route(monkeypatch, "booking", period=None)
    said = ", ".join(f"{d:%d/%m}" for d in named[:3]) + f" ou {named[3]:%d/%m}"
    await handler.process_message(CONN, update(f"quero marcar {said}"))
    await handler.process_message(CONN, update("ana@example.com 91 98888-7777"))
    assert sorted(_days_on(desk.keyboards[-1])) == named


async def test_another_day_written_with_a_period_uses_that_period(monkeypatch, desk, choose):
    """Codex r2: with Thursday morning open, "sexta à tarde" is Friday afternoon."""
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(4), "morning", None, None))))
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("sexta à tarde"))
    shown = [s.start.astimezone(ZONE) for s in desk.offers["42"].slots]
    assert shown and {t.date() for t in shown} == {_next(5)} and all(t.hour >= 12 for t in shown)


async def _thursday_morning_open() -> None:
    await handler.process_callback(
        CONN, tap(lists.callback(lists.Nav("day", _next(4), "morning", None, None))))


@pytest.mark.parametrize("text", ["quinta à tarde", "de tarde"])
async def test_a_new_period_for_the_open_day_shows_that_period(monkeypatch, desk, choose, text):
    """Codex r3: with Thursday morning open, the afternoon of the same day."""
    await _thursday_morning_open()
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update(text))
    shown = [s.start.astimezone(ZONE) for s in desk.offers["42"].slots]
    assert shown and {t.date() for t in shown} == {_next(4)} and all(t.hour >= 12 for t in shown)


async def test_several_days_with_a_period_list_only_those(monkeypatch, desk, choose):
    """Codex r3: "sexta ou segunda à tarde" lists Friday and Monday afternoons."""
    choose.starts.append(_at(_next(1), 15))
    choose.starts.append(_at(_ahead(9), 15))  # a day nobody named
    await _thursday_morning_open()
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("sexta ou segunda à tarde"))
    assert set(_days_on(desk.keyboards[-1])) == {_next(5), _next(1)}
    assert choose.periods[-1] == "afternoon"


@pytest.mark.parametrize("text", ["qualquer horário", "quinta, qualquer horário"])
async def test_any_time_lifts_the_period_of_the_open_day(monkeypatch, desk, choose, text):
    """Codex r4: with Thursday morning open, "qualquer horário" shows the whole day."""
    await _thursday_morning_open()
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update(text))
    shown = [s.start.astimezone(ZONE) for s in desk.offers["42"].slots]
    assert {t.date() for t in shown} == {_next(4)} and any(t.hour >= 12 for t in shown)
    assert desk.offers["42"].period is None


async def test_any_time_lifts_the_period_even_with_one_time_on_the_page(monkeypatch, desk,
                                                                        choose):
    """Codex r5: a morning page with only 9h; "quinta, qualquer horário" shows
    the whole day instead of booking 9h."""
    thursday = _next(4)
    choose.starts = [s for s in choose.starts
                     if s.start.astimezone(ZONE).date() != thursday] + [
        _at(thursday, 9), _at(thursday, 15)]
    await _thursday_morning_open()
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("quinta, qualquer horário"))
    assert not desk.appointments
    assert {s.start.astimezone(ZONE).hour for s in desk.offers["42"].slots} == {9, 15}


async def test_a_named_time_with_any_time_still_books_it(monkeypatch, desk, choose):
    """The lift does not override a time the person named."""
    await _thursday_morning_open()
    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("às 10h, qualquer horário"))
    assert desk.appointments and desk.appointments[0].starts_at == _at(_next(4), 10).start


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
