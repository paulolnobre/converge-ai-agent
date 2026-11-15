"""Appointments, mirrored from the calendar that actually owns them.

The calendar is the source of truth. These rows exist so the agent can answer
"what did I book for this person?" without a round-trip, and so a reminder can be
sent exactly once.
"""
from __future__ import annotations

import logging
from datetime import datetime

from psycopg.errors import UniqueViolation

from core.crypto import SealedDataError
from core.models import Appointment
from core.sealed_fields import field, read, write

logger = logging.getLogger(__name__)

_TITLE, _MEETING_URL = field("appointments", "title"), field("appointments", "meeting_url")
_COLUMNS = (
    "id, contact_id, staff_id, calendar_id, provider, provider_event_id, "
    "title, title_sealed, starts_at, ends_at, status, meeting_url, meeting_url_sealed, test_mode, "
    "booked_via, provider_etag, provider_missing_at, sync_problem, moved_at"
)


def _to_appointment(row) -> Appointment:
    return Appointment(
        id=row[0], contact_id=row[1], staff_id=row[2], calendar_id=row[3],
        provider=row[4], provider_event_id=row[5],
        title=read(_TITLE, row[6], row[7], row[0]) or "",
        starts_at=row[8], ends_at=row[9], status=row[10],
        meeting_url=read(_MEETING_URL, row[11], row[12], row[0]), test_mode=row[13],
        booked_via=row[14], provider_etag=row[15], provider_missing_at=row[16],
        sync_problem=row[17], moved_at=row[18],
    )


def _appointments(rows) -> list[Appointment]:
    """A list for a sweep or a count: an unreadable row is left out and named
    in the log, so one bad envelope does not stop the reminders of everyone."""
    kept = []
    for row in rows:
        try:
            kept.append(_to_appointment(row))
        except SealedDataError as exc:
            logger.error("Agendamento ilegível ignorado: %s", exc)
    return kept


# The partial unique index that stops two people taking the same slot. Named
# here because the only way to tell that collision from any other is by name.
_SLOT_INDEX = "idx_appointments_slot"
# The first key of the per-contact booking lock (pg_advisory_xact_lock(a, b)):
# the contact id is the second, so this never meets another lock's numbers.
_BOOKING_LOCK = 7_411_301


async def record(conn, appointment: Appointment) -> Appointment | None:
    """Store a booking, or return None when that slot is already confirmed.

    Two uniqueness rules meet here, and they mean different things. The provider
    event id is a retry: a booking whose response was lost must link to the
    event that already exists rather than create a second one, which is what the
    ON CONFLICT clause does. The slot index is a race: someone else's booking
    landed first, and there is nothing to link to — the caller has to undo the
    calendar event it just created and apologize.

    Wrapped in a savepoint so that losing the race does not abort the whole
    update's transaction along with it.
    """
    try:
        async with conn.transaction():
            return await _insert(conn, appointment)
    except UniqueViolation as exc:
        if getattr(exc.diag, "constraint_name", None) != _SLOT_INDEX:
            raise
        logger.info(
            "Slot %s on %s was confirmed by someone else first.",
            appointment.starts_at, appointment.calendar_id,
        )
        return None


async def _insert(conn, appointment: Appointment) -> Appointment:
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO appointments
                (contact_id, staff_id, calendar_id, provider, provider_event_id,
                 title, title_sealed, starts_at, ends_at, status,
                 meeting_url, meeting_url_sealed, test_mode, booked_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provider_event_id) DO UPDATE SET
                starts_at = EXCLUDED.starts_at,
                ends_at   = EXCLUDED.ends_at,
                title     = EXCLUDED.title,
                title_sealed = EXCLUDED.title_sealed,
                status    = EXCLUDED.status,
                meeting_url = EXCLUDED.meeting_url,
                meeting_url_sealed = EXCLUDED.meeting_url_sealed
            RETURNING {_COLUMNS}
            """,
            (
                appointment.contact_id, appointment.staff_id, appointment.calendar_id,
                appointment.provider, appointment.provider_event_id,
                *write(_TITLE, appointment.title).values(),
                appointment.starts_at, appointment.ends_at, appointment.status,
                *write(_MEETING_URL, appointment.meeting_url).values(),
                appointment.test_mode, appointment.booked_via,
            ),
        )
        row = await cur.fetchone()
    return _to_appointment(row)


async def upcoming_for_contact(conn, contact_id: int) -> list[Appointment]:
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM appointments "
            "WHERE contact_id = %s AND status = 'confirmed' AND starts_at >= NOW() "
            "ORDER BY starts_at",
            (contact_id,),
        )
        rows = await cur.fetchall()
    return _appointments(rows)


async def just_booked(conn, contact_id: int, *, minutes: int = 120) -> Appointment | None:
    """The meeting this contact booked in the last `minutes`, while nothing has
    been said since its confirmation (written with it, in the same moment)."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM appointments a "
            "WHERE a.contact_id = %s AND a.status = 'confirmed' AND a.starts_at >= NOW() "
            "AND a.created_at > NOW() - make_interval(mins => %s) "
            "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.contact_id = a.contact_id "
            "                AND s.created_at > a.created_at + INTERVAL '10 seconds') "
            "ORDER BY a.created_at DESC LIMIT 1",
            (contact_id, minutes),
        )
        rows = await cur.fetchall()
    found = _appointments(rows)
    return found[0] if found else None


async def get_by_id(conn, appointment_id: int) -> Appointment | None:
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM appointments WHERE id = %s", (appointment_id,)
        )
        row = await cur.fetchone()
    return _to_appointment(row) if row else None


async def cancel(conn, appointment_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE appointments SET status = 'cancelled' "
            "WHERE id = %s AND status = 'confirmed'",
            (appointment_id,),
        )
        return cur.rowcount > 0


async def due_reminders(conn, hours_before: int, late_minutes: int = 60,
                        limit: int = 100) -> list[Appointment]:
    """Confirmed appointments whose reminder is due, not yet reminded.

    Booked well ahead: reminded `hours_before` before. Booked inside that
    window: reminded `late_minutes` before instead — otherwise the reminder
    landed minutes after the confirmation, for a meeting the person had just
    agreed to.

    The `reminder_sent_at IS NULL` clause is what stops a restart from sending
    the same reminder again — a duplicate reminder reads as a double booking.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_COLUMNS} FROM appointments
             WHERE status = 'confirmed'
               AND reminder_sent_at IS NULL
               AND sync_problem IS NULL
               AND NOT EXISTS (SELECT 1 FROM contacts bc WHERE bc.id = appointments.contact_id
                                AND bc.blocked_at IS NOT NULL)
               AND starts_at > NOW()
               AND starts_at <= NOW() + CASE
                     WHEN starts_at - created_at >= make_interval(hours => %s)
                       THEN make_interval(hours => %s)
                     ELSE make_interval(mins => %s)
                   END
             ORDER BY starts_at
             LIMIT %s
            """,
            (hours_before, hours_before, late_minutes, limit),
        )
        rows = await cur.fetchall()
    return _appointments(rows)


async def mark_reminded(conn, appointment_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE appointments SET reminder_sent_at = NOW() WHERE id = %s",
            (appointment_id,),
        )


async def confirm_attendance(conn, appointment_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE appointments SET attendance_confirmed_at = NOW() "
            "WHERE id = %s AND status = 'confirmed' AND starts_at > NOW()",
            (appointment_id,),
        )
        return cur.rowcount > 0


async def due_outcomes(conn, limit: int = 50) -> list[Appointment]:
    """Confirmed appointments that have ended and nobody was asked about yet.

    Only the last week: on a first deploy, a year of history must not arrive in
    the team group as a year of questions. Never a test's meeting (/teste): it
    is not asked about, and would take a place among the fifty.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_COLUMNS} FROM appointments
             WHERE status = 'confirmed'
               AND outcome_asked_at IS NULL
               AND NOT test_mode
               AND ends_at < NOW()
               AND ends_at > NOW() - INTERVAL '7 days'
             ORDER BY ends_at
             LIMIT %s
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return _appointments(rows)


async def mark_outcome_asked(conn, appointment_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE appointments SET outcome_asked_at = NOW() WHERE id = %s",
            (appointment_id,),
        )


async def set_outcome(conn, appointment_id: int, status: str, by: str) -> bool:
    """done or no_show, once: the first answer wins, a second tap changes nothing."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE appointments SET status = %s, outcome_by = %s "
            "WHERE id = %s AND status = 'confirmed' AND ends_at < NOW()",
            (status, by, appointment_id),
        )
        return cur.rowcount > 0


async def lock_contact(conn, contact_id: int) -> None:
    """Hold this contact's bookings until the transaction ends: two taps in two
    lists wait for each other instead of both passing the limit."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_BOOKING_LOCK, contact_id))


async def count_upcoming(conn, contact_id: int, *, excluding: int | None = None) -> int:
    """Confirmed appointments ahead for this contact, leaving one out (the one
    a reschedule replaces)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM appointments WHERE contact_id = %s AND status = 'confirmed' "
            "AND starts_at > NOW() AND id IS DISTINCT FROM %s",
            (contact_id, excluding),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


# ── Following the calendar ────────────────────────────────────────────────────
# Every change from the sync applies only while the row is still what the sync
# read before asking the provider: (starts_at, status, provider_etag). A row
# the panel or the bot changed meanwhile waits for the next pass.

_EVENT_LOCK = 7411303
_SAME = "id = %s AND status = 'confirmed' AND starts_at = %s AND status = %s " \
        "AND provider_etag IS NOT DISTINCT FROM %s"


async def sync_targets(conn, calendar_id: str) -> list[Appointment]:
    """Future and recently ended meetings, whether "como foi?" was asked or not.

    Follow the mirror's 14-day history window, with no upper bound: an event
    moved beyond the listing window must still be fetched individually.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM appointments WHERE calendar_id = %s AND status = 'confirmed' "
            f"AND provider_event_id IS NOT NULL AND ends_at > NOW() - INTERVAL '14 days' "
            f"ORDER BY id", (calendar_id,))
        rows = await cur.fetchall()
    return _appointments(rows)


async def calendars_with_targets(conn) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT calendar_id FROM appointments WHERE status = 'confirmed' "
                          "AND provider_event_id IS NOT NULL "
                          "AND ends_at > NOW() - INTERVAL '14 days' ORDER BY calendar_id")
        return [row[0] for row in await cur.fetchall()]


async def lock_event(conn, calendar_id: str, provider_event_id: str) -> None:
    """The event's lock, always taken after the contact's (lock_contact)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                          (_EVENT_LOCK, f"{calendar_id}:{provider_event_id}"))


async def _update(conn, appointment_id: int, expected: tuple, assignments: str,
                  params: tuple = ()) -> bool:
    starts_at, status, etag = expected
    async with conn.cursor() as cur:
        await cur.execute(f"UPDATE appointments SET {assignments} WHERE {_SAME}",
                          (*params, appointment_id, starts_at, status, etag))
        return cur.rowcount > 0


async def apply_move(conn, appointment_id: int, expected: tuple, start: datetime,
                     end: datetime, etag: str | None) -> bool:
    """Moved in the calendar: the new time, the reminder and the attendance
    answer due again, and — when it now ends in the future — the "como foi?"
    too. A collision and a missing mark are over."""
    return await _update(
        conn, appointment_id, expected,
        "starts_at = %s, ends_at = %s, provider_etag = %s, moved_at = clock_timestamp(), "
        "provider_missing_at = NULL, "
        "sync_problem = CASE WHEN sync_problem = 'start_collision' THEN NULL ELSE sync_problem END, "
        "reminder_sent_at = CASE WHEN %s > NOW() THEN NULL ELSE reminder_sent_at END, "
        "attendance_confirmed_at = CASE WHEN %s > NOW() THEN NULL ELSE attendance_confirmed_at END, "
        "outcome_asked_at = CASE WHEN %s > NOW() THEN NULL ELSE outcome_asked_at END",
        (start, end, etag, end, end, end))


async def apply_etag(conn, appointment_id: int, expected: tuple, etag: str | None) -> bool:
    """Same time, new version (a title, an attendee): only the version. Found
    again, it is no longer missing; back at the stored time, no collision."""
    return await _update(
        conn, appointment_id, expected,
        "provider_etag = %s, provider_missing_at = NULL, "
        "sync_problem = CASE WHEN sync_problem = 'start_collision' THEN NULL ELSE sync_problem END",
        (etag,))


async def apply_duration(conn, appointment_id: int, expected: tuple, end: datetime,
                         etag: str | None) -> bool:
    """Only the duration changed: keep reminders, answers and buttons valid."""
    return await _update(
        conn, appointment_id, expected,
        "ends_at = %s, provider_etag = %s, provider_missing_at = NULL, "
        "sync_problem = CASE WHEN sync_problem = 'start_collision' THEN NULL ELSE sync_problem END",
        (end, etag))


async def set_sync_problem(conn, appointment_id: int, expected: tuple, problem: str | None) -> bool:
    return await _update(conn, appointment_id, expected,
                         "sync_problem = %s, provider_missing_at = NULL", (problem,))


async def mark_missing(conn, appointment_id: int, expected: tuple) -> bool:
    """The first pass that did not find the event; a later one keeps it."""
    return await _update(conn, appointment_id, expected,
                         "provider_missing_at = COALESCE(provider_missing_at, clock_timestamp())")


async def cancel_from_provider(conn, appointment_id: int, expected: tuple) -> bool:
    return await _update(conn, appointment_id, expected, "status = 'cancelled'")


async def claim_reminder(conn, appointment_id: int, starts_at: datetime) -> Appointment | None:
    """The reminder, claimed only for the time that was read: a meeting moved
    meanwhile gets its reminder at the new time instead."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE appointments SET reminder_sent_at = NOW() WHERE id = %s "
            f"AND reminder_sent_at IS NULL AND status = 'confirmed' AND sync_problem IS NULL "
            f"AND starts_at = %s RETURNING {_COLUMNS}", (appointment_id, starts_at))
        row = await cur.fetchone()
    return _to_appointment(row) if row else None


async def claim_outcome(conn, appointment_id: int, starts_at: datetime) -> Appointment | None:
    """"Como foi?", claimed only for a meeting that ended at the time read."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE appointments SET outcome_asked_at = NOW() WHERE id = %s "
            f"AND outcome_asked_at IS NULL AND status = 'confirmed' AND ends_at < NOW() "
            f"AND starts_at = %s RETURNING {_COLUMNS}", (appointment_id, starts_at))
        row = await cur.fetchone()
    return _to_appointment(row) if row else None
