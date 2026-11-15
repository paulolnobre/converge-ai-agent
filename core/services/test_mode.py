"""/teste: someone in the team is served as a customer, on the real path —
the bot, the calendar, the Inbox, the team group — with everything marked
TESTE and wiped at the end (/sair, or TEST_IDLE_MINUTES without a message).

The commands are read from the message's own text before anything else in
the turn (the rate limit, the opt-out, triage): a voice note is never a
command, and "SAIR" without the slash stays the opt-out.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from core import channels, content
from core.config import settings
from core.integrations import telegram, workspace
from core.integrations.workspace.models import TimeSlot
from core.models import Appointment, Contact, StaffMember
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import test_mode as repo
from core.services import scheduling, test_marks

logger = logging.getLogger(__name__)

Command = Literal["novo", "antigo", "sair"]


def parse_command(text: str | None) -> Command | None:
    """"/teste", "/teste antigo" or "/sair", however typed; anything else is
    an ordinary message. Telegram may add the bot's name: "/teste@Bot"."""
    words = (text or "").strip().lower().split()
    if not words:
        return None
    head = words[0].split("@")[0]
    if head == "/sair" and len(words) == 1:
        return "sair"
    if head == "/teste":
        if len(words) == 1:
            return "novo"
        if words[1:] == ["antigo"]:
            return "antigo"
    return None


def _when(appointment) -> str:
    parts = scheduling.when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))
    return f"{parts['data']} às {parts['horario']}"


async def cleanup(conn, contact: Contact, *, staff_own: bool) -> tuple[bool, list[str]]:
    """Cancel the contact's future events in the calendar, then wipe the
    contact. A calendar that refuses one event does not stop the wipe; the
    event comes back as a line asking for it to be removed by hand. Returns
    whether the contact was deleted, and those lines."""
    failures: list[str] = []
    if contact.id is None:
        return False, failures
    for appointment in await appointments_repo.upcoming_for_contact(conn, contact.id):
        try:
            cancelled = await scheduling.cancel(conn, appointment)
        except Exception:
            # Not only WorkspaceError: a timeout renewing the Google token is
            # an httpx error, and it must not leave the test's data behind.
            logger.exception("Cancelling a test event failed")
            cancelled = False
        if not cancelled:
            line = content.render("teste.cancel_failed", quando=_when(appointment))
            failures.append(line)
            await _report_leftover(appointment, contact, line)
    deleted = await repo.delete(conn, contact.id, staff_own=staff_own)
    return deleted, failures


async def _report_leftover(appointment, contact: Contact, line: str) -> None:
    """An event the wipe could not cancel survives the wipe: say so where it
    cannot be lost with the chat message — the log and the team group."""
    logger.error("Test event left in the calendar: event %s in %s at %s",
                 appointment.provider_event_id, appointment.calendar_id,
                 appointment.starts_at.isoformat())
    team = settings.team_chat_id.strip()
    if team:
        try:
            await telegram.post_message(team, test_marks.team_text(line, contact))
        except Exception:
            logger.exception("Could not tell the team about a leftover test event")


async def _tell(chat_id, text: str) -> None:
    """channels.send says it failed by returning False; either way it is logged."""
    try:
        delivered = await channels.send(chat_id, text)
    except Exception:
        logger.exception("A test-mode message was not delivered")
        return
    if not delivered:
        logger.error("A test-mode message was not delivered")


def _reply(key: str, failures: list[str]) -> str:
    return "\n\n".join([content.text(key), *failures])


def _phone(chat_id: str) -> str | None:
    """The number of the WhatsApp chat being tested from; Telegram has none."""
    return "+" + chat_id.removeprefix("wa:") if channels.is_whatsapp(chat_id) else None


async def _start(conn, chat_id, staff: StaffMember, mode: Command,
                 inbound_at: datetime) -> str:
    current = await contacts_repo.get_or_create(conn, chat_id)
    _, failures = await cleanup(conn, current, staff_own=True)
    returning = mode == "antigo"
    contact = await repo.create(
        conn, str(chat_id), mode,
        name=staff.name if returning else None,
        email=staff.email if returning else None,
        phone=_phone(str(chat_id)) if returning else None,
        inbound_at=inbound_at,
    )
    if returning and contact.id is not None:
        await repo.seed_yesterday(conn, contact.id, str(chat_id), "oi",
                                  content.text("reply.welcome"))
    await _tell(chat_id, _reply(
        "teste.started_returning" if returning else "teste.started_new", failures))
    logger.info("A team member started a test (%s)", mode)
    return "test_started"


async def handle_command(conn, chat_id, text: str | None, inbound_at: datetime) -> str | None:
    """Handle /teste, /teste antigo or /sair and answer in the chat. None when
    the message should go on as usual: not a command, a /teste from someone
    outside the team, or a /sair from someone neither in the team nor in a test."""
    command = parse_command(text)
    if command is None:
        return None
    if command in ("novo", "antigo"):
        staff = await contacts_repo.find_staff_by_chat(conn, chat_id)
        if staff is None:
            return None
        return await _start(conn, chat_id, staff, command, inbound_at)

    contact = await contacts_repo.get_or_create(conn, chat_id)
    if contact.test_mode:
        _, failures = await cleanup(conn, contact, staff_own=False)
        await _tell(chat_id, _reply("teste.ended", failures))
        logger.info("A test was ended with /sair")
        return "test_ended"
    if await contacts_repo.find_staff_by_chat(conn, chat_id) is None:
        return None
    await _tell(chat_id, content.text("teste.not_in_test"))
    return "not_in_test"


async def _decide_expiry(conn, contact_id: int, chat_id: str
                        ) -> tuple[Contact, list[Appointment]] | None:
    """Phase 1, short and under the chat's lock: wipe the test if it is still
    quiet, keeping the list of its future events. No outside call here, so no
    lock is held while Google answers."""
    await handoffs_repo.lock_chat(conn, chat_id)
    if not await repo.still_idle(conn, contact_id, settings.test_idle_minutes):
        return None
    contact = await contacts_repo.get_by_id(conn, contact_id)
    if contact is None or contact.id is None or not contact.test_mode:
        return None
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id)
    if not await repo.delete(conn, contact.id, staff_own=False,
                             keep_ids=[a.id for a in upcoming if a.id is not None],
                             idle_minutes=settings.test_idle_minutes):
        # The tester wrote meanwhile: the test goes on, untouched.
        return None
    logger.info("Ending an idle test; calendar events to cancel: %s",
                [a.provider_event_id for a in upcoming])
    return contact, upcoming


async def _cancel_in_calendar(appointment: Appointment) -> bool:
    """Cancel one event straight in the calendar. Its row is already gone with
    the test, so this is not scheduling.cancel; it never raises."""
    if not appointment.provider_event_id:
        return True
    try:
        await workspace.calendar().cancel_event(appointment.calendar_id,
                                                appointment.provider_event_id)
        return True
    except Exception:
        logger.exception("Cancelling an expired test's event failed")
        return False


# A wiped test's leftover appointment has no contact any more; this stands in
# for it so the team's notice still says TESTE.
_A_TEST = Contact(test_mode="novo")


async def _clear_appointment(pool, appointment: Appointment, contact: Contact) -> str | None:
    """Cancel one wiped test's event in the calendar, then drop its row — in
    that order, so a crash in between leaves the row for the next sweep. A
    refusal is reported and the row dropped anyway: the line asks a person."""
    line = None
    if not await _cancel_in_calendar(appointment):
        line = content.render("teste.cancel_failed", quando=_when(appointment))
        await _report_leftover(appointment, contact, line)
    if appointment.id is not None:
        async with pool.connection() as conn:
            await repo.forget_appointment(conn, appointment.id)
    return line


async def _finish_expiry(pool, chat_id: str, contact: Contact,
                         upcoming: list[Appointment]) -> None:
    """Phase 2, after the wipe is committed: clean the calendar and say so."""
    failures = [line for appointment in upcoming
                if (line := await _clear_appointment(pool, appointment, contact))]
    # The window is open: the last message is a little over 2h old.
    await _tell(chat_id, _reply("teste.expired", failures))


async def _finish_interrupted(pool) -> None:
    """What a sweep killed halfway left behind: cancelled and dropped now. The
    tester's chat went with the test; a refusal reaches the team only."""
    async with pool.connection() as conn:
        leftovers = await repo.orphan_appointments(conn)
    for appointment in leftovers:
        await _clear_appointment(pool, appointment, _A_TEST)


async def sweep_in_pool(pool) -> int:
    """End the tests that went quiet. Each is decided and wiped in its own
    short transaction; its calendar is cleaned after that commits."""
    try:
        await _finish_interrupted(pool)
    except Exception:
        logger.exception("Could not finish an interrupted test expiry")
    async with pool.connection() as conn:
        quiet = await repo.idle(conn, settings.test_idle_minutes)
    ended = 0
    for contact_id, chat_id in quiet:
        # One test that cannot be ended must not keep the others alive.
        try:
            async with pool.connection() as conn:
                decided = await _decide_expiry(conn, contact_id, chat_id)
            if decided is not None:
                await _finish_expiry(pool, chat_id, *decided)
                ended += 1
        except Exception:
            logger.exception("Could not end an idle test")
    return ended
