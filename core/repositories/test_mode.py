"""The SQL behind /teste: the contact a team member is tested as, the history
an 'antigo' test starts with, the wipe, and which tests have gone quiet.

The wipe is the part that must never be wrong, so its scope lives in the SQL
itself: a contact in test, or — only when a test is being started — the team
member's own chat. A bug above this module cannot point it at a customer."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from core.channels import channel_of
from core.models import Appointment, Contact
from core.repositories.appointments import _COLUMNS as _APPOINTMENT_COLUMNS
from core.repositories.appointments import _appointments
from core.repositories.contacts import (
    _COLUMNS,
    _to_contact,
    is_staff_sql,
    sealed_assignments,
)
from core.sealed_fields import field, write
from core.services import media_store


async def create(conn, chat_id: str, mode: str, *, name: str | None, email: str | None,
                 phone: str | None, inbound_at: datetime) -> Contact:
    """The contact a test runs as. last_inbound_at is the /teste message's time:
    the wipe before this took the old one, and without it WhatsApp's 24h window
    would look closed and the bot could not answer."""
    assignments, params = sealed_assignments({"name": name, "email": email, "phone": phone})
    columns = [a.split(" = ")[0] for a in assignments]
    async with conn.cursor() as cur:
        await cur.execute(
            f"INSERT INTO contacts (chat_id, {', '.join(columns)}, source, test_mode, "
            "                      test_started_at, last_inbound_at) "
            f"VALUES (%s, {', '.join(['%s'] * len(columns))}, %s, %s, NOW(), %s) "
            f"RETURNING {_COLUMNS}",
            (str(chat_id), *params, channel_of(chat_id), mode, inbound_at),
        )
        row = await cur.fetchone()
    return _to_contact(row)


async def seed_yesterday(conn, contact_id: int, chat_id: str, user_text: str,
                         assistant_text: str) -> None:
    """A short exchange dated yesterday, so an 'antigo' test is greeted as
    someone the desk already talked to."""
    asked, answered = (write(field("sessions", "message"), text)
                       for text in (user_text, assistant_text))
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO sessions (contact_id, chat_id, role, message, message_sealed, intent, "
            "                      created_at) "
            "VALUES (%s, %s, 'user', %s, %s, 'greeting', NOW() - INTERVAL '1 day'), "
            "       (%s, %s, 'assistant', %s, %s, 'greeting', NOW() - INTERVAL '1 day')",
            (contact_id, str(chat_id), *asked.values(),
             contact_id, str(chat_id), *answered.values()),
        )


async def delete(conn, contact_id: int, *, staff_own: bool,
                 idle_minutes: int | None = None, keep_ids: Sequence[int] = ()) -> bool:
    """Wipe a contact in test and everything of theirs, in one statement.

    `staff_own` also allows the team member's own, untested chat — only when
    a test is being started. The cascade from contacts takes offers, handoff
    sessions (and their messages), handoffs and the automation queue; billing
    rows of a test contact are deleted explicitly.

    `idle_minutes`, for the expiry: delete only if the test is still quiet,
    checked in this same statement — a message noted while the calendar was
    being cleaned keeps the test alive, and no lock is held across those calls."""
    idle = f"AND {_IDLE.replace('%s', '%(idle_minutes)s')}" if idle_minutes is not None else ""
    # keep_ids, for the expiry: the appointments whose calendar event is still
    # to cancel stay — marked cancelled here, so no reminder, no "aconteceu?"
    # and no count sees them, and left without a contact by the foreign key —
    # until each event has been handled. A process killed halfway leaves them
    # for the next sweep. Every other appointment goes now.
    appointments = (",\n               a AS (DELETE FROM appointments WHERE contact_id IN "
                    "(SELECT id FROM target) AND NOT (id = ANY(%(keep)s))),\n"
                    "               k AS (UPDATE appointments SET status = 'cancelled' "
                    "WHERE id = ANY(%(keep)s) AND contact_id IN (SELECT id FROM target))")
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            WITH target AS (
                SELECT c.id, c.chat_id FROM contacts c
                 WHERE c.id = %(id)s
                   AND (c.test_mode IS NOT NULL
                        OR (%(staff_own)s AND {is_staff_sql('c.chat_id')}))
                   {idle}
                 FOR UPDATE
            ), tb AS (
                SELECT b.id FROM billing_customers b
                  JOIN contacts c ON c.id = b.contact_id
                 WHERE c.id IN (SELECT id FROM target) AND c.test_mode IS NOT NULL
            ), s AS (DELETE FROM sessions          WHERE chat_id    IN (SELECT chat_id FROM target)),
               w AS (DELETE FROM workspace_actions WHERE contact_id IN (SELECT id FROM target)),
               -- A test's billing has no cascade (it is money, not chat): it goes
               -- here, and only for a contact in test, never a real customer's.
               bc AS (DELETE FROM billing_charges WHERE customer_id IN (SELECT id FROM tb)),
               bs AS (DELETE FROM billing_subscriptions WHERE customer_id IN (SELECT id FROM tb)),
               bu AS (DELETE FROM billing_customers WHERE id IN (SELECT id FROM tb)),
               -- The files kept for the Inbox, seen before the cascade (one
               -- snapshot for the whole statement), removed from disk below.
               mk AS (SELECT storage_key FROM media WHERE contact_id IN (SELECT id FROM target)
                       AND storage_key IS NOT NULL){appointments}
            DELETE FROM contacts WHERE id IN (SELECT id FROM target)
            RETURNING id, (SELECT array_agg(storage_key) FROM mk)
            """,
            {"id": contact_id, "staff_own": staff_own, "idle_minutes": idle_minutes,
             "keep": list(keep_ids)},
        )
        row = await cur.fetchone()
    if row is None:
        return False
    # Right away, in the turn's transaction: a rollback after this leaves
    # records of a test contact marked missing, never a file shown again.
    for key in (row[1] if len(row) > 1 and row[1] else []):
        media_store.delete(key)
    return True


_IDLE = ("c.test_mode IS NOT NULL "
         "AND GREATEST(c.last_inbound_at, c.test_started_at) < NOW() - make_interval(mins => %s)")


async def idle(conn, minutes: int) -> list[tuple[int, str]]:
    """Tests with no message from the person for `minutes`."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT c.id, c.chat_id FROM contacts c WHERE {_IDLE}", (minutes,))
        rows = await cur.fetchall()
    return [(row[0], row[1]) for row in rows]


async def still_idle(conn, contact_id: int, minutes: int) -> bool:
    """The same question for one contact, asked again under the chat's lock: a
    message that arrived meanwhile keeps the test alive."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT 1 FROM contacts c WHERE c.id = %s AND {_IDLE}",
                          (contact_id, minutes))
        return await cur.fetchone() is not None


# A test's appointment carries test_mode (set when it is booked; the title
# says [TESTE] too, but it is encrypted and cannot be matched in SQL). With the
# contact gone, the mark is what tells a test's leftover from a customer's.


async def orphan_appointments(conn) -> list[Appointment]:
    """Appointments of tests already wiped whose calendar event is still to be
    cancelled — left by a sweep that did not finish. Whatever their time: one
    past its start is still in the calendar."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_APPOINTMENT_COLUMNS} FROM appointments "
            "WHERE contact_id IS NULL AND test_mode ORDER BY starts_at",
        )
        rows = await cur.fetchall()
    return _appointments(rows)


async def forget_appointment(conn, appointment_id: int) -> None:
    """Drop a wiped test's appointment once its calendar event was handled.
    Scoped like the wipe: never a customer's row."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM appointments WHERE id = %s AND contact_id IS NULL AND test_mode",
            (appointment_id,),
        )
