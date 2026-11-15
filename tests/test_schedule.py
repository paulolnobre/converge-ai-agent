"""When the desk is open."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from core.config import settings
from core.schedule import is_open, is_workday

ZONE = ZoneInfo(settings.office_timezone)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-09-14 is a Monday, so 14..18 are Mon..Fri and 19/20 the weekend.
    return datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def test_open_during_the_working_day():
    assert is_open(at(14, 10)) is True


def test_closed_before_opening_and_after_closing():
    assert is_open(at(14, 7)) is False
    assert is_open(at(14, 19)) is False


def test_closed_on_the_weekend_even_during_office_hours():
    """An office open 09:00-18:00 is not open at 10:00 on a Sunday."""
    assert is_open(at(20, 10)) is False


def test_the_moment_of_opening_counts_as_open():
    assert is_open(at(14, 9)) is True


def test_the_moment_of_closing_counts_as_closed():
    assert is_open(at(14, 18)) is False


def test_workdays_follow_the_configuration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "office_workdays", "6,7")
    assert is_workday(date(2026, 9, 19)) is True   # Saturday
    assert is_workday(date(2026, 9, 14)) is False  # Monday


def test_desk_always_open_overrides_the_office_hours(monkeypatch: pytest.MonkeyPatch):
    """The bot's own hours, not the office's — a booking is a separate check,
    in scheduling.offer_times(), that this flag never touches."""
    monkeypatch.setattr(settings, "desk_always_open", True)
    assert is_open(at(20, 3)) is True   # Sunday, 3am
    assert is_open(at(14, 7)) is True   # a weekday, before opening


def test_desk_always_open_off_still_follows_office_hours(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "desk_always_open", False)
    assert is_open(at(20, 10)) is False  # Sunday, still closed
