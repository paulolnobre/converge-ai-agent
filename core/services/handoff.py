"""Handing a conversation to a person, and who owns it once it is handed off.

The desk says "someone from the team will continue here" — this file is what
makes that true. A handoff opens a session and tells the team group in one
line, with a button to the conversation in /painel (core/services/inbox.py).
Someone takes it there and answers there. Until someone does, the bot keeps
answering — nobody is left without a reply overnight. While a person holds
it, the bot stays quiet: the customer's messages are only recorded for the
panel, and the person holding it gets a short private notice.

    /bot   the customer's own way to leave a session early, from their side

A session with no message on either side for HANDOFF_TIMEOUT_MINUTES closes
itself — see sweep_timeouts(), run on a timer in core/agent.py — and both
sides are told, never left to wonder why the other stopped answering.

None of this is read by a model: a tap, a command or a click already says
exactly what it means.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from core import channels, content
from core.config import settings
from core.integrations import telegram
from core.models import Contact
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories.connection import get_pool
from core.services import inbox, test_marks
from core.services.media_types import Kind

logger = logging.getLogger(__name__)

_SWEEP_SECONDS = 300

STAGE_LABELS = {
    "novo": "Novo lead", "respondido": "Bot respondeu", "agendado": "Conversa agendada",
    "proposta": "Proposta enviada", "fechado": "Fechado", "perdido": "Perdido",
}


def configured() -> bool:
    return bool(settings.team_chat_id.strip())


def _team() -> str:
    return settings.team_chat_id.strip()


async def start(conn, contact: Contact, chat_id, reason: str) -> bool:
    """Open a session and tell the team — or nothing new if a person already
    holds it, since the customer only ever has one live request to the team.
    Asked again while it waits, the team is told again."""
    if not configured():
        logger.warning("Handoff requested but TEAM_CHAT_ID is not set.")
        return False
    session = await handoffs_repo.open_session(
        conn, contact_id=contact.id, chat_id=chat_id, team_chat_id=_team(), reason=reason,
    )
    if session.id is None:
        return False
    if session.status != "open":
        return True  # a person already holds it
    motivo = content.text(f"inbox.reason_{reason}") if reason in ("pedido", "botao") else reason
    text = test_marks.team_text(content.render(
        "inbox.needs_person", nome=contact.name or "Sem nome",
        canal=inbox.channel_label(contact.channel), motivo=motivo,
    ), contact)
    inbox.push_to_team(text, contact)
    if test_marks.quiet(contact):
        return True  # the person is told the team was called; a test has no group to tell
    return await inbox.notify_team(text, contact.id)


async def open_for_media(conn, contact: Contact, chat_id,
                         kind: Kind) -> Literal["opened", "existing"]:
    """A photo or file with no text: a request in "Esperando pessoa", opened
    once. Called under the turn's chat lock. Already open or held: its
    activity is touched and nobody is told again — an album of six photos is
    one request, one notice. The notice comes after the request exists, and
    a notice that fails leaves the request where the team will see it."""
    session = await handoffs_repo.active_session(conn, chat_id)
    if session is not None and session.id is not None:
        await handoffs_repo.touch(conn, session.id)
        return "existing"
    await handoffs_repo.open_session(
        conn, contact_id=contact.id, chat_id=chat_id, team_chat_id=_team(), reason="midia")
    motivo = content.render("inbox.reason_media", tipo=content.text(f"media.kind_{kind}"))
    text = test_marks.team_text(content.render(
        "inbox.needs_person", nome=contact.name or "Sem nome",
        canal=inbox.channel_label(contact.channel), motivo=motivo,
    ), contact)
    inbox.push_to_team(text, contact)
    try:
        if not test_marks.quiet(contact):
            await inbox.notify_team(text, contact.id)
    except Exception:
        logger.exception("The team was not told of a media request; it is in the Inbox")
    return "opened"


async def held_media(conn, contact: Contact, chat_id, *, notify: bool) -> bool:
    """Whether a person holds the conversation. Their activity is touched
    and, with `notify`, they hear that the customer sent something; nothing
    is recorded here — the capture already wrote the media's line."""
    session = await handoffs_repo.active_session(conn, chat_id)
    if session is None or session.id is None or session.status != "claimed":
        return False
    await handoffs_repo.touch(conn, session.id)
    if notify:
        await inbox.ping_owner(conn, session, contact)
    await inbox.deliver_after_reopen(conn, contact, chat_id)
    return True


async def handle_customer_message(conn, contact: Contact, chat_id, body: str
                                  ) -> Literal["claimed", "waiting"] | None:
    """Where a message goes when the team was called in.

    "claimed": a person holds the conversation. The message is recorded for
    the panel and that person hears, privately, that the customer wrote; the
    caller must not run triage on it. "waiting": nobody has taken it yet, and
    the bot answers as usual (the caller records the message). None: no one
    was called in.
    """
    session = await handoffs_repo.active_session(conn, chat_id)
    if session is None or session.id is None:
        return None

    if body.strip().lower() == "/bot":
        await handoffs_repo.close(conn, session.id)
        text = content.text("handoff.back_to_bot")
        await channels.send(chat_id, text)
        if configured() and not test_marks.quiet(contact):
            await telegram.post_message(
                _team(), test_marks.team_text(
                    content.render("handoff.closed_notice", nome=contact.name or "o cliente"),
                    contact)
            )
        if contact.id is not None:
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "handoff")
        return "claimed"

    await handoffs_repo.touch(conn, session.id)
    if session.status != "claimed":
        return "waiting"
    if contact.id is not None:
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "handoff")
    await inbox.ping_owner(conn, session, contact)
    await inbox.deliver_after_reopen(conn, contact, chat_id)
    return "claimed"


async def _close_stale(conn, item: handoffs_repo.StaleSession) -> bool:
    session = item.session
    if session.id is None:
        return False
    await handoffs_repo.lock_chat(conn, session.chat_id)
    if not await handoffs_repo.close_if_idle(conn, session.id, settings.handoff_timeout_minutes):
        return False
    who = item.contact_name or "o cliente"
    text = content.text("handoff.timeout_customer")
    # Past WhatsApp's 24h window Meta would refuse the notice: the team still
    # hears of the close, the customer does not, and nothing unsent is recorded.
    delivered = False
    if await _window_open(conn, session):
        delivered = await channels.send(session.chat_id, text)
    if session.claimed_by_name:
        notice = content.render(
            "handoff.timeout_claimed", nome=who, atendente=session.claimed_by_name
        )
    else:
        notice = content.render("handoff.timeout_open", nome=who)
    contact = (await contacts_repo.get_by_id(conn, session.contact_id)
               if session.contact_id is not None else None)
    notice = test_marks.team_text(notice, contact)
    if configured() and not test_marks.quiet(contact):
        await telegram.post_message(_team(), notice)
    if delivered and session.contact_id is not None:
        await contacts_repo.record_message(
            conn, session.contact_id, session.chat_id, "assistant", text, "handoff"
        )
    return True


async def _window_open(conn, session) -> bool:
    if not channels.is_whatsapp(session.chat_id) or session.contact_id is None:
        return True
    return await inbox_repo.window_open(conn, session.contact_id)


async def sweep_timeouts(conn) -> int:
    """Close sessions that remain inactive when the chat lock is held."""
    stale = await handoffs_repo.stale_sessions(conn, settings.handoff_timeout_minutes)
    closed = 0
    for item in stale:
        closed += await _close_stale(conn, item)
    return closed


async def sweep_timeouts_in_pool(pool) -> int:
    """Commit each closure separately so its chat lock ends after its notice."""
    async with pool.connection() as conn:
        stale = await handoffs_repo.stale_sessions(conn, settings.handoff_timeout_minutes)
    closed = 0
    for item in stale:
        async with pool.connection() as conn:
            closed += await _close_stale(conn, item)
    return closed


async def run_handoff_timeout_worker(stop_event: asyncio.Event) -> None:
    pool = await get_pool()
    while not stop_event.is_set():
        try:
            closed = await sweep_timeouts_in_pool(pool)
            if closed:
                logger.info("Closed %d handoff session(s) for inactivity", closed)
        except Exception:
            logger.exception("Handoff timeout sweep failed")
        try:
            # Imported here: test_mode reaches handoff's neighbours, not handoff.
            from core.services import test_mode

            ended = await test_mode.sweep_in_pool(pool)
            if ended:
                logger.info("Ended %d test(s) for inactivity", ended)
        except Exception:
            logger.exception("Test mode sweep failed")
        await asyncio.sleep(_SWEEP_SECONDS)
