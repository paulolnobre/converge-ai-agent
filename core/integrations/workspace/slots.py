"""Turning busy periods into offerable times.

Both products need the same arithmetic — the receptionist offering a client
three times, the site assistant finding a window for a supplier visit — and
neither should re-derive it from a provider's free/busy payload.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from core.integrations.workspace.models import TimeSlot


def _office_window(day: datetime) -> tuple[datetime, datetime]:
    zone = ZoneInfo(settings.office_timezone)
    local = day.astimezone(zone)
    opens = datetime.combine(
        local.date(),
        time(settings.office_start_hour, settings.office_start_minute),
        tzinfo=zone,
    )
    closes = datetime.combine(
        local.date(),
        time(settings.office_end_hour, settings.office_end_minute),
        tzinfo=zone,
    )
    return opens, closes


def merge(periods: list[TimeSlot]) -> list[TimeSlot]:
    """Collapse overlapping busy blocks into one ordered list.

    Two calendars queried together routinely report the same meeting twice; a
    naive subtraction would then leave a phantom gap between the copies.
    """
    ordered = sorted(periods, key=lambda slot: slot.start)
    merged: list[TimeSlot] = []
    for slot in ordered:
        if merged and slot.start <= merged[-1].end:
            if slot.end > merged[-1].end:
                merged[-1] = TimeSlot(start=merged[-1].start, end=slot.end)
            continue
        merged.append(TimeSlot(start=slot.start, end=slot.end))
    return merged


def free_slots(
    busy: list[TimeSlot],
    start: datetime,
    end: datetime,
    duration_minutes: int,
    *,
    step_minutes: int = 30,
    limit: int = 12,
    now: datetime | None = None,
) -> list[TimeSlot]:
    """Offerable start times inside office hours, on a fixed grid.

    A grid (:00 and :30 by default) rather than "the first free minute": a
    client offered 14:07 assumes the agent is guessing, and the staff diary is
    kept in round slots anyway.
    """
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    blocked = merge(busy)
    floor = now or datetime.now(tz=ZoneInfo(settings.office_timezone))

    found: list[TimeSlot] = []
    day = start
    while day.date() <= end.date() and len(found) < limit:
        opens, closes = _office_window(day)
        cursor = max(opens, start, floor)
        # Snap forward onto the grid rather than back, so a slot never starts
        # in the past or before the office opens.
        minutes_past = (cursor - opens) % step
        if minutes_past:
            cursor += step - minutes_past

        while cursor + duration <= closes and len(found) < limit:
            candidate = TimeSlot(start=cursor, end=cursor + duration)
            clash = next(
                (
                    period
                    for period in blocked
                    if period.start < candidate.end and candidate.start < period.end
                ),
                None,
            )
            if clash is None:
                found.append(candidate)
                cursor += step
            else:
                # Jump to the end of what blocked us instead of stepping through it.
                cursor = max(cursor + step, clash.end)
                minutes_past = (cursor - opens) % step
                if minutes_past:
                    cursor += step - minutes_past
        day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return found
