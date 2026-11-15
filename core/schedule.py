"""When the desk is open, and what to say when it is not."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from core import content
from core.config import settings


def is_open(moment: datetime | None = None) -> bool:
    """Whether the *bot* answers right now — not whether anyone would pick up
    the phone. DESK_ALWAYS_OPEN decouples the two on purpose: the office's real
    hours (OFFICE_START_HOUR/END_HOUR/WORKDAYS) still govern which meeting
    times offer_times() offers, in core/services/scheduling.py and
    core/integrations/workspace/slots.py, whatever this returns — a bot can
    read messages at 3am without being able to book someone into one.

    With it off: weekday and hour, both. An office that is open 09:00–18:00 is
    not open at 10:00 on a Sunday, and a bot that books an appointment into
    one is worse than a bot that says it is closed.
    """
    if settings.desk_always_open:
        return True
    zone = ZoneInfo(settings.office_timezone)
    now = (moment or datetime.now(tz=zone)).astimezone(zone)
    if now.isoweekday() not in settings.workdays():
        return False
    opens = time(settings.office_start_hour, settings.office_start_minute)
    closes = time(settings.office_end_hour, settings.office_end_minute)
    return opens <= now.time() < closes


def is_workday(day: date) -> bool:
    return day.isoweekday() in settings.workdays()


def out_of_hours_message() -> str:
    """What the desk says when it is closed — the organization's own words.

    `OUT_OF_HOURS_REPLY` in the environment still wins, for a deployment that
    wants to change this without touching the content file.
    """
    override = settings.out_of_hours_reply.strip()
    return override or content.text("reply.out_of_hours")
