"""Leads the team adds by hand: someone who left their number by e-mail, a
card handed over. A WhatsApp contact with its details and origin, and, since
the person never wrote, the approved first-contact template. When they answer,
the desk carries on from that message like from any other conversation.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from core import channels, content
from core.config import settings
from core.crypto import canonical_email
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.services import inbox
from core.services.inbox import InboxError

logger = logging.getLogger(__name__)


class LeadForm(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=30)
    email: str | None = Field(default=None, max_length=254)
    origin: str | None = "email"
    detail: str | None = Field(default=None, max_length=160)
    send: bool = True


def _chat_ids(raw: str) -> list[str]:
    """Every WhatsApp id this number may have, the typed form first. A + keeps
    the country code as typed; without one, the number is Brazilian, with or
    without the 55."""
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        ids = channels.whatsapp_ids("+" + digits)
    elif len(digits) in (10, 11):
        ids = channels.whatsapp_ids("+55" + digits)
    elif len(digits) in (12, 13) and digits.startswith("55"):
        ids = channels.whatsapp_ids("+" + digits)
    else:
        ids = []
    if not ids:
        raise InboxError(422, "Telefone inválido. Use DDD e número, ex.: (91) 99999-0000.")
    return ids


async def add(conn, user: dict, form: LeadForm) -> dict:
    """The contact and, when asked, the first message. A number that already
    has a WhatsApp conversation, in either form, opens it untouched."""
    name = form.name.strip()
    if not name:
        raise InboxError(422, "Escreva o nome.")
    if form.origin is not None and form.origin not in inbox.ORIGINS:
        raise InboxError(422, "Origem inválida.")
    email = canonical_email(form.email) if form.email and form.email.strip() else None
    if form.email and form.email.strip() and (email is None or "@" not in email):
        raise InboxError(422, "E-mail inválido.")
    chat_ids = _chat_ids(form.phone)

    # Two people adding the same number at once: one sends, the other finds it.
    # Held until the request commits; the same lock a reply from either form takes.
    for chat_id in sorted(chat_ids):
        await handoffs_repo.lock_chat(conn, chat_id)
    existing = await contacts_repo.find_by_chat_ids(conn, chat_ids)
    if existing is not None and existing.id is not None:
        return {"contact_id": existing.id, "existing": True, "sent": False,
                "notice": content.text("lead.exists")}

    # Sent before the contact exists: Meta answers with the id the person's
    # replies will come from, and that is the chat the contact is kept under.
    chat_id, first = chat_ids[0], inbox.first_name(name)
    sent, notice, said = False, content.text("lead.added"), None
    if form.send:
        template, said = _pick_template(form.origin)
        sent, notice, meta_id = await _first_contact(chat_id, first, template)
        chat_id = meta_id or chat_id

    contact = await contacts_repo.get_or_create(conn, chat_id, name)
    assert contact.id is not None
    await contacts_repo.update_details(conn, contact.id,
                                       phone=chat_ids[0].removeprefix(channels.WA_PREFIX),
                                       **({"email": email} if email else {}))
    detail = (form.detail or "").strip() or None
    await inbox_repo.set_origin(conn, contact.id, form.origin, detail, user["user_id"])
    if sent and said:
        await contacts_repo.record_message(
            conn, contact.id, chat_id, "assistant",
            content.render(said, nome=first), "lead",
            author_user_id=user["user_id"])
    return {"contact_id": contact.id, "existing": False, "sent": sent, "notice": notice}


def _pick_template(origin: str | None) -> tuple[str | None, str | None]:
    """The approved template for this lead and the history text that mirrors it.
    Someone who left their contact by e-mail gets the e-mail one; anyone else,
    or an e-mail lead while that one is not approved, the general one."""
    if origin == "email" and inbox.template_ready(settings.whatsapp_lead_template):
        return settings.whatsapp_lead_template, "lead.first_contact_text"
    if inbox.template_ready(settings.whatsapp_lead_general_template):
        return settings.whatsapp_lead_general_template, "lead.first_contact_general_text"
    return None, None


async def _first_contact(chat_id: str, first: str,
                         template: str | None) -> tuple[bool, str, str | None]:
    if not template:
        return False, content.text("lead.no_template"), None
    delivery, meta_id = await inbox.send_first_template(chat_id, template, [first],
                                                        ["lead:yes", "lead:no"])
    if delivery != "ok":
        logger.warning("Lead first-contact template refused (%s)", delivery)
        return False, content.text("lead.send_failed"), None
    return True, content.text("lead.sent"), meta_id
