"""The bot's meetings follow the calendar (agenda no painel, increment 2).

For each confirmed appointment with an event in a calendar, after that
calendar's listing: moved in Google → the new time, and the reminder (and the
"como foi?", when it ends in the future) due again; gone → cancelled. Never on
a guess:
- `Gone` (the provider says cancelled) is proof;
- `NotFound` counts only on a second pass with good listings both times;
- any error changes nothing.
Every change applies under the contact's and the event's locks, and only to a
row still as it was read before asking the provider. No message to the
customer: whoever moved it in Google talked to them. Logs carry ids, never a
title.
"""
from __future__ import annotations

import logging

from psycopg.errors import UniqueViolation

from core.integrations import workspace
from core.integrations.workspace.models import CalendarEvent, Gone, NotFound
from core.models import Appointment
from core.repositories import appointments as appointments_repo
from core.repositories import audit
from core.services import funnel

logger = logging.getLogger(__name__)


async def _cancel(conn, provider: str, meeting: Appointment, expected: tuple) -> None:
    if await appointments_repo.cancel_from_provider(conn, meeting.id or 0, expected):
        await funnel.step_back_after_cancel(conn, meeting.contact_id)
        await audit.record(conn, provider=provider, capability="calendar", action="sync_cancel",
                           target=meeting.provider_event_id, contact_id=meeting.contact_id)


async def _apply(conn, provider: str, meeting: Appointment,
                 found: CalendarEvent | Gone | NotFound, listing_ok: bool) -> bool:
    """One appointment, in its own transaction. Returns False for a start
    collision (the pass is then not a success)."""
    appointment_id = meeting.id or 0
    expected = (meeting.starts_at, meeting.status, meeting.provider_etag)
    async with conn.transaction():
        if meeting.contact_id is not None:
            await appointments_repo.lock_contact(conn, meeting.contact_id)
        await appointments_repo.lock_event(conn, meeting.calendar_id, meeting.provider_event_id or "")
        if isinstance(found, Gone):
            await _cancel(conn, provider, meeting, expected)
            return True
        if isinstance(found, NotFound):
            if not listing_ok:
                return True  # an unreadable calendar proves nothing
            if meeting.provider_missing_at is not None:
                await _cancel(conn, provider, meeting, expected)
            else:
                await appointments_repo.mark_missing(conn, appointment_id, expected)
            return True
        if found.all_day:
            await appointments_repo.set_sync_problem(conn, appointment_id, expected, "all_day")
            return True
        if not found.organizer_is_self:
            await appointments_repo.set_sync_problem(conn, appointment_id, expected, "not_organizer")
            return True
        if meeting.sync_problem in ("all_day", "not_organizer"):
            await appointments_repo.set_sync_problem(conn, appointment_id, expected, None)
        if found.start != meeting.starts_at or found.end != meeting.ends_at:
            try:
                async with conn.transaction():
                    if found.start != meeting.starts_at:
                        moved = await appointments_repo.apply_move(
                            conn, appointment_id, expected, found.start, found.end, found.etag)
                    else:
                        moved = await appointments_repo.apply_duration(
                            conn, appointment_id, expected, found.end, found.etag)
            except UniqueViolation:
                await appointments_repo.set_sync_problem(conn, appointment_id, expected,
                                                         "start_collision")
                logger.warning("Appointment %s: its new time collides with another", appointment_id)
                return False
            if moved:
                await audit.record(conn, provider=provider, capability="calendar",
                                   action="sync_update", target=meeting.provider_event_id,
                                   contact_id=meeting.contact_id)
            return True
        if (found.etag != meeting.provider_etag or meeting.provider_missing_at is not None
                or meeting.sync_problem == "start_collision"):
            await appointments_repo.apply_etag(conn, appointment_id, expected, found.etag)
        return True


async def reconcile_calendar(conn, calendar_id: str, provider: str,
                             listed: dict[str, CalendarEvent] | None,
                             snapshot: list[Appointment], listing_ok: bool) -> int:
    """Every appointment of the snapshot (read before the listing) against
    what the provider says now. `listed` is the calendar's listing by event
    id, or None when it could not be read. Returns how many failed."""
    errors = 0
    for meeting in snapshot:
        if not meeting.provider_event_id or meeting.id is None:
            continue
        found: CalendarEvent | Gone | NotFound | None = (listed or {}).get(meeting.provider_event_id)
        try:
            if found is None:
                found = await workspace.calendar().get_event(calendar_id, meeting.provider_event_id)
            if not await _apply(conn, provider, meeting, found, listing_ok):
                errors += 1
        except Exception as exc:
            errors += 1
            logger.warning("Appointment %s could not be checked against calendar %s (%s)",
                           meeting.id, calendar_id, type(exc).__name__)
    return errors
