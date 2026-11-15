"""Telling the team when a person's e-mail reaches the shared mailbox.

Read only and one attempt per e-mail: the point where the watcher stands is
saved before any alert goes out, so a failure or a restart never repeats one
(and may lose one, which the mailbox itself still holds).
"""
from __future__ import annotations

import asyncio
import logging

from core import content
from core.config import settings
from core.integrations.workspace import WorkspaceError
from core.integrations.workspace import spacemail_imap as imap
from core.repositories import contacts as contacts_repo
from core.repositories import mail_alerts as repo
from core.repositories.connection import connect_async, get_pool
from core.services import inbox, push, test_marks

logger = logging.getLogger(__name__)

MAX_ALERTS_PER_PASS = 10
_failing = False


def alert_text(mail: imap.IncomingMail) -> str:
    who = mail.sender_name or mail.sender_email or "remetente desconhecido"
    return content.render("inbox.email_alert", remetente=who,
                          assunto=mail.subject or "(sem assunto)",
                          previa=mail.preview).strip()


async def _alert(conn, mail: imap.IncomingMail) -> bool:
    text = alert_text(mail)
    contact = await contacts_repo.find_by_email(conn, mail.sender_email)
    if contact is not None and contact.blocked_at is not None:
        return False  # blocked in the panel: nobody is told
    if contact is not None and contact.id is not None:
        inbox.push_to_team(text, contact)
        if test_marks.quiet(contact):
            return False  # a test's e-mail is read in the Inbox, not announced in the group
        return await inbox.notify_team(text, contact.id)
    push.schedule_team(title=text, url="/painel/#inbox", tag="email")
    return await inbox.notify_team(text, None)


async def run_pass() -> int:
    """One look at the mailbox. Returns how many alerts the team group took."""
    if not settings.email_alerts_enabled:
        return 0
    mailbox = settings.spacemail_username
    # Under a lock (one replica at a time) and committed before any alert goes
    # out: a crash or a restart after this loses an alert, never repeats one.
    # A connection of its own, outside the pool: the IMAP read happens while
    # it is held, and the panel must not wait for it.
    conn = await connect_async()
    try:
        if not await repo.try_lock(conn, mailbox):
            return 0
        state = await repo.get_state(conn, mailbox)
        fetched = await asyncio.to_thread(imap.fetch_since, state)
        await repo.set_state(conn, mailbox, fetched.uidvalidity, fetched.max_uid)
        await conn.commit()
    finally:
        await conn.close()
    pool = await get_pool()
    people = [m for m in fetched.mails if not m.automated]
    sent = 0
    async with pool.connection() as conn:
        for mail in people[:MAX_ALERTS_PER_PASS]:
            try:
                sent += bool(await _alert(conn, mail))
            except Exception:
                logger.exception("A mail alert failed")
    extra = max(0, len(people) - MAX_ALERTS_PER_PASS) + fetched.skipped
    if extra > 0:
        await inbox.notify_team(content.render("inbox.email_alert_more", n=extra), None)
    return sent


async def run_mail_alert_worker(stop_event: asyncio.Event) -> None:
    global _failing
    interval = max(1, settings.email_alert_minutes) * 60
    while not stop_event.is_set():
        try:
            sent = await run_pass()
            if _failing:
                logger.info("The mailbox watcher is reading again")
                _failing = False
            if sent:
                logger.info("Mail alerts sent: %d", sent)
        except WorkspaceError as exc:
            # Once per streak: an outage must not fill the log every 5 minutes.
            if not _failing:
                logger.warning("The mailbox watcher could not read: %s", exc)
            _failing = True
        except Exception:
            logger.exception("The mailbox watcher failed")
        try:
            await asyncio.wait_for(stop_event.wait(), interval)
        except TimeoutError:
            pass
