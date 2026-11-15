"""The pages of days and times a person chooses from (BOOKING_MODE=choose_day).

Pure: given every free start of the horizon (scheduling.available_starts), it
says which days a page lists, which times a day's page shows, how they are
titled, and what a tap carries. No I/O, so every rule is tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Literal
from zoneinfo import ZoneInfo

from core.config import settings
from core.integrations.workspace import TimeSlot

DAYS_PER_PAGE = 9
TIMES_PER_PAGE = 9   # when the whole day fits
TIMES_WITH_MORE = 8  # when it does not: 8, "Mais horários" and "Outro dia"

_WEEKDAYS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")
_PERIOD_CODE = {"morning": "m", "afternoon": "t", None: "-"}
_CODE_PERIOD = {code: period for period, code in _PERIOD_CODE.items()}


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.office_timezone)


def _day_of(slot: TimeSlot) -> date:
    return slot.start.astimezone(_zone()).date()


def _in(slot: TimeSlot, period: str | None) -> bool:
    if period not in ("morning", "afternoon"):
        return True
    hour = slot.start.astimezone(_zone()).hour
    return hour < 12 if period == "morning" else hour >= 12


@dataclass(frozen=True)
class DayEntry:
    day: date
    count: int
    only: Literal["morning", "afternoon"] | None  # every free time on one side of noon


def days_page(starts: list[TimeSlot], *, first: date | None, period: str | None,
              limit: int = DAYS_PER_PAGE) -> tuple[list[DayEntry], date | None]:
    """The days with a free time (in `period`) from `first` on, at most
    `limit`, and the first day of the next page (None when there is none)."""
    by_day: dict[date, list[TimeSlot]] = {}
    for slot in starts:
        day = _day_of(slot)
        if (first is None or day >= first) and _in(slot, period):
            by_day.setdefault(day, []).append(slot)
    days = sorted(by_day)
    entries = []
    for day in days[:limit]:
        slots = by_day[day]
        mornings = sum(1 for s in slots if _in(s, "morning"))
        only = "morning" if mornings == len(slots) else "afternoon" if mornings == 0 else None
        entries.append(DayEntry(day=day, count=len(slots), only=only))
    return entries, (days[limit] if len(days) > limit else None)


def times_page(starts: list[TimeSlot], day: date, *, after: time | None,
               period: str | None = None) -> tuple[list[TimeSlot], time | None]:
    """The free times of `day` (in `period`) after `after`: all of them when
    they fit in one page (9), else 8 and the time the next page starts after."""
    zone = _zone()
    times = sorted(
        (s for s in starts if _day_of(s) == day and _in(s, period)
         and (after is None or s.start.astimezone(zone).time() > after)),
        key=lambda s: s.start,
    )
    if len(times) <= TIMES_PER_PAGE:
        return times, None
    page = times[:TIMES_WITH_MORE]
    return page, page[-1].start.astimezone(zone).time()


def day_title(day: date) -> str:
    """"Terça, 21/10"."""
    return f"{_WEEKDAYS[day.weekday()]}, {day:%d/%m}"


def time_title(slot: TimeSlot) -> str:
    """"9h", "9h30"."""
    local = slot.start.astimezone(_zone())
    return f"{local.hour}h{local.minute:02d}" if local.minute else f"{local.hour}h"


@dataclass(frozen=True)
class Nav:
    """What a tap on a day, a page of days or a page of times carries."""

    kind: Literal["day", "days", "more"]
    day: date
    period: str | None       # "morning", "afternoon" or None
    replaces: int | None     # the appointment being moved, if any
    after: time | None       # "more" only: the last time already shown


def callback(nav: Nav) -> str:
    """day:<AAAA-MM-DD>:<p>:<r>, days:..., more:<AAAA-MM-DD>:<HHMM>:<p>:<r>."""
    parts = [nav.kind, nav.day.isoformat()]
    if nav.kind == "more":
        assert nav.after is not None
        parts.append(f"{nav.after:%H%M}")
    parts += [_PERIOD_CODE[nav.period], str(nav.replaces) if nav.replaces else "-"]
    return ":".join(parts)


def parse(data: str) -> Nav | None:
    """The tap back, or None for anything malformed (an old or forged button)."""
    parts = data.split(":")
    kind = parts[0] if parts else ""
    expected = {"day": 4, "days": 4, "more": 5}.get(kind)
    if expected is None or len(parts) != expected:
        return None
    try:
        day = date.fromisoformat(parts[1])
        after = None
        if kind == "more":
            stamp = parts[2]
            if len(stamp) != 4 or not stamp.isdigit():
                return None
            after = time(int(stamp[:2]), int(stamp[2:]))
        period_code, replaces_code = parts[-2], parts[-1]
    except ValueError:
        return None
    if period_code not in _CODE_PERIOD:
        return None
    if replaces_code != "-" and not replaces_code.isdigit():
        return None
    replaces = int(replaces_code) if replaces_code != "-" else None
    return Nav(kind, day, _CODE_PERIOD[period_code], replaces, after)  # type: ignore[arg-type]
