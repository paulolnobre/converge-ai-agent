"""After the booking: the person answers the reminder, the team answers for the meeting.

The reminder carries three buttons — confirm, reschedule, cancel — read by code
like every other tap. Cancelling asks once more, because a button is easier to
hit by accident than a sentence is to type by accident.

Once the meeting's time has passed, the team group is asked whether it
happened. A no-show is recorded only from that answer: the bot cannot tell a
missed meeting from one held on a link it never saw, and marking a lead as a
no-show on a guess is worse than not marking it at all.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from core import content
from core.config import settings
from core.integrations import telegram
from core.integrations.workspace.models import TimeSlot
from core.models import Appointment, Contact
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.services import appointment_taps as taps
from core.services import funnel, scheduling, test_marks

logger = logging.getLogger(__name__)


def _team() -> str:
    return settings.team_chat_id.strip()


def _parts(appointment: Appointment) -> dict[str, str]:
    return scheduling.when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))


def reminder_keyboard(appointment: Appointment) -> dict:
    return telegram.inline_keyboard([[
        (content.text("scheduling.reminder_confirm"), taps.tap("ok", appointment)),
        (content.text("scheduling.reminder_reschedule"), taps.tap("move", appointment)),
        (content.text("scheduling.reminder_cancel"), taps.tap("cancel", appointment)),
    ]])


async def notify_team(appointment: Appointment, contact: Contact | None, key: str) -> None:
    """booked, rescheduled, confirmed or cancelled notice, when there is a team
    group to tell — marked TESTE when the contact is in a test."""
    if not _team() or test_marks.quiet(contact):
        return
    parts = _parts(appointment)
    await telegram.post_message(_team(), test_marks.team_text(content.render(
        f"attendance.{key}", nome=(contact.name if contact else None) or "O cliente",
        data=parts["data"], horario=parts["horario"],
    ), contact))


async def own_upcoming(conn, contact: Contact, appointment_id: int) -> Appointment | None:
    """The appointment a reminder button names — only if it is still this
    person's, still confirmed and still ahead. Anything else is a stale button."""
    appointment = await appointments_repo.get_by_id(conn, appointment_id)
    if (
        appointment is None
        or appointment.contact_id != contact.id
        or appointment.status != "confirmed"
        or appointment.starts_at <= datetime.now(UTC)
    ):
        return None
    return appointment


async def handle_tap(conn, contact: Contact, data: str) -> tuple[str, str, dict | None]:
    """A reminder button other than "Remarcar", which the handler owns because
    it continues into the offer flow. Returns (what was chosen, reply, keyboard)."""
    action, appointment_id, tapped_stamp = taps.parse(data)
    labels = {
        "ok": content.text("scheduling.reminder_confirm"),
        "cancel": content.text("scheduling.reminder_cancel"),
        "cancel_yes": content.text("scheduling.cancel_yes"),
        "keep": content.text("scheduling.cancel_no"),
    }
    chose = labels.get(action, data)
    appointment = await own_upcoming(conn, contact, appointment_id)
    # A button from before the meeting moved acts on nothing.
    if appointment is None or appointment.id is None or not taps.still_valid(appointment,
                                                                             tapped_stamp):
        return chose, content.text("scheduling.reminder_stale"), None
    parts = _parts(appointment)

    if action == "ok":
        await appointments_repo.confirm_attendance(conn, appointment.id)
        await notify_team(appointment, contact, "confirmed_notice")
        return chose, content.render("scheduling.attendance_confirmed", **parts), None

    if action == "cancel":
        keyboard = telegram.inline_keyboard([[
            (content.text("scheduling.cancel_yes"), taps.tap("cancel_yes", appointment)),
            (content.text("scheduling.cancel_no"), taps.tap("keep", appointment)),
        ]])
        return chose, content.render("scheduling.cancel_check", **parts), keyboard

    if action == "cancel_yes":
        # Under the contact's and the event's locks, read again: the calendar
        # sync may have moved it since the button was shown.
        await appointments_repo.lock_contact(conn, contact.id or 0)
        if appointment.provider_event_id:
            await appointments_repo.lock_event(conn, appointment.calendar_id,
                                               appointment.provider_event_id)
        appointment = await own_upcoming(conn, contact, appointment.id)
        if appointment is None or not taps.still_valid(appointment, tapped_stamp):
            return chose, content.text("scheduling.reminder_stale"), None
        parts = _parts(appointment)
        if not await scheduling.cancel(conn, appointment):
            return chose, content.text("scheduling.could_not_book"), None
        await funnel.step_back_after_cancel(conn, contact.id)
        await notify_team(appointment, contact, "cancelled_notice")
        return chose, content.render("scheduling.cancellation", **parts), None

    return chose, content.text("scheduling.kept"), None


async def ask_outcomes(conn) -> int:
    """Ask the team, once per meeting, whether each one that ended happened.
    Each question in its own transaction, so a contact's lock is held only
    while that question goes out."""
    if not _team():
        return 0
    asked = 0
    async with conn.transaction():
        due = await appointments_repo.due_outcomes(conn)
    for appointment in due:
        if appointment.id is None:
            continue
        async with conn.transaction():
            asked += await _ask_outcome(conn, appointment)
    return asked


async def _ask_outcome(conn, appointment: Appointment) -> int:
    contact = (await contacts_repo.get_by_id(conn, appointment.contact_id)
               if appointment.contact_id is not None else None)
    name = contact.name if contact else None
    if test_marks.quiet(contact):
        return 0  # a test's meeting is not asked about in the group
    # Claimed first, as reminders are (one missed question beats two), and
    # only for the time read: a meeting the calendar sync moved to the future
    # meanwhile is asked about after its new time.
    if appointment.contact_id is not None:
        await appointments_repo.lock_contact(conn, appointment.contact_id)
    if appointment.provider_event_id:
        await appointments_repo.lock_event(conn, appointment.calendar_id,
                                           appointment.provider_event_id)
    claimed = await appointments_repo.claim_outcome(conn, appointment.id or 0,
                                                    appointment.starts_at)
    if claimed is None:
        return 0
    parts = _parts(claimed)
    keyboard = telegram.inline_keyboard([[
        (content.text("attendance.outcome_done"), taps.outcome_tap("done", claimed)),
        (content.text("attendance.outcome_no_show"), taps.outcome_tap("no_show", claimed)),
    ]])
    sent = await telegram.post_message(_team(), test_marks.team_text(content.render(
        "attendance.outcome_question", nome=name or "o cliente",
        data=parts["data"], horario=parts["horario"],
    ), contact), reply_markup=keyboard)
    return 1 if sent is not None else 0


async def handle_team_tap(conn, callback: dict) -> bool:
    """Aconteceu / Não compareceu, tapped in the team group. The first answer
    is recorded; a second tap on the same card changes nothing."""
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    if not _team() or str(chat_id) != _team():
        return False
    try:
        outcome, appointment_id, tapped_stamp = taps.parse(callback.get("data") or "")
    except ValueError:
        return False
    if outcome not in ("done", "no_show"):
        return False
    who = (callback.get("from") or {}).get("first_name") or "Equipe"
    appointment = await appointments_repo.get_by_id(conn, appointment_id)
    # A question about a time the meeting no longer has records nothing.
    if appointment is None or appointment.id is None or not taps.still_valid(appointment,
                                                                             tapped_stamp):
        return False
    if not await appointments_repo.set_outcome(conn, appointment.id, outcome, who):
        return False
    contact = (await contacts_repo.get_by_id(conn, appointment.contact_id)
               if appointment.contact_id is not None else None)
    name = contact.name if contact else None
    if outcome == "no_show":
        await funnel.advance(conn, appointment.contact_id, "no_show")
    if test_marks.quiet(contact):
        return True
    await telegram.post_message(_team(), test_marks.team_text(content.render(
        f"attendance.recorded_{outcome}", nome=name or "o cliente", atendente=who,
    ), contact))
    return True
