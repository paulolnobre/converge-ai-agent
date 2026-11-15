"""Offering times, booking them, and undoing that.

The calendar is the source of truth throughout. Availability is read from it
every time — never from the appointments table — because the person whose diary
it is books things in it directly, and an agent working from a stale mirror
double-books them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core import content
from core.channels import channel_of
from core.config import settings
from core.integrations import workspace
from core.integrations.workspace import CalendarEvent, TimeSlot
from core.integrations.workspace.errors import audit_detail
from core.integrations.workspace.slots import free_slots
from core.models import Appointment, BookingResult, Contact, Offer
from core.repositories import appointments as appointments_repo
from core.repositories import audit
from core.repositories import offers as offers_repo
from core.schedule import is_workday

logger = logging.getLogger(__name__)


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.office_timezone)


def calendars_in_scope(staff_calendar_ids: list[str] | None = None) -> list[str]:
    """Whose diaries decide availability.

    Configured staff calendars if there are any, otherwise the reception
    calendar alone — a one-person office should not have to configure a list.
    """
    if staff_calendar_ids:
        return staff_calendar_ids
    configured = [
        value.strip() for value in settings.staff_calendar_ids.split(",") if value.strip()
    ]
    if configured:
        return configured
    return [settings.reception_calendar_id] if settings.reception_calendar_id else []


def _in_period(slot: TimeSlot, period: str, zone: ZoneInfo) -> bool:
    """Morning ends at noon, afternoon starts there — office hours never reach
    evening, so those two buckets are the whole day."""
    hour = slot.start.astimezone(zone).hour
    return hour < 12 if period == "morning" else hour >= 12


async def offer_times(
    *,
    preferred_day=None,
    preferred_period: str | None = None,
    duration_minutes: int | None = None,
    calendar_ids: list[str] | None = None,
    now: datetime | None = None,
) -> list[TimeSlot]:
    """Times that are genuinely free across every calendar in scope.

    Free means free for all of them: a slot offered because one person is
    available, when the meeting needs two, is a slot that gets cancelled.
    """
    calendars = calendars_in_scope(calendar_ids)
    if not calendars:
        raise ValueError("No calendar configured. Set RECEPTION_CALENDAR_ID.")

    zone = _zone()
    moment = now or datetime.now(tz=zone)
    # Never offer something starting in the next few minutes: the person needs
    # time to get there, and the staff member time to see it.
    earliest = moment + timedelta(minutes=settings.minimum_notice_minutes)
    start = earliest
    if preferred_day:
        candidate = datetime.combine(
            preferred_day, datetime.min.time(), tzinfo=zone
        )
        start = max(candidate, earliest)
    end = start + timedelta(days=settings.booking_horizon_days)

    busy_by_calendar = await workspace.calendar().busy_periods(calendars, start, end)
    busy: list[TimeSlot] = []
    for periods in busy_by_calendar.values():
        busy.extend(periods)

    filtered = preferred_period in ("morning", "afternoon")
    # Ask for more than we will show, so closed days, half of every day when a
    # period is chosen, and the slots skipped for sitting too close together
    # can all be dropped below and the caller is still offered a full set.
    spacing = max(1, settings.slot_min_gap_minutes // max(1, settings.slot_step_minutes))
    slots = free_slots(
        busy,
        start,
        end,
        duration_minutes or settings.appointment_duration_minutes,
        step_minutes=settings.slot_step_minutes,
        limit=settings.slots_offered * 6 * (2 if filtered else 1) * spacing,
        now=earliest,
    )
    open_slots = [slot for slot in slots if is_workday(slot.start.date())]
    if preferred_period in ("morning", "afternoon"):
        open_slots = [
            slot for slot in open_slots if _in_period(slot, preferred_period, zone)
        ]
    return _spread(open_slots, settings.slots_offered, settings.slot_min_gap_minutes)


async def available_starts(*, period: str | None, calendars: list[str] | None = None,
                           now: datetime | None = None) -> list[TimeSlot]:
    """Every free start of the horizon, for choosing the day and then the time.

    The rules of offer_times — the calendars in scope and the union of their
    busy times, duration, grid, office hours, workdays, notice — but every
    start, not three spread out. The horizon runs from today to today +
    booking_horizon_days, inclusive, in the office's zone: it does not move with
    the page or with a day the person asked for. One call to the provider.
    """
    calendar_list = calendars_in_scope(calendars)
    if not calendar_list:
        raise ValueError("No calendar configured. Set RECEPTION_CALENDAR_ID.")
    zone = _zone()
    moment = (now or datetime.now(tz=zone)).astimezone(zone)
    earliest = moment + timedelta(minutes=settings.minimum_notice_minutes)
    last_day = moment.date() + timedelta(days=settings.booking_horizon_days)
    end = datetime.combine(last_day, datetime.max.time(), tzinfo=zone)

    busy_by_calendar = await workspace.calendar().busy_periods(calendar_list, earliest, end)
    busy = [period for periods in busy_by_calendar.values() for period in periods]
    slots = free_slots(
        busy, earliest, end, settings.appointment_duration_minutes,
        step_minutes=settings.slot_step_minutes, limit=100_000, now=earliest,
    )
    return [
        slot for slot in slots
        if is_workday(slot.start.astimezone(zone).date())
        and (period not in ("morning", "afternoon") or _in_period(slot, period, zone))
    ]


def _spread(slots: list[TimeSlot], count: int, gap_minutes: int) -> list[TimeSlot]:
    """The earliest `count` slots at least `gap_minutes` apart.

    Earliest still wins, so a person is offered the soonest real choice — just
    not three neighbours of the same half hour.
    """
    chosen: list[TimeSlot] = []
    for slot in slots:
        if chosen and slot.start - chosen[-1].start < timedelta(minutes=gap_minutes):
            continue
        chosen.append(slot)
        if len(chosen) == count:
            break
    return chosen


async def is_free(slot: TimeSlot, calendar_ids: list[str] | None = None) -> bool:
    """Whether every calendar in scope is still free for exactly this slot.

    Read again at the moment of booking rather than trusted from the offer:
    thirty seconds is enough for the staff member to take the time themselves,
    and the offer may have been sitting unanswered for half an hour.
    """
    calendars = calendars_in_scope(calendar_ids)
    if not calendars:
        raise ValueError("No calendar configured. Set RECEPTION_CALENDAR_ID.")
    busy_by_calendar = await workspace.calendar().busy_periods(
        calendars, slot.start, slot.end
    )
    for periods in busy_by_calendar.values():
        for period in periods:
            if period.start < slot.end and slot.start < period.end:
                return False
    return True


async def book(
    conn,
    *,
    contact: Contact,
    slot: TimeSlot,
    title: str,
    calendar_id: str | None = None,
    staff_id: int | None = None,
    description: str | None = None,
    verify: bool = True,
) -> BookingResult:
    """Re-check the slot, create the event, then mirror it.

    The order matters twice over. The calendar is re-read first, because an
    offer is not a hold and the time may be gone. Then the event is created
    before the database row: if the calendar write fails there must be no row
    claiming an appointment exists, and the reverse order produces a confirmed
    booking nobody's diary knows about.

    The last guard is the database. Two people can both read the calendar as
    free in the same second, so the row, not the read, is what decides — and the
    one that loses has its calendar event cancelled again here.
    """
    calendar = calendar_id or settings.reception_calendar_id
    if not calendar:
        raise ValueError("No calendar configured. Set RECEPTION_CALENDAR_ID.")

    provider = workspace.configuration_report()["capabilities"]["calendar"]

    if verify:
        try:
            if not await is_free(slot):
                logger.info("Slot %s was taken before it could be booked.", slot.start)
                return BookingResult(outcome="taken")
        except workspace.WorkspaceError as exc:
            logger.error("Could not re-check the slot: %s", exc)
            await audit.record(
                conn, provider=provider, capability="calendar", action="create_event",
                target=calendar, contact_id=contact.id, succeeded=False,
                detail=f"availability re-check failed: {exc}",
            )
            return BookingResult(outcome="failed")

    attendees = []
    if contact.email:
        from core.integrations.workspace import Attendee

        attendees.append(Attendee(email=contact.email, name=contact.name))

    try:
        created = await workspace.calendar().create_event(
            calendar,
            CalendarEvent(
                title=title,
                description=description,
                start=slot.start,
                end=slot.end,
                attendees=attendees,
                online_meeting=settings.online_meetings,
            ),
        )
    except workspace.WorkspaceError as exc:
        logger.error("Could not create the calendar event: %s", exc)
        await audit.record(
            conn, provider=provider, capability="calendar", action="create_event",
            target=calendar, contact_id=contact.id, succeeded=False, detail=audit_detail(exc),
        )
        return BookingResult(outcome="failed")

    appointment = await appointments_repo.record(
        conn,
        Appointment(
            contact_id=contact.id,
            staff_id=staff_id,
            calendar_id=calendar,
            provider=provider,
            provider_event_id=created.id,
            title=title,
            starts_at=slot.start,
            ends_at=slot.end,
            meeting_url=created.meeting_link,
            test_mode=contact.test_mode is not None,
            booked_via=channel_of(contact.chat_id),
        ),
    )
    if appointment is None:
        # Someone else's row got there first. Undo our event, or the slot stays
        # blocked in the diary by an appointment no table knows about.
        await _undo(conn, calendar, created.id, contact.id)
        return BookingResult(outcome="taken")

    await audit.record(
        conn, provider=provider, capability="calendar", action="create_event",
        target=created.id, contact_id=contact.id, staff_id=staff_id, succeeded=True,
    )
    return BookingResult(outcome="booked", appointment=appointment)


async def _undo(conn, calendar: str, event_id: str | None, contact_id: int | None
                ) -> None:
    """Remove an event whose database row could not be written."""
    provider = workspace.configuration_report()["capabilities"]["calendar"]
    succeeded = False
    detail = "lost the slot race; event withdrawn"
    if event_id:
        try:
            await workspace.calendar().cancel_event(calendar, event_id)
            succeeded = True
        except workspace.WorkspaceError as exc:
            # Nothing left to try automatically. The audit row is how a person
            # finds the orphan event and removes it by hand.
            logger.error("Could not withdraw the losing event %s: %s", event_id, exc)
            detail = f"lost the slot race and the event could not be withdrawn: {exc}"
    await audit.record(
        conn, provider=provider, capability="calendar", action="cancel_event",
        target=event_id, contact_id=contact_id, succeeded=succeeded, detail=detail,
    )


async def cancel(conn, appointment: Appointment) -> bool:
    """Cancel in the calendar first, then locally.

    A row marked cancelled while the event still sits in someone's diary is the
    worse of the two failures: the staff member keeps the slot blocked and the
    agent believes it is free.
    """
    provider = appointment.provider
    if appointment.provider_event_id:
        try:
            await workspace.calendar().cancel_event(
                appointment.calendar_id, appointment.provider_event_id
            )
        except workspace.WorkspaceError as exc:
            logger.error("Could not cancel the calendar event: %s", exc)
            await audit.record(
                conn, provider=provider, capability="calendar", action="cancel_event",
                target=appointment.provider_event_id, contact_id=appointment.contact_id,
                succeeded=False, detail=audit_detail(exc),
            )
            return False

    cancelled = await appointments_repo.cancel(conn, appointment.id or 0)
    await audit.record(
        conn, provider=provider, capability="calendar", action="cancel_event",
        target=appointment.provider_event_id, contact_id=appointment.contact_id,
        succeeded=cancelled,
    )
    return cancelled


async def offer_and_remember(
    conn,
    *,
    contact: Contact,
    chat_id,
    preferred_day=None,
    preferred_period: str | None = None,
    calendar_id: str | None = None,
    staff_id: int | None = None,
    replaces_id: int | None = None,
    now: datetime | None = None,
) -> tuple[list[TimeSlot], Offer | None]:
    """Offer times and write down which ones, so the next message can pick one.

    The two halves belong together. Offering without recording is what makes a
    desk repeat itself: the person answers "a terca", nothing knows what that
    refers to, and the same three times go out again.
    """
    slots = await offer_times(
        preferred_day=preferred_day, preferred_period=preferred_period, now=now
    )
    if not slots:
        return [], None

    # The period chosen stays with the offer: a time taken meanwhile is offered
    # again from the same morning or afternoon.
    period = preferred_period if preferred_period in ("morning", "afternoon") else None
    offer = await remember_offer(conn, contact=contact, chat_id=chat_id, slots=slots,
                                 period=period, calendar_id=calendar_id, staff_id=staff_id,
                                 replaces_id=replaces_id)
    return slots, offer


async def remember_offer(conn, *, contact: Contact, chat_id, slots: list[TimeSlot],
                         period: str | None = None, calendar_id: str | None = None,
                         staff_id: int | None = None, replaces_id: int | None = None) -> Offer:
    """Write down which times were put in front of someone (superseding the
    last ones), so the next tap or message can pick one."""
    calendar = calendar_id or settings.reception_calendar_id
    if not calendar:
        raise ValueError("No calendar configured. Set RECEPTION_CALENDAR_ID.")
    replaces_starts_at = None
    if replaces_id:
        replacing = await appointments_repo.get_by_id(conn, replaces_id)
        replaces_starts_at = replacing.starts_at if replacing else None
    return await offers_repo.save(
        conn,
        Offer(contact_id=contact.id, chat_id=str(chat_id), slots=slots, calendar_id=calendar,
              staff_id=staff_id, replaces_id=replaces_id, period=period,
              replaces_starts_at=replaces_starts_at),
        settings.offer_ttl_minutes,
    )


async def limit_reached(conn, contact: Contact) -> list[Appointment] | None:
    """The appointments that use up this contact's limit, or None when a new
    booking is allowed (no limit, or below it)."""
    limit = settings.max_upcoming_appointments
    if not limit or contact.id is None:
        return None
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id)
    return upcoming if len(upcoming) >= limit else None


async def take_offer(conn, *, offer: Offer, contact: Contact, slot: TimeSlot,
                     title: str) -> BookingResult:
    """Book the slot the person picked out of an offer, exactly once.

    The offer is claimed before the calendar is touched, so the same answer
    arriving twice — a person who pressed send again because the first looked
    slow — produces one booking attempt. A claim that fails to turn into a
    booking is put back, or the next thing they say has nothing to match.
    """
    limit = settings.max_upcoming_appointments
    if offer.replaces_id or limit:
        # Under the contact's lock, read the facts the booking depends on, so
        # two taps in two lists cannot both pass.
        await appointments_repo.lock_contact(conn, contact.id or 0)
    if offer.replaces_id:
        moving = await appointments_repo.get_by_id(conn, offer.replaces_id)
        # Moved since the offer was made (in Google, by the calendar sync):
        # the times offered were for another situation.
        if (moving is None or moving.contact_id != contact.id or moving.status != "confirmed"
                or moving.starts_at <= datetime.now(tz=_zone())
                or (offer.replaces_starts_at is not None
                    and moving.starts_at != offer.replaces_starts_at)):
            await offers_repo.expire(conn, offer.chat_id)
            return BookingResult(outcome="stale_reschedule")
    if limit and await appointments_repo.count_upcoming(
            conn, contact.id or 0, excluding=offer.replaces_id) >= limit:
        await offers_repo.expire(conn, offer.chat_id)
        return BookingResult(outcome="limit")

    if offer.id is None or not await offers_repo.claim(conn, offer.id):
        logger.info("Offer for chat %s was already used.", offer.chat_id)
        return BookingResult(outcome="failed")

    result = await book(
        conn, contact=contact, slot=slot, title=title,
        calendar_id=offer.calendar_id, staff_id=offer.staff_id,
    )
    if result.outcome != "booked":
        await offers_repo.release(conn, offer.id)
        return result

    if offer.replaces_id:
        await _replace(conn, offer.replaces_id, contact=contact, new=slot)
    return result


async def _replace(conn, appointment_id: int, *, contact: Contact, new: TimeSlot) -> None:
    """Cancel the appointment a reschedule replaces, now that the new one exists.

    New first, old second, always. The reverse order can leave someone with
    neither — cancelled out of their original time, and unable to take the new
    one because the calendar refused it.

    When the calendar refuses to cancel the old one, its row is cancelled here
    anyway and the team is told to remove the event by hand: the person gets no
    reminder for it, it does not count in their limit, and the calendar —
    where availability is read — still blocks that time until someone deletes
    it. `cancel` has already written the failed audit row.
    """
    previous = await appointments_repo.get_by_id(conn, appointment_id)
    if previous is None:
        return
    if not await cancel(conn, previous):
        logger.error(
            "Rescheduled appointment %s could not be cancelled in the calendar; "
            "cancelled here and the team told.", appointment_id,
        )
        await appointments_repo.cancel(conn, appointment_id)
        from core.services import inbox  # services import each other; late to avoid a cycle

        await inbox.notify_team(
            content.render("booking.team_cancel_failed", quando=when_appointment(previous),
                           cliente=contact.name or "cliente", quando_nova=when(new)),
            contact.id,
        )


_DAYS = (
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
)


def when_parts(slot: TimeSlot) -> dict[str, str]:
    """One time, split into the pieces the organization's templates name.

    Every message about a slot is rendered from these, so the day a person is
    offered, the day they confirm and the day they are reminded of are the same
    words in all three.
    """
    local = slot.start.astimezone(_zone())
    return {
        "data": local.strftime("%d/%m"),
        "dia_semana": _DAYS[local.weekday()],
        "horario": local.strftime("%H:%M"),
    }


def when(slot: TimeSlot) -> str:
    """One time, as the desk says it out loud."""
    parts = when_parts(slot)
    return f"{parts['dia_semana']} ({parts['data']}) às {parts['horario']}"


def short_when(slot: TimeSlot) -> str:
    """One time, compact — a button label has no room for a full sentence."""
    parts = when_parts(slot)
    return f"{parts['data']} {parts['horario']}"


def phrase(slots: list[TimeSlot]) -> str:
    """The offered times as a list a person would read aloud.

    Just the times: the sentence around them belongs to the organization, in
    content/messages.toml, not here.
    """
    parts = [when(slot) for slot in slots]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} ou {parts[-1]}"


def with_meeting_link(text: str, appointment: Appointment | None) -> str:
    """The message, and under it how to join — when the booking has a link."""
    if appointment is None or not appointment.meeting_url:
        return text
    return f"{text}\n\n{content.render('scheduling.meeting_link', link=appointment.meeting_url)}"


def when_appointment(appointment: Appointment) -> str:
    """The same phrasing, for something already in the diary."""
    return when(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))


