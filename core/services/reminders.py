"""Reminding people about appointments, exactly once.

Phase 2. The "exactly once" is the whole design: `reminder_sent_at` is claimed
in the database before the message goes out, so a replica that restarts halfway
through a sweep does not remind everyone a second time. A duplicate reminder
reads as a second appointment.
"""
from __future__ import annotations

import asyncio
import logging

from core import channels, content
from core.config import settings
from core.integrations import telegram, whatsapp, workspace
from core.integrations.workspace.models import OutgoingMail
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories.connection import get_pool
from core.services import appointment_taps as taps
from core.services import attendance, automations

logger = logging.getLogger(__name__)

_SWEEP_SECONDS = 300


def compose(appointment, contact_name: str | None = None) -> str:
    """The reminder, in the organization's words.

    The patient's own name is used when the desk has it: the organization's
    template asks for it, and a reminder that opens with "Olá!" reads like a
    broadcast rather than a message about their appointment.
    """
    from core.integrations.workspace.models import TimeSlot
    from core.services.scheduling import when_parts, with_meeting_link

    parts = when_parts(
        TimeSlot(start=appointment.starts_at, end=appointment.ends_at)
    )
    return with_meeting_link(content.render(
        "scheduling.reminder",
        nome_paciente=contact_name or "tudo bem",
        data=parts["data"],
        dia_semana=parts["dia_semana"],
        horario=parts["horario"],
    ), appointment)


async def _send_whatsapp(chat_id: str, appointment, contact_name: str | None) -> bool:
    """WhatsApp takes a message the company starts only as an approved
    template: the same words, with the same three buttons carrying the same
    taps as on Telegram."""
    template = settings.whatsapp_reminder_template
    if not template or not settings.whatsapp_configured():
        logger.warning("WhatsApp reminder for appointment %s not sent: "
                       "WHATSAPP_REMINDER_TEMPLATE or WhatsApp itself is not set", appointment.id)
        return False
    from core.integrations.workspace.models import TimeSlot
    from core.services.scheduling import when_parts

    parts = when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))
    delivery = await whatsapp.send_template(
        chat_id.removeprefix(channels.WA_PREFIX), template, "pt_BR",
        [contact_name or "tudo bem", f"{parts['dia_semana']}, {parts['data']}", parts["horario"],
         appointment.meeting_url or content.text("whatsapp.no_meet_link")],
        [taps.tap("ok", appointment), taps.tap("move", appointment),
         taps.tap("cancel", appointment)],
    )
    return delivery == "ok"


async def _send_email(contact, appointment) -> str | None:
    """The reminder by e-mail, from the shared mailbox, when the channel it was
    meant for did not take it. Returns the text sent, None if nothing went out:
    no e-mail on file, no mailbox, or the mailbox refused."""
    mailbox = workspace.sender_mailbox()
    if not mailbox or not contact.email:
        return None
    from core.integrations.workspace.models import TimeSlot
    from core.services.scheduling import when_parts

    parts = when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at))
    values = {"nome_paciente": contact.name or "tudo bem", **parts}
    link = (f"\n{content.render('scheduling.meeting_link', link=appointment.meeting_url)}\n"
            if appointment.meeting_url else "")
    body = content.render("scheduling.reminder_email", reuniao=link, **values).strip()
    try:
        await workspace.mail_sender().send_message(mailbox, OutgoingMail(
            to=[contact.email],
            subject=content.render("scheduling.reminder_email_subject", **values),
            body_text=body,
        ))
    except workspace.WorkspaceError as exc:
        # The address is not logged: only what failed.
        logger.warning("Reminder e-mail for appointment %s not sent: %s (status %s)",
                       appointment.id, type(exc).__name__, exc.status)
        return None
    return body


async def _remind(conn, appointment) -> int:
    """One reminder. Returns 1 when it went out."""
    if appointment.contact_id is None or appointment.id is None:
        return 0
    contact = await contacts_repo.get_by_id(conn, appointment.contact_id)
    if contact is None or not contact.chat_id:
        return 0
    # Under the contact's and the event's locks, claimed only for the time read:
    # a meeting moved meanwhile (in Google, by the calendar sync) gets its
    # reminder at the new time instead. Claimed before sending: if the send
    # fails, one person misses one reminder, rather than everyone getting two.
    # The chat's lock first, as a bot turn and the panel take it: a block
    # being saved right now is seen, and none lands until this one is out.
    await handoffs_repo.lock_chat(conn, contact.chat_id)
    if await contacts_repo.is_blocked(conn, contact.chat_id):
        return 0
    await appointments_repo.lock_contact(conn, appointment.contact_id)
    if appointment.provider_event_id:
        await appointments_repo.lock_event(conn, appointment.calendar_id,
                                           appointment.provider_event_id)
    claimed = await appointments_repo.claim_reminder(conn, appointment.id, appointment.starts_at)
    if claimed is None:
        return 0
    appointment = claimed
    text = compose(appointment, contact.name)
    if channels.is_whatsapp(contact.chat_id):
        sent = await _send_whatsapp(contact.chat_id, appointment, contact.name)
    else:
        sent = bool(await telegram.send_message(
            contact.chat_id, text, reply_markup=attendance.reminder_keyboard(appointment)
        ))
    if not sent:
        # The channel did not take it (or WhatsApp has no template yet): the
        # e-mail on file is the next way to reach them, once.
        emailed = await _send_email(contact, appointment)
        if emailed is None:
            return 0
        text = emailed
    # In the history, so a typed answer to it is read with its context.
    await contacts_repo.record_message(
        conn, contact.id, contact.chat_id, "assistant", text, "reminder"
    )
    return 1


async def send_due(conn) -> int:
    """Send every reminder now due. Returns how many went out."""
    due = await appointments_repo.due_reminders(
        conn, settings.reminder_hours_before, settings.late_reminder_minutes
    )
    # The SELECT starts an implicit transaction even with no reminders due.
    # End it here so ask_outcomes starts real transactions for each question,
    # releasing locks and committing claims before the rest of the sweep.
    await conn.commit()
    sent = 0
    for appointment in due:
        # Each reminder is its own transaction, committed as it goes out. One
        # that fails — the contact of a /teste wiped meanwhile, say — undoes
        # only itself; no lock waits for the rest of the batch; and a crash
        # mid-batch never sends the ones already sent again.
        try:
            sent += await _remind(conn, appointment)
            await conn.commit()
        except Exception:
            await conn.rollback()
            logger.exception("A reminder failed; the rest of the batch goes on")
    return sent


async def run_reminder_worker(stop_event: asyncio.Event) -> None:
    pool = await get_pool()
    while not stop_event.is_set():
        try:
            async with pool.connection() as conn:
                sent = await send_due(conn)
                asked = await attendance.ask_outcomes(conn)
                ran = await automations.run(conn, dispatch=False)
            ran["sent"] = await automations.dispatch_approved(pool)
            if sent:
                logger.info("Sent %d appointment reminders", sent)
            if asked:
                logger.info("Asked the team about %d finished appointments", asked)
            if any(ran.values()):
                logger.info("Automations: %s", ran)
        except Exception:
            logger.exception("Reminder sweep failed")
        await asyncio.sleep(_SWEEP_SECONDS)
