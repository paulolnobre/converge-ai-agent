"""Taking the booking: the turn after the offer.

The offer is not a hold, so everything here is about the gap between "here are
three times" and "the second one, please" — who else took the slot in between,
and what the desk is left holding when something fails halfway.
"""
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.models import CalendarEvent, TimeSlot
from core.models import Appointment, Contact, Offer
from core.repositories import appointments as appointments_repo
from core.repositories import audit
from core.repositories import offers as offers_repo
from core.services import scheduling

ZONE = ZoneInfo(settings.office_timezone)
CONN = object()  # every repository call is stubbed; nothing reaches Postgres
CONTACT = Contact(id=7, chat_id="42", name="Ana")


def slot(day: int = 15, hour: int = 14) -> TimeSlot:
    start = datetime(2026, 9, day, hour, 0, tzinfo=ZONE)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


class FakeCalendar:
    """A calendar that records what was asked of it, in order."""

    def __init__(self, busy: list[TimeSlot] | None = None, fail_create: bool = False):
        self.busy = busy or []
        self.fail_create = fail_create
        self.created: list[CalendarEvent] = []
        self.cancelled: list[str] = []
        self.calls: list[str] = []

    async def busy_periods(self, calendar_ids, start, end):
        self.calls.append("busy")
        return {name: list(self.busy) for name in calendar_ids}

    async def create_event(self, calendar_id, event: CalendarEvent) -> CalendarEvent:
        self.calls.append("create")
        if self.fail_create:
            raise workspace.WorkspaceError("calendar refused")
        self.created.append(event)
        return event.model_copy(update={"id": f"evt{len(self.created)}"})

    async def cancel_event(self, calendar_id, event_id) -> bool:
        self.calls.append("cancel")
        self.cancelled.append(event_id)
        return True


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch):
    """A configured one-calendar office with every repository stubbed out."""
    monkeypatch.setattr(settings, "reception_calendar_id", "desk@example.com")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    monkeypatch.setattr(
        workspace, "configuration_report",
        lambda: {"capabilities": {"calendar": "google"}},
    )

    written: list[str] = []

    async def fake_audit(conn, **kwargs):
        written.append(f"{kwargs['action']}:{'ok' if kwargs['succeeded'] else 'fail'}")

    monkeypatch.setattr(audit, "record", fake_audit)
    return written


def _recorded(monkeypatch: pytest.MonkeyPatch, result: Appointment | None):
    """What the appointments table does with the row book() tries to write."""
    saved: list[Appointment] = []

    async def fake_record(conn, appointment: Appointment):
        saved.append(appointment)
        return result

    monkeypatch.setattr(appointments_repo, "record", fake_record)
    return saved


def _booked(appointment_id: int = 1) -> Appointment:
    return Appointment(
        id=appointment_id, contact_id=7, calendar_id="desk@example.com",
        provider="google", provider_event_id="evt1", title="Atendimento: Ana",
        starts_at=slot().start, ends_at=slot().end,
    )


def _with_calendar(monkeypatch: pytest.MonkeyPatch, fake: FakeCalendar):
    monkeypatch.setattr(workspace, "calendar", lambda: fake)
    return fake


# ── The slot is re-checked at the moment of booking ──────────────────────────

async def test_a_slot_taken_since_the_offer_is_not_booked(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Half an hour can pass between the offer and the answer."""
    calendar = _with_calendar(monkeypatch, FakeCalendar(busy=[slot()]))
    saved = _recorded(monkeypatch, _booked())

    result = await scheduling.book(
        CONN, contact=CONTACT, slot=slot(), title="Atendimento: Ana"
    )
    assert result.outcome == "taken"
    assert calendar.created == [] and saved == []


async def test_the_calendar_is_read_before_it_is_written(
    monkeypatch: pytest.MonkeyPatch, desk
):
    calendar = _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())

    await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="Atendimento")
    assert calendar.calls == ["busy", "create"]


async def test_a_calendar_that_cannot_be_read_does_not_book_blind(
    monkeypatch: pytest.MonkeyPatch, desk
):
    class Unreachable(FakeCalendar):
        async def busy_periods(self, calendar_ids, start, end):
            raise workspace.WorkspaceError("timeout")

    calendar = _with_calendar(monkeypatch, Unreachable())
    saved = _recorded(monkeypatch, _booked())

    result = await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="x")
    assert result.outcome == "failed"
    assert calendar.created == [] and saved == []


# ── The calendar is written before the database ──────────────────────────────

async def test_a_refused_calendar_write_leaves_no_appointment_row(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """A row claiming an appointment nobody's diary knows about is the worst
    outcome available, so it must not be reachable."""
    _with_calendar(monkeypatch, FakeCalendar(fail_create=True))
    saved = _recorded(monkeypatch, _booked())

    result = await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="x")
    assert result.outcome == "failed"
    assert result.appointment is None and saved == []
    assert "create_event:fail" in desk


async def test_a_booking_that_succeeds_returns_the_appointment(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())

    result = await scheduling.book(
        CONN, contact=CONTACT, slot=slot(), title="Atendimento: Ana"
    )
    assert result.outcome == "booked"
    assert result.appointment is not None and result.appointment.id == 1
    assert "create_event:ok" in desk


# ── Two people racing for the last slot ──────────────────────────────────────

async def test_losing_the_race_withdraws_the_event_it_created(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Both read the calendar as free; the row decides. The loser must not
    leave an event blocking a slot that no appointment row claims."""
    calendar = _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, None)  # the unique slot index rejected the row

    result = await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="x")
    assert result.outcome == "taken"
    assert result.appointment is None
    assert calendar.cancelled == ["evt1"]


async def test_an_orphan_event_that_cannot_be_withdrawn_is_audited(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Nothing left to try automatically — the audit row is how a person finds
    the event and removes it by hand."""

    class WontCancel(FakeCalendar):
        async def cancel_event(self, calendar_id, event_id):
            raise workspace.WorkspaceError("gone")

    _with_calendar(monkeypatch, WontCancel())
    _recorded(monkeypatch, None)

    result = await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="x")
    assert result.outcome == "taken"
    assert "cancel_event:fail" in desk


# ── Taking an offer off the table ────────────────────────────────────────────

def _offer(replaces_id: int | None = None) -> Offer:
    return Offer(
        id=99, contact_id=7, chat_id="42", slots=[slot()],
        calendar_id="desk@example.com", replaces_id=replaces_id,
    )


def _claims(monkeypatch: pytest.MonkeyPatch, succeeds: bool = True):
    events: list[str] = []

    async def fake_claim(conn, offer_id):
        events.append("claim")
        return succeeds

    async def fake_release(conn, offer_id):
        events.append("release")

    monkeypatch.setattr(offers_repo, "claim", fake_claim)
    monkeypatch.setattr(offers_repo, "release", fake_release)
    return events


async def test_the_offer_is_claimed_before_the_calendar_is_touched(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The same answer sent twice must produce one booking, not two."""
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    events = _claims(monkeypatch)

    result = await scheduling.take_offer(
        CONN, offer=_offer(), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "booked"
    assert events == ["claim"]


async def test_an_offer_already_used_books_nothing(
    monkeypatch: pytest.MonkeyPatch, desk
):
    calendar = _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    _claims(monkeypatch, succeeds=False)

    result = await scheduling.take_offer(
        CONN, offer=_offer(), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "failed"
    assert calendar.created == []


async def test_a_claim_that_does_not_become_a_booking_is_put_back(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """Otherwise a calendar error costs them the offer as well as the booking:
    the next thing they say has nothing left to match against."""
    _with_calendar(monkeypatch, FakeCalendar(fail_create=True))
    _recorded(monkeypatch, _booked())
    events = _claims(monkeypatch)

    result = await scheduling.take_offer(
        CONN, offer=_offer(), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "failed"
    assert events == ["claim", "release"]


# ── Rescheduling ─────────────────────────────────────────────────────────────

def _replacing(monkeypatch: pytest.MonkeyPatch, cancel_succeeds: bool = True):
    cancelled: list[int] = []

    async def fake_get(conn, appointment_id):
        # Still ahead and still this contact's: a reschedule is only valid then.
        ahead = datetime.now(ZONE) + timedelta(days=5)
        return _booked(appointment_id).model_copy(update={
            "starts_at": ahead, "ends_at": ahead + timedelta(minutes=30)})

    async def fake_cancel(conn, appointment):
        cancelled.append(appointment.id)
        return cancel_succeeds

    async def lock_contact(conn, contact_id):
        return None

    monkeypatch.setattr(appointments_repo, "get_by_id", fake_get)
    monkeypatch.setattr(appointments_repo, "lock_contact", lock_contact)
    monkeypatch.setattr(scheduling, "cancel", fake_cancel)
    return cancelled


async def test_a_reschedule_cancels_the_old_only_once_the_new_one_exists(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked(2))
    _claims(monkeypatch)
    cancelled = _replacing(monkeypatch)

    result = await scheduling.take_offer(
        CONN, offer=_offer(replaces_id=1), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "booked"
    assert cancelled == [1]


async def test_a_failed_reschedule_leaves_the_original_appointment_intact(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The person must never be left with neither: nothing is released until
    the replacement is real."""
    _with_calendar(monkeypatch, FakeCalendar(fail_create=True))
    _recorded(monkeypatch, _booked(2))
    _claims(monkeypatch)
    cancelled = _replacing(monkeypatch)

    result = await scheduling.take_offer(
        CONN, offer=_offer(replaces_id=1), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "failed"
    assert cancelled == []


async def test_a_slot_taken_during_a_reschedule_keeps_the_original(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _with_calendar(monkeypatch, FakeCalendar(busy=[slot()]))
    _recorded(monkeypatch, _booked(2))
    _claims(monkeypatch)
    cancelled = _replacing(monkeypatch)

    result = await scheduling.take_offer(
        CONN, offer=_offer(replaces_id=1), contact=CONTACT, slot=slot(), title="x"
    )
    assert result.outcome == "taken"
    assert cancelled == []


async def test_a_booking_in_test_mode_is_marked(monkeypatch: pytest.MonkeyPatch, desk):
    """The test mode finds its own appointments by this mark, not by a title it
    can no longer match in SQL."""
    _with_calendar(monkeypatch, FakeCalendar())
    saved = _recorded(monkeypatch, _booked())
    tester = CONTACT.model_copy(update={"test_mode": "novo"})

    await scheduling.book(CONN, contact=tester, slot=slot(), title="[TESTE] Atendimento")
    await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="Atendimento: Ana")

    assert [a.test_mode for a in saved] == [True, False]


async def test_a_booking_records_the_channel_it_came_from(monkeypatch: pytest.MonkeyPatch, desk):
    _with_calendar(monkeypatch, FakeCalendar())
    saved = _recorded(monkeypatch, _booked())
    whatsapp = CONTACT.model_copy(update={"chat_id": "wa:5591988887777"})

    await scheduling.book(CONN, contact=whatsapp, slot=slot(), title="Atendimento: Ana")
    await scheduling.book(CONN, contact=CONTACT, slot=slot(), title="Atendimento: Ana")

    assert [a.booked_via for a in saved] == ["whatsapp", "telegram"]


# ── The limit of upcoming appointments, and a reschedule that is still valid ─

def _future(appointment_id: int, *, contact_id: int = 7,
            status: Literal["confirmed", "cancelled", "no_show", "done"] = "confirmed",
            days: int = 5) -> Appointment:
    start = datetime.now(ZONE) + timedelta(days=days)
    return Appointment(id=appointment_id, contact_id=contact_id, calendar_id="desk@example.com",
                       provider="google", provider_event_id=f"ev{appointment_id}", title="x",
                       starts_at=start, ends_at=start + timedelta(minutes=30), status=status)


def _held(monkeypatch: pytest.MonkeyPatch, upcoming: list[Appointment],
          known: list[Appointment] | None = None) -> list[str]:
    """The contact's appointments, and the order the booking reads them in."""
    events: list[str] = []

    async def lock_contact(conn, contact_id):
        events.append("lock")

    async def count_upcoming(conn, contact_id, *, excluding=None):
        events.append("count")
        return sum(1 for a in upcoming if a.id != excluding)

    async def get_by_id(conn, appointment_id):
        return next((a for a in (known or upcoming) if a.id == appointment_id), None)

    async def expire(conn, chat_id):
        events.append("expire")

    monkeypatch.setattr(appointments_repo, "lock_contact", lock_contact)
    monkeypatch.setattr(appointments_repo, "count_upcoming", count_upcoming)
    async def cancel(conn, appointment):
        events.append(f"cancel {appointment.id}")
        return True

    monkeypatch.setattr(appointments_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(offers_repo, "expire", expire)
    monkeypatch.setattr(scheduling, "cancel", cancel)
    return events


async def test_at_the_limit_a_new_booking_is_refused(monkeypatch: pytest.MonkeyPatch, desk):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 1)
    calendar = _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    claims = _claims(monkeypatch)
    events = _held(monkeypatch, [_future(1)])

    result = await scheduling.take_offer(CONN, offer=_offer(), contact=CONTACT, slot=slot(),
                                         title="x")
    assert result.outcome == "limit"
    assert calendar.created == [] and claims == [] and "expire" in events


async def test_the_limit_is_read_under_the_contacts_lock_before_the_claim(
    monkeypatch: pytest.MonkeyPatch, desk
):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 2)
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    events = _held(monkeypatch, [_future(1)])
    claims = _claims(monkeypatch)

    result = await scheduling.take_offer(CONN, offer=_offer(), contact=CONTACT, slot=slot(),
                                         title="x")
    assert result.outcome == "booked"
    assert events[:2] == ["lock", "count"] and claims == ["claim"]


async def test_a_reschedule_does_not_count_against_the_limit(monkeypatch: pytest.MonkeyPatch,
                                                             desk):
    monkeypatch.setattr(settings, "max_upcoming_appointments", 1)
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    _claims(monkeypatch)
    _held(monkeypatch, [_future(1)])

    result = await scheduling.take_offer(CONN, offer=_offer(replaces_id=1), contact=CONTACT,
                                         slot=slot(), title="x")
    assert result.outcome == "booked"


@pytest.mark.parametrize("replaced", [
    _future(1, status="cancelled"), _future(1, days=-1), _future(1, contact_id=99), None])
async def test_a_reschedule_of_an_appointment_no_longer_held_is_refused(
    monkeypatch: pytest.MonkeyPatch, desk, replaced
):
    """Even with no limit: an old "remarcar" button must not book a second one."""
    calendar = _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    claims = _claims(monkeypatch)
    events = _held(monkeypatch, [], known=[replaced] if replaced else [])

    result = await scheduling.take_offer(CONN, offer=_offer(replaces_id=1), contact=CONTACT,
                                         slot=slot(), title="x")
    assert result.outcome == "stale_reschedule"
    assert calendar.created == [] and claims == [] and "expire" in events


async def test_with_no_limit_and_no_reschedule_nothing_more_is_read(
    monkeypatch: pytest.MonkeyPatch, desk
):
    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked())
    _claims(monkeypatch)
    events = _held(monkeypatch, [_future(1), _future(2)])

    result = await scheduling.take_offer(CONN, offer=_offer(), contact=CONTACT, slot=slot(),
                                         title="x")
    assert result.outcome == "booked" and events == []


async def test_a_refused_cancel_of_the_old_marks_it_cancelled_and_tells_the_team(
    monkeypatch: pytest.MonkeyPatch, desk
):
    """The new one is booked and confirmed; the old event stays in the calendar
    (the calendar still blocks that time), so the row is cancelled here and the
    team is told to remove the event by hand."""
    from core.services import inbox

    _with_calendar(monkeypatch, FakeCalendar())
    _recorded(monkeypatch, _booked(2))
    _claims(monkeypatch)
    _replacing(monkeypatch, cancel_succeeds=False)
    locally: list[int] = []
    told: list[tuple[str, int | None]] = []

    async def cancel_row(conn, appointment_id):
        locally.append(appointment_id)
        return True

    async def notify_team(text, contact_id):
        told.append((text, contact_id))
        return True

    monkeypatch.setattr(appointments_repo, "cancel", cancel_row)
    monkeypatch.setattr(inbox, "notify_team", notify_team)

    result = await scheduling.take_offer(CONN, offer=_offer(replaces_id=1), contact=CONTACT,
                                         slot=slot(), title="x")
    assert result.outcome == "booked"
    assert locally == [1]
    assert len(told) == 1 and told[0][1] == CONTACT.id
    assert "Ana" in told[0][0] and "Apague o evento à mão" in told[0][0]
