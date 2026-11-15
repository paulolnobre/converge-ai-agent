"""The full availability, and the pages of days and times built from it."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.models import TimeSlot
from core.services import booking_lists as lists
from core.services import scheduling

ZONE = ZoneInfo(settings.office_timezone)


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE)  # 19/10/2026 is a Monday


class FakeCalendar:
    def __init__(self, busy: dict[str, list[TimeSlot]] | None = None):
        self.busy = busy or {}
        self.asked: list[tuple] = []

    async def busy_periods(self, calendar_ids, start, end):
        self.asked.append((tuple(calendar_ids), start, end))
        return {name: self.busy.get(name, []) for name in calendar_ids}


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "reception_calendar_id", "desk@example.com")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    monkeypatch.setattr(settings, "booking_horizon_days", 30)
    monkeypatch.setattr(settings, "slot_step_minutes", 30)
    monkeypatch.setattr(settings, "appointment_duration_minutes", 30)
    monkeypatch.setattr(settings, "minimum_notice_minutes", 120)
    fake = FakeCalendar()
    monkeypatch.setattr(workspace, "calendar", lambda: fake)
    return fake


def _local(slot: TimeSlot) -> datetime:
    return slot.start.astimezone(ZONE)


# ── available_starts ─────────────────────────────────────────────────────────

async def test_every_free_start_of_the_horizon_on_the_grid(desk):
    starts = await scheduling.available_starts(period=None, now=at(19, 8))
    first, last = _local(starts[0]), _local(starts[-1])
    assert first == at(19, 10)  # 08:00 + 120 min of notice
    assert last.date() <= (at(19, 8) + timedelta(days=30)).date()
    assert all(_local(s).minute in (0, 30) for s in starts)
    assert all(_local(s).isoweekday() in settings.workdays() for s in starts)
    assert all(time(9) <= _local(s).time() and (_local(s) + timedelta(minutes=30)).time() <= time(18)
               for s in starts)
    assert len(desk.asked) == 1  # one call to the provider for the whole horizon


async def test_busy_on_any_calendar_in_scope_is_not_free(desk, monkeypatch):
    monkeypatch.setattr(settings, "staff_calendar_ids", "ana@example.com,bruno@example.com")
    desk.busy = {"bruno@example.com": [TimeSlot(start=at(20, 0), end=at(20, 23, 59))]}
    starts = await scheduling.available_starts(period=None, now=at(19, 8))
    assert not any(_local(s).date() == date(2026, 10, 20) for s in starts)
    assert desk.asked[0][0] == ("ana@example.com", "bruno@example.com")


async def test_the_grid_follows_slot_step_minutes(desk, monkeypatch):
    monkeypatch.setattr(settings, "slot_step_minutes", 60)
    starts = await scheduling.available_starts(period=None, now=at(19, 8))
    assert all(_local(s).minute == 0 for s in starts)


@pytest.mark.parametrize("horizon", [7, 30])
async def test_the_horizon_is_counted_from_today_in_the_office_time_zone(desk, monkeypatch,
                                                                         horizon):
    monkeypatch.setattr(settings, "booking_horizon_days", horizon)
    starts = await scheduling.available_starts(period=None, now=at(19, 8))
    assert max(_local(s).date() for s in starts) <= date(2026, 10, 19) + timedelta(days=horizon)


@pytest.mark.parametrize("clock", [(19, 23, 50), (20, 0, 10)])
async def test_late_night_shows_no_past_time_and_counts_days_locally(desk, clock):
    """Review Focus 1: 23h50 and 00h10 in the office's zone, not in UTC."""
    now = at(*clock)
    starts = await scheduling.available_starts(period=None, now=now)
    assert all(s.start >= now + timedelta(minutes=120) for s in starts)
    assert max(_local(s).date() for s in starts) <= now.date() + timedelta(days=30)


async def test_a_period_filters_the_starts(desk):
    morning = await scheduling.available_starts(period="morning", now=at(19, 8))
    afternoon = await scheduling.available_starts(period="afternoon", now=at(19, 8))
    assert morning and all(_local(s).hour < 12 for s in morning)
    assert afternoon and all(_local(s).hour >= 12 for s in afternoon)


# ── days_page ────────────────────────────────────────────────────────────────

def _starts(*moments: datetime) -> list[TimeSlot]:
    return [TimeSlot(start=m, end=m + timedelta(minutes=30)) for m in moments]


def test_days_page_lists_only_days_with_free_times_up_to_nine():
    starts = _starts(*(at(day, 10) for day in range(19, 31)), at(19, 14))
    entries, following = lists.days_page(starts, first=None, period=None)
    assert [e.day for e in entries] == [date(2026, 10, d) for d in range(19, 28)]
    assert entries[0].count == 2 and entries[1].count == 1
    assert following == date(2026, 10, 28)


def test_days_page_has_no_next_when_everything_fits():
    entries, following = lists.days_page(_starts(at(19, 10), at(21, 10)), first=None, period=None)
    assert [e.day for e in entries] == [date(2026, 10, 19), date(2026, 10, 21)]
    assert following is None


def test_days_page_starts_at_first_and_tells_one_sided_days():
    starts = _starts(at(19, 10), at(20, 9), at(20, 10), at(21, 14), at(22, 10), at(22, 15))
    entries, _ = lists.days_page(starts, first=date(2026, 10, 20), period=None)
    assert [(e.day.day, e.count, e.only) for e in entries] == [
        (20, 2, "morning"), (21, 1, "afternoon"), (22, 2, None)]


def test_days_page_filters_by_period():
    starts = _starts(at(19, 10), at(20, 15))
    entries, _ = lists.days_page(starts, first=None, period="afternoon")
    assert [e.day.day for e in entries] == [20]


# ── times_page ───────────────────────────────────────────────────────────────

def test_more_than_nine_times_give_eight_and_a_next_page():
    day = _starts(*(at(19, 9, 0) + timedelta(minutes=30 * i) for i in range(12)))
    first, after = lists.times_page(day, date(2026, 10, 19), after=None)
    assert len(first) == 8 and after == time(12, 30)
    second, again = lists.times_page(day, date(2026, 10, 19), after=after)
    assert [_local(s).time() for s in second] == [time(13), time(13, 30), time(14), time(14, 30)]
    assert again is None


def test_nine_times_or_fewer_fit_one_page():
    day = _starts(*(at(19, 9, 0) + timedelta(minutes=30 * i) for i in range(9)))
    page, after = lists.times_page(day, date(2026, 10, 19), after=None)
    assert len(page) == 9 and after is None


def test_times_page_keeps_to_its_day():
    page, _ = lists.times_page(_starts(at(19, 10), at(20, 10)), date(2026, 10, 20), after=None)
    assert [_local(s).day for s in page] == [20]


# ── titles and callbacks ─────────────────────────────────────────────────────

def test_titles():
    assert lists.day_title(date(2026, 10, 20)) == "Terça, 20/10"
    assert lists.day_title(date(2026, 12, 31)) == "Quinta, 31/12"
    assert lists.time_title(_starts(at(19, 9))[0]) == "9h"
    assert lists.time_title(_starts(at(19, 9, 30))[0]) == "9h30"


def test_callbacks_round_trip_and_fit_telegram():
    for nav in (lists.Nav("day", date(2026, 10, 21), "morning", 42, None),
                lists.Nav("days", date(2026, 10, 21), None, None, None),
                lists.Nav("more", date(2026, 10, 21), "afternoon", 9_223_372_036_854_775_807,
                          time(14, 30))):
        data = lists.callback(nav)
        assert lists.parse(data) == nav
        assert len(data.encode()) <= 64, data


@pytest.mark.parametrize("data", ["day:2026-13-40:m:-", "lixo", "more:2026-10-21:9h:m:-",
                                  "day:2026-10-21:x:-", "day:2026-10-21:m:abc", "days:2026-10-21"])
def test_a_malformed_callback_is_none(data):
    assert lists.parse(data) is None
