"""Asking the person for more information to build their proposal, from the
panel: one message with two buttons, "Conversar por aqui" (stays with the
person who asked) and "Agendar conversa" (the booking): `propinfo:team` and
`propinfo:meet`.

Within WhatsApp's 24h window it is one message; past it, the approved template
(WHATSAPP_INFO_TEMPLATE). Like everything the panel writes to a customer, only
whoever holds the conversation sends it.
"""
from __future__ import annotations

import asyncio
import logging
import re

from core import channels, content
from core.config import settings
from core.integrations import telegram
from core.models import Contact
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as repo
from core.services import inbox, panel_uploads
from core.services.inbox import InboxError

logger = logging.getLogger(__name__)

# Their own taps, not the questionnaire's, naming the request they belong to:
# the person who sends it holds the conversation, and a tap on a held
# conversation goes to them. process_callback lets "Conversar 30 min" of a
# request still waiting through to the booking (`tap`).
PREFIX = "propinfo:"
_TAP = re.compile(r"propinfo:(meet|team):(\d{1,18})")


def payloads(briefing_id: int) -> list[str]:
    """In the buttons' order: "Conversar por aqui", then "Agendar conversa"."""
    return [f"{PREFIX}team:{briefing_id}", f"{PREFIX}meet:{briefing_id}"]


def tap(data: str) -> tuple[str, int] | None:
    """("meet" | "team", request id) of one of these buttons, or None."""
    found = _TAP.fullmatch(data)
    return (found.group(1), int(found.group(2))) if found else None


async def waiting(conn, contact: Contact, briefing_id: int) -> bool:
    """The tap is about this person's request, still waiting on a proposal."""
    b = await repo.get(conn, briefing_id)
    return b is not None and b.contact_id == contact.id and b.status in _OPEN
_SEND_SECONDS = 30
_OPEN = ("sent", "incomplete_sent")


def _keyboard(briefing_id: int) -> dict:
    labels = [content.text("proposal_info.button_team"), content.text("proposal_info.button_meet")]
    return telegram.inline_keyboard([[(label, data)] for label, data
                                     in zip(labels, payloads(briefing_id))])


async def _route(conn, contact: Contact) -> str:
    """"telegram", "window" (free-form WhatsApp), "template", or why not."""
    if contact.channel == "telegram":
        return "telegram"
    if contact.channel != "whatsapp" or contact.id is None:
        return "channel"
    if await inbox_repo.window_open(conn, contact.id):
        return "window"
    return "template" if inbox.template_ready(settings.whatsapp_info_template) else "no_template"


async def ask(pool, user: dict, token_hash: str, briefing_id: int) -> dict:
    """Send the request for more information. InboxError says why not."""
    async with pool.connection() as conn:
        # The same lock as sending the PDF: a request closed meanwhile is read
        # as closed, never asked about.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"proposal-doc:{briefing_id}",))
        await panel_uploads.require_live_session(conn, user, token_hash)
        b = await repo.get(conn, briefing_id)
        if b is None:
            raise InboxError(404, "Pedido não encontrado.")
        if b.status not in _OPEN:
            raise InboxError(409, "Este pedido já teve a proposta enviada ou não está aberto.")
        contact = await contacts_repo.get_by_id(conn, b.contact_id)
        if contact is None or contact.id is None or not contact.chat_id:
            raise InboxError(404, "Contato não encontrado.")
        session_id = await inbox.lock_as_owner(conn, contact, user, past_window=True)
        route = await _route(conn, contact)
        if route in ("no_template", "channel"):
            raise InboxError(409, content.text(f"proposal_info.{route}"))
        name = inbox.first_name(contact.name)
        text = content.render("proposal_info.text", nome=name)
        try:
            async with asyncio.timeout(_SEND_SECONDS):
                delivery = await _deliver(contact, route, text, name, briefing_id)
        except Exception as exc:
            logger.error("Request for information %s interrupted (%s)", briefing_id,
                         type(exc).__name__)
            delivery = "failed"
        if delivery != "ok":
            # A lost answer and a refusal look the same from here: it may have
            # reached them, so never "try again", which would ask twice.
            raise InboxError(502, content.text("proposal_info.failed"))
        try:
            await contacts_repo.record_message(
                conn, contact.id, contact.chat_id, "assistant",
                content.render("proposal_info.history", texto=text), "proposal_info",
                author_user_id=user["user_id"])
            await handoffs_repo.touch(conn, session_id)
        except Exception:
            # Sent; only the history failed. Saying "failed" would ask twice.
            logger.exception("Request for information %s sent, not recorded", briefing_id)
    return {"sent": True, "notice": content.text("proposal_info.sent")}


async def _deliver(contact: Contact, route: str, text: str, name: str,
                   briefing_id: int) -> str:
    chat_id = contact.chat_id or ""
    template = settings.whatsapp_info_template
    if route in ("telegram", "window"):
        delivery = await channels.deliver(chat_id, text, _keyboard(briefing_id), parse_mode=None)
        if delivery != "window_closed" or not inbox.template_ready(template):
            return "ok" if delivery == "ok" else "failed"
    delivery = await inbox.send_template(chat_id, template or "", [name], payloads(briefing_id))
    return "ok" if delivery == "ok" else "failed"
