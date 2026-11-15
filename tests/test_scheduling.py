"""Offering times, and refusing to offer bad ones."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.models import TimeSlot
from core.services import scheduling

ZONE = ZoneInfo(settings.office_timezone)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


class FakeCalendar:
    def __init__(self, busy: dict[str, list[TimeSlot]]):
        self.busy = busy

    async def busy_periods(self, calendar_ids, start, end):
        return {name: self.busy.get(name, []) for name in calendar_ids}


@pytest.fixture
def one_calendar(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "reception_calendar_id", "desk@example.com")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    return "desk@example.com"


def _with_calendar(monkeypatch: pytest.MonkeyPatch, fake):
    monkeypatch.setattr(workspace, "calendar", lambda: fake)


async def test_offers_the_configured_number_of_slots(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    _with_calendar(monkeypatch, FakeCalendar({}))
    slots = await scheduling.offer_times(now=at(14, 9))
    assert len(slots) == settings.slots_offered


async def test_never_offers_inside_the_notice_window(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    """Someone needs time to get there, and the staff to see it."""
    _with_calendar(monkeypatch, FakeCalendar({}))
    now = at(14, 9)
    slots = await scheduling.offer_times(now=now)
    floor = now + timedelta(minutes=settings.minimum_notice_minutes)
    assert all(slot.start >= floor for slot in slots)


async def test_a_slot_busy_on_any_calendar_is_not_offered(
    monkeypatch: pytest.MonkeyPatch
):
    """Free for one person is not free for a meeting that needs two."""
    monkeypatch.setattr(settings, "staff_calendar_ids", "ana@example.com,bruno@example.com")
    busy_all_monday = [TimeSlot(start=at(14, 0), end=at(14, 23, 59))]
    _with_calendar(
        monkeypatch,
        FakeCalendar({"ana@example.com": [], "bruno@example.com": busy_all_monday}),
    )
    slots = await scheduling.offer_times(now=at(14, 8))
    assert all(slot.start.date() != at(14, 0).date() for slot in slots)


async def test_never_offers_a_day_the_desk_is_closed(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    _with_calendar(monkeypatch, FakeCalendar({}))
    slots = await scheduling.offer_times(now=at(18, 17))  # Friday evening
    assert all(slot.start.isoweekday() in settings.workdays() for slot in slots)


async def test_a_fully_booked_horizon_offers_nothing(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    everything = [TimeSlot(start=at(14, 0), end=at(14, 0) + timedelta(days=60))]
    _with_calendar(monkeypatch, FakeCalendar({"desk@example.com": everything}))
    assert await scheduling.offer_times(now=at(14, 9)) == []


async def test_no_configured_calendar_is_an_error_not_a_silent_empty_list(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "reception_calendar_id", None)
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    with pytest.raises(ValueError, match="RECEPTION_CALENDAR_ID"):
        await scheduling.offer_times(now=at(14, 9))


def test_the_offer_reads_like_a_person_said_it():
    text = scheduling.phrase(
        [TimeSlot(start=at(14, 10), end=at(14, 10, 30)),
         TimeSlot(start=at(15, 14), end=at(15, 14, 30))]
    )
    assert "segunda-feira" in text and "terça-feira" in text and " ou " in text


def test_the_offer_is_written_in_portuguese_with_its_accents():
    """A clinic that writes "terca" to a patient reads as careless."""
    text = scheduling.phrase([TimeSlot(start=at(15, 14), end=at(15, 14, 30))])
    assert "terça-feira" in text and "às" in text


def test_an_empty_offer_is_empty_not_a_sentence():
    """The apology belongs to the organization, in content/messages.toml."""
    assert scheduling.phrase([]) == ""


def test_scope_prefers_staff_calendars_over_the_desk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "staff_calendar_ids", "ana@example.com, bruno@example.com")
    monkeypatch.setattr(settings, "reception_calendar_id", "desk@example.com")
    assert scheduling.calendars_in_scope() == ["ana@example.com", "bruno@example.com"]


def test_short_when_is_compact_for_a_button_label():
    """A button has no room for a full sentence, unlike scheduling.when()."""
    text = scheduling.short_when(TimeSlot(start=at(15, 14), end=at(15, 14, 30)))
    assert text == "15/09 14:00"


# ── Asking for a period ──────────────────────────────────────────────────────

async def test_a_morning_preference_excludes_afternoon_slots(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    _with_calendar(monkeypatch, FakeCalendar({}))
    slots = await scheduling.offer_times(now=at(14, 8), preferred_period="morning")
    assert slots
    assert all(slot.start.astimezone(ZONE).hour < 12 for slot in slots)


async def test_an_afternoon_preference_excludes_morning_slots(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    _with_calendar(monkeypatch, FakeCalendar({}))
    slots = await scheduling.offer_times(now=at(14, 8), preferred_period="afternoon")
    assert slots
    assert all(slot.start.astimezone(ZONE).hour >= 12 for slot in slots)


async def test_period_any_offers_the_same_as_no_preference(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    _with_calendar(monkeypatch, FakeCalendar({}))
    assert (
        await scheduling.offer_times(now=at(14, 8), preferred_period="any")
        == await scheduling.offer_times(now=at(14, 8))
    )


async def test_offered_slots_are_not_neighbours(
    monkeypatch: pytest.MonkeyPatch, one_calendar
):
    """An empty diary used to offer 09:00, 09:30 and 10:00 — three answers to
    one question. Earliest still wins, but each is SLOT_MIN_GAP_MINUTES apart."""
    _with_calendar(monkeypatch, FakeCalendar({}))
    slots = await scheduling.offer_times(now=at(14, 6))
    assert len(slots) == settings.slots_offered
    gap = timedelta(minutes=settings.slot_min_gap_minutes)
    assert all(b.start - a.start >= gap for a, b in zip(slots, slots[1:]))
