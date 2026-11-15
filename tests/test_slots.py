"""Turning busy periods into times a person can be offered."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from core.integrations.workspace.models import TimeSlot
from core.integrations.workspace.slots import free_slots, merge

ZONE = ZoneInfo(settings.office_timezone)


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def test_merge_collapses_the_same_meeting_reported_by_two_calendars():
    duplicated = [
        TimeSlot(start=at(9), end=at(10)),
        TimeSlot(start=at(9), end=at(10)),
    ]
    assert merge(duplicated) == [TimeSlot(start=at(9), end=at(10))]


def test_merge_joins_overlapping_blocks():
    merged = merge(
        [
            TimeSlot(start=at(9), end=at(10, 30)),
            TimeSlot(start=at(10), end=at(11)),
        ]
    )
    assert merged == [TimeSlot(start=at(9), end=at(11))]


def test_free_slots_avoid_a_busy_block():
    busy = [TimeSlot(start=at(9), end=at(10))]
    found = free_slots(
        busy, at(8), at(12), duration_minutes=60, limit=3, now=at(7)
    )
    assert all(slot.start >= at(10) or slot.end <= at(9) for slot in found)


def test_free_slots_never_start_in_the_past():
    found = free_slots([], at(8), at(18), duration_minutes=30, limit=4, now=at(14, 5))
    assert found and all(slot.start >= at(14, 5) for slot in found)


def test_free_slots_land_on_the_grid():
    found = free_slots([], at(8), at(12), duration_minutes=30, limit=4, now=at(7))
    assert all(slot.start.minute in (0, 30) for slot in found)


def test_free_slots_stay_inside_working_hours():
    found = free_slots([], at(0), at(23, 59), duration_minutes=60, limit=50, now=at(0))
    opens = settings.office_start_hour
    closes = settings.office_end_hour
    assert all(opens <= slot.start.hour and slot.end.hour <= closes for slot in found)


def test_a_fully_booked_day_offers_nothing():
    busy = [TimeSlot(start=at(0), end=at(23, 59))]
    assert free_slots(busy, at(8), at(12), duration_minutes=30, now=at(7)) == []


def test_search_spans_more_than_one_day():
    busy = [TimeSlot(start=at(0, 0, day=14), end=at(23, 59, day=14))]
    found = free_slots(
        busy,
        at(8, 0, day=14),
        at(18, 0, day=15),
        duration_minutes=60,
        limit=2,
        now=at(7, 0, day=14),
    )
    assert found and all(slot.start.date() == at(0, 0, day=15).date() for slot in found)


def test_duration_is_respected():
    found = free_slots([], at(8), at(12), duration_minutes=90, limit=1, now=at(7))
    assert found[0].end - found[0].start == timedelta(minutes=90)
