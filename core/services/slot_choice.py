"""Which of the offered times the person just picked.

Deliberately not a model call. The desk offers three times and the reply is
almost always two words — "a terca", "10h", "o primeiro" — and the cost of
reading those two words wrong is an appointment in a stranger's diary at a time
nobody agreed to. So this is arithmetic over the times that were actually
offered, and anything it cannot read confidently comes back as `unclear`, which
makes the desk ask again.

The four outcomes exist because the desk says something different for each:

    picked      book it
    ambiguous   "terca eu tenho as 9 e as 14 — qual delas?"
    declined    none of these; offer others
    unclear     "desculpe, qual deles?"
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core.config import settings
from core.integrations.workspace.models import TimeSlot

Kind = Literal["picked", "ambiguous", "declined", "unclear"]


class Choice(BaseModel):
    kind: Kind
    slot: TimeSlot | None = None
    # What the message narrowed the offer down to. Ambiguity is only useful if
    # the desk can say which times it is asking between.
    candidates: list[TimeSlot] = Field(default_factory=list)


# "boa tarde" is a greeting, not a request for the afternoon. Stripped before
# anything else, because otherwise it silently rules out every morning slot.
_GREETINGS = re.compile(r"\b(bom dia|boa tarde|boa noite|ola|oi|obrigad[oa])\b")

_WEEKDAYS = {
    1: re.compile(r"\bsegunda\b|\bseg\b"),
    # No bare "ter": it is also the verb, and "vou ter que ver" is not Tuesday.
    2: re.compile(r"\bterca\b"),
    3: re.compile(r"\bquarta\b|\bqua\b"),
    4: re.compile(r"\bquinta\b|\bqui\b"),
    5: re.compile(r"\bsexta\b|\bsex\b"),
    6: re.compile(r"\bsabado\b|\bsab\b"),
    7: re.compile(r"\bdomingo\b|\bdom\b"),
}

_ORDINALS = {
    1: re.compile(r"\bprimeir[oa]\b|\b1[oa]\b"),
    # "segundo", never "segunda" — in an offer of weekdays that word is Monday.
    2: re.compile(r"\bsegundo\b|\b2[oa]\b"),
    3: re.compile(r"\bterceir[oa]\b|\b3[oa]\b"),
}
_OPTION = re.compile(r"\bopcao\s*(\d)\b")
_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
_HOUR_MINUTE = re.compile(r"\b(\d{1,2})\s*[:h]\s*(\d{2})\b")
_HOUR = re.compile(r"\b(\d{1,2})\s*h(?:s|rs|oras?)?\b|\bas\s+(\d{1,2})\b")

_AFFIRMATIVE = re.compile(
    r"\b(sim|isso|pode ser|pode|perfeito|confirmo|confirmado|ok|okay|beleza|"
    r"fechado|claro|otimo|combinado|esse|essa|esse mesmo|ta bom|tudo bem)\b"
)
# Only unmistakable refusals. A bare "nao" is not one of them: "nao pode ser
# terca?" is someone choosing Tuesday.
_DECLINE = re.compile(
    r"\b(nenhum|nenhuma|outr[oa]s?\s+(horarios?|dias?|datas?|horas?)|"
    r"nao serve|nao posso|nao consigo|nao da|mais tarde|mais cedo)\b"
)
# "mais tarde" is later, not the afternoon — and it is a refusal of these
# times, so reading it as a preference for 14:00 books the opposite of what was
# asked. Same for "mais cedo" and the morning.
_PERIODS = {
    "morning": (re.compile(r"(?<!mais )\b(?:de manha|pela manha|manha)\b"), 0, 11),
    "afternoon": (re.compile(r"(?<!mais )\b(?:de tarde|a tarde|tarde)\b"), 12, 23),
}


def normalize(text: str) -> str:
    """Lowercase, unaccented, punctuation-free — how the patterns above read.

    NFKD also folds the ordinal indicators, so "1º" arrives as "1o" and needs no
    pattern of its own.
    """
    folded = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:/-]", " ", stripped)).strip()


def _local(slot: TimeSlot) -> datetime:
    """The slot as the office sees it — the same clock the offer was read in."""
    return slot.start.astimezone(ZoneInfo(settings.office_timezone))


def _ordinal_in(text: str) -> int | None:
    option = _OPTION.search(text)
    if option:
        return int(option.group(1))
    for index, pattern in _ORDINALS.items():
        if pattern.search(text):
            return index
    # A message that is nothing but a digit is choosing by position. It could in
    # principle be an hour, but nobody says "2" for two in the afternoon — they
    # say "14h" or "2 da tarde", both of which are read as times above.
    if text.isdigit():
        return int(text)
    return None


def names_a_time(message: str) -> bool:
    """Whether the message names a clock time ("às 10h", "9:30")."""
    return bool(_times_in(normalize(message)))


def _times_in(text: str) -> list[tuple[int, int | None]]:
    """Every clock time mentioned, as (hour, minute or None).

    Dates are removed first: "14/09" is a date, and the 14 in it is not 2pm.
    """
    without_dates = _DATE.sub(" ", text)
    found: list[tuple[int, int | None]] = []
    for hour, minute in _HOUR_MINUTE.findall(without_dates):
        found.append((int(hour), int(minute)))
    consumed = _HOUR_MINUTE.sub(" ", without_dates)
    for suffixed, after_as in _HOUR.findall(consumed):
        found.append((int(suffixed or after_as), None))

    afternoon = _PERIODS["afternoon"][0].search(text)
    if afternoon:
        # "2 da tarde" is 14:00. Only ever moves a time forward, so a stated
        # "14h da tarde" is left alone.
        found = [(hour + 12 if hour < 12 else hour, minute) for hour, minute in found]
    return found


_FULL_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DAY_OF_MONTH = re.compile(r"\bdia (\d{1,2})\b(?!/)")


def requested_day(message: str, *, now: datetime | None = None) -> date | None:
    """The day a person asked for, read in code — never left to the model.

    The triage model does not know what today is, so "quinta" or "1/10" came
    back as no date (or last year's), and the desk offered today's times again.
    An explicit date wins over a weekday said with it ("quinta 1/10"). A weekday
    means the next one after today: someone naming today says "hoje". A past
    date is returned as it is, for the caller to say it has passed.
    """
    text = re.sub(r"\bn\b", "nao", normalize(message))
    zone = ZoneInfo(settings.office_timezone)
    today = (now or datetime.now(tz=zone)).astimezone(zone).date()

    full = _FULL_DATE.search(text)
    if full:
        day, month, year = int(full.group(1)), int(full.group(2)), full.group(3)
        if year:
            candidates = [int(year) + (2000 if len(year) == 2 else 0)]
        else:
            # "05/01" said in December is next January, not ten months ago.
            candidates = [today.year, today.year + 1]
        for value in candidates:
            try:
                found = date(value, month, day)
            except ValueError:
                return None
            if year or (today - found).days <= 60:
                return found
        return None

    next_week = re.search(r"\b(semana que vem|proxima semana)\b", text) is not None
    next_monday = today + timedelta(days=8 - today.isoweekday())
    if re.search(r"\bdepois de amanha\b", text):
        return today + timedelta(days=2)
    if re.search(r"\bamanha\b", text):
        return today + timedelta(days=1)
    if re.search(r"\bhoje\b", text):
        return today

    only_day = _DAY_OF_MONTH.search(text)
    if only_day:
        day = int(only_day.group(1))
        month, year = today.month, today.year
        if day < today.day:
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    for weekday, pattern in _WEEKDAYS.items():
        if pattern.search(text):
            if next_week:
                return next_monday + timedelta(days=weekday - 1)
            ahead = (weekday - today.isoweekday()) % 7 or 7
            return today + timedelta(days=ahead)
    if next_week:
        return next_monday
    # Only when no day was named: "amanhã, sem preferência de horário" is
    # tomorrow, the "no preference" being about the time.
    if re.search(r"\b(qualquer dia|tanto faz|sem preferencia|o quanto antes|o mais cedo possivel|"
                 r"mais proximo|primeiro dia livre)\b", text):
        return today
    return None


def interpret(message: str, slots: list[TimeSlot], *, now: datetime | None = None
              ) -> Choice:
    """Read a reply against the times that were actually offered."""
    if not slots:
        return Choice(kind="unclear")

    text = _GREETINGS.sub(" ", normalize(message)).strip()
    # "n" is the common shorthand for "não" in a quick reply ("n consigo",
    # "n posso", "n da") — read the same as the full word.
    text = re.sub(r"\bn\b", "nao", text)

    # An explicit refusal outranks whatever date or time rides along with it:
    # "nao consigo dia 28/09" is turning the whole offer down, not picking
    # among three times that all happen to fall on the 28th.
    if _DECLINE.search(text):
        return Choice(kind="declined")

    zone = ZoneInfo(settings.office_timezone)
    today = (now or datetime.now(tz=zone)).astimezone(zone).date()

    weekdays = {day for day, pattern in _WEEKDAYS.items() if pattern.search(text)}
    dates = {(int(day), int(month)) for day, month in _DATE.findall(text)}
    if re.search(r"\bhoje\b", text):
        dates.add((today.day, today.month))
    if re.search(r"\bamanha\b", text):
        tomorrow = today.toordinal() + 1
        dates.add((datetime.fromordinal(tomorrow).day,
                   datetime.fromordinal(tomorrow).month))
    times = _times_in(text)
    ordinal = _ordinal_in(text)
    periods = [
        (low, high) for _, (pattern, low, high) in _PERIODS.items()
        if pattern.search(text)
    ]

    candidates = list(slots)
    if ordinal is not None:
        # An ordinal outside the offer ("o quinto", of three) says the person is
        # counting something else. Better to ask than to book the nearest guess.
        if not 1 <= ordinal <= len(slots):
            return Choice(kind="unclear", candidates=slots)
        candidates = [slots[ordinal - 1]]
    if dates:
        candidates = [
            slot for slot in candidates
            if (_local(slot).day, _local(slot).month) in dates
        ]
    if weekdays:
        candidates = [slot for slot in candidates if _local(slot).isoweekday() in weekdays]
    if times:
        # "10h" is 10:00. When the page has no 10:00 but has a 10:30, the
        # person did not say which: asking beats booking a time they did not name.
        on_the_hour = [
            slot for slot in candidates
            if any(_local(slot).hour == hour and _local(slot).minute == (minute or 0)
                   for hour, minute in times)
        ]
        same_hour = [
            slot for slot in candidates
            if any(minute is None and _local(slot).hour == hour for hour, minute in times)
        ]
        if not on_the_hour and same_hour:
            return Choice(kind="ambiguous", candidates=same_hour)
        candidates = on_the_hour
    if periods:
        candidates = [
            slot for slot in candidates
            if any(low <= _local(slot).hour <= high for low, high in periods)
        ]

    said_something_specific = bool(weekdays or dates or times or periods) or ordinal
    if said_something_specific:
        if len(candidates) == 1:
            return Choice(kind="picked", slot=candidates[0], candidates=candidates)
        if len(candidates) > 1:
            return Choice(kind="ambiguous", candidates=candidates)
        # Named a time, and it was not one of ours: "quarta" when we offered
        # Monday and Tuesday is a refusal of the offer, not a failure to read it.
        return Choice(kind="declined", candidates=[])

    if _AFFIRMATIVE.search(text):
        # "pode ser" against a single offered time is a choice. Against three it
        # is agreement with the idea of meeting, which is not a booking.
        if len(slots) == 1:
            return Choice(kind="picked", slot=slots[0], candidates=slots)
        return Choice(kind="ambiguous", candidates=slots)
    return Choice(kind="unclear", candidates=slots)


# ── A typed day while the list of days is open (BOOKING_MODE=choose_day) ──────

DayKind = Literal["clear", "mixed", "period_only", "cancel_or_exit", "none"]


class DayReply(BaseModel):
    kind: DayKind
    days: list[date] = Field(default_factory=list)
    period: str | None = None  # "morning", "afternoon", "any"


_NO_PREFERENCE = re.compile(
    r"\b(?:nao tenho preferencia|sem preferencia|qualquer horario|qualquer hora|tanto faz)"
    r"(?: (?:de|do|pelo|por|quanto ao) (?:horario|hora|periodo|turno))?\b"
)
_CANCEL_OR_EXIT = re.compile(r"\b(cancelar|cancela|desmarcar|desmarca|desistir|desisto|"
                             r"sair|parar|esquece)\b")
# A negative next to a day or a period: the desk cannot tell which one was
# refused. A "sim" alone is not one — "sim, pode ser quinta" chooses Thursday.
_NEGATION = re.compile(r"\b(nao|nem|sem ser|exceto|menos)\b")


# What may stand between a weekday and the date it goes with: "quinta 29/10",
# "quinta-feira, dia 29", "29/10, quinta".
_SAME_DAY_GAP = re.compile(r"(?:[\s-]*feira)?[\s,]*(?:dia\s*)?")


def _days_in(text: str, today: date) -> list[date]:
    """Every day named, in the order said, read as requested_day reads one: an
    explicit date wins over the weekday said beside it ("quinta 29/10" is the
    29th; "quinta 29/10 ou sexta" is the 29th and Friday),
    and "semana que vem" moves a weekday to the next week (alone, it is next
    Monday)."""
    noon = datetime.combine(today, datetime.min.time(),
                            tzinfo=ZoneInfo(settings.office_timezone)).replace(hour=12)
    found: list[tuple[int, date]] = []
    dated: list[tuple[int, int]] = []  # where the explicit dates are
    for pattern in (_FULL_DATE, _DAY_OF_MONTH):
        for match in pattern.finditer(text):
            day = requested_day(match.group(0), now=noon)
            if day:
                found.append((match.start(), day))
                dated.append(match.span())
    for match in re.finditer(r"\b(depois de amanha|amanha|hoje)\b", text):
        offset = {"depois de amanha": 2, "amanha": 1, "hoje": 0}[match.group(1)]
        found.append((match.start(), today + timedelta(days=offset)))

    def beside_a_date(start: int, end: int) -> bool:
        """ "quinta 29/10", "29/10, quinta", "quinta, dia 29": one day, not two."""
        return any((end <= d_start and _SAME_DAY_GAP.fullmatch(text[end:d_start])) or
                   (d_end <= start and _SAME_DAY_GAP.fullmatch(text[d_end:start]))
                   for d_start, d_end in dated)

    next_week = re.search(r"\b(semana que vem|proxima semana)\b", text)
    next_monday = today + timedelta(days=8 - today.isoweekday())
    weekdays = 0
    for weekday, pattern in _WEEKDAYS.items():
        for match in pattern.finditer(text):
            weekdays += 1
            if beside_a_date(*match.span()):
                continue
            if next_week:
                found.append((match.start(), next_monday + timedelta(days=weekday - 1)))
            else:
                ahead = (weekday - today.isoweekday()) % 7 or 7
                found.append((match.start(), today + timedelta(days=ahead)))
    if next_week and not weekdays and not found:
        found.append((next_week.start(), next_monday))
    days: list[date] = []
    for _, day in sorted(found, key=lambda item: item[0]):
        if day not in days:
            days.append(day)
    return days


def read_day_reply(message: str, *, now: datetime | None = None) -> DayReply:
    """What a typed answer to the list of days says — and only what it says
    clearly. A day or a period said together with a "não", "nem" or "sim" is
    `mixed`: the desk asks for a tap rather than guess which one was refused.
    The order of the rules decides (the escolher-dia spec's table)."""
    text = re.sub(r"\bn\b", "nao", _GREETINGS.sub(" ", normalize(message)))
    period: str | None = None
    if _NO_PREFERENCE.search(text):
        period = "any"
        text = _NO_PREFERENCE.sub(" ", text)
    if _CANCEL_OR_EXIT.search(text):
        return DayReply(kind="cancel_or_exit")
    zone = ZoneInfo(settings.office_timezone)
    today = (now or datetime.now(tz=zone)).astimezone(zone).date()
    days = _days_in(text, today)
    named = next((name for name, (pattern, _, _) in _PERIODS.items() if pattern.search(text)), None)
    if (days or named) and _NEGATION.search(text):
        return DayReply(kind="mixed")
    period = named or period
    if days:
        return DayReply(kind="clear", days=days, period=period)
    if period:
        return DayReply(kind="period_only", period=period)
    return DayReply(kind="none")
