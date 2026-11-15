"""The proposal PDF, sent from the panel to the person who asked for it.

Within WhatsApp's 24h window it goes as one message: the document, the text
and three buttons (accept, doubts, not interested). Past it, the approved
template with the document as its header and the same buttons. On Telegram,
the document and then the text with the buttons.

Sent once: the request is checked and marked sent first, then the channel is
called; a sure refusal undoes the mark, and an uncertain outcome keeps it, so
a second click never sends it again. The buttons' answers
are handled in core/handlers/message.py (`propdoc:*`).
"""
from __future__ import annotations

import asyncio
import logging

from core import content
from core.config import settings
from core.integrations import telegram, whatsapp
from core.models import Contact
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as repo
from core.services import inbox, panel_uploads, test_marks
from core.services.inbox import InboxError
from core.services.upload_types import Upload

logger = logging.getLogger(__name__)

PAYLOADS = ["propdoc:accept", "propdoc:doubt", "propdoc:decline"]
_SEND_SECONDS = 55


def _buttons() -> list[tuple[str, str]]:
    return [(content.text(f"proposal_doc.button_{key.split(':')[1]}"), key) for key in PAYLOADS]


def _names(contact: Contact, quem: str) -> tuple[str, str]:
    """{{1}} and {{2}}: the first name, and the company the request named."""
    parts = [p.strip() for p in quem.split(" · ")]
    company = (parts[1] if len(parts) >= 3 and parts[1] else contact.organization) or ""
    return (inbox.first_name(contact.name or parts[0] or ""),
            company.strip() or content.text("proposal_doc.no_company"))


def _check(upload: Upload) -> None:
    if upload.mime != "application/pdf" or not upload.data.startswith(b"%PDF-"):
        raise InboxError(422, "Envie a proposta em PDF.")


async def send(pool, user: dict, token_hash: str, briefing_id: int, upload: Upload) -> dict:
    """Marked sent before the channel is called, then undone only when the
    channel surely refused it: a lost answer or a failed write afterwards
    never makes a second click send it again."""
    _check(upload)
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"proposal-doc:{briefing_id}",))
        # After the upload and the lock: a session revoked meanwhile sends nothing.
        await panel_uploads.require_live_session(conn, user, token_hash)
        b = await repo.get(conn, briefing_id)
        if b is None or b.status not in ("sent", "incomplete_sent"):
            raise InboxError(409, "Este pedido já teve a proposta enviada ou não está aberto.")
        contact = await contacts_repo.get_by_id(conn, b.contact_id)
        if contact is None or contact.id is None or not contact.chat_id:
            raise InboxError(404, "Contato não encontrado.")
        # Like every other message from the panel: only whoever holds the
        # conversation writes to the customer. The window is checked below,
        # since past it the template goes.
        await inbox.lock_as_owner(conn, contact, user, past_window=True)
        route = await _route(conn, contact)
        if route in ("no_template", "channel"):
            raise InboxError(409, content.text(f"proposal_doc.{route}"))
        if not await repo.mark_proposal_sent(conn, briefing_id, user["user_id"]):
            # Marked by hand meanwhile ("Marcar como enviada"): nothing goes.
            raise InboxError(409, "Este pedido já teve a proposta enviada ou não está aberto.")
    nome, empresa = _names(contact, b.answers.get("quem", ""))
    text = content.render("proposal_doc.text", nome=nome, empresa=empresa)
    delivery, refusal, started = "uncertain", None, []
    try:
        async with pool.connection() as conn:
            # Authorized again and held through the send, as attachments are:
            # an owner who let go, or a session revoked, during the upload to
            # Meta sends nothing.
            try:
                await panel_uploads.require_live_session(conn, user, token_hash)
                session_id = await inbox.lock_as_owner(conn, contact, user, past_window=True)
                route = await _route(conn, contact)
                if route in ("no_template", "channel"):
                    raise InboxError(409, content.text(f"proposal_doc.{route}"))
            except InboxError as exc:
                refusal, delivery = exc, "failed"
            else:
                async with asyncio.timeout(_SEND_SECONDS):
                    delivery = await _deliver(contact, route, upload, text, [nome, empresa],
                                              started)
                if delivery == "ok":
                    await contacts_repo.record_message(
                        conn, contact.id, contact.chat_id, "assistant",
                        content.render("proposal_doc.history", arquivo=upload.filename,
                                       texto=text),
                        "proposal_doc", author_user_id=user["user_id"])
                    await funnel_repo.move_stage(conn, b.contact_id, "proposta",
                                                 ("novo", "respondido", "agendado"))
                    await handoffs_repo.touch(conn, session_id)
    except Exception as exc:
        if delivery == "ok":
            raise  # Sent; only the bookkeeping failed. It stays marked.
        # Before anything went to the customer (a slow upload to Meta, say),
        # it surely was not sent; after, it may have been.
        delivery = "uncertain" if started else "failed"
        logger.error("Proposal send interrupted (briefing %s, %s, %s)", briefing_id,
                     type(exc).__name__, delivery)
    if delivery == "uncertain":
        # It may have reached them: it stays marked sent, and a person checks.
        raise InboxError(502, content.text("proposal_doc.uncertain"))
    if delivery != "ok":
        # Not sent: the request waits again. Committed before the error.
        async with pool.connection() as conn:
            await repo.unmark_proposal_sent(conn, briefing_id, b.status)
        raise refusal or InboxError(502, content.text("proposal_doc.failed"))
    return {"sent": True, "notice": content.text("proposal_doc.sent")}


async def _route(conn, contact: Contact) -> str:
    """How it can go now: "telegram", "window" (free-form WhatsApp),
    "template", or why not ("no_template", "channel")."""
    if contact.channel == "telegram":
        return "telegram"
    if contact.channel != "whatsapp" or contact.id is None:
        return "channel"
    if await inbox_repo.window_open(conn, contact.id):
        return "window"
    return "template" if inbox.template_ready(settings.whatsapp_proposal_template) else "no_template"


async def _deliver(contact: Contact, route: str, upload: Upload, text: str,
                   params: list[str], started: list[bool]) -> str:
    """"ok", "failed" when the channel surely refused it, or "uncertain".
    `started` gets an entry right before the first call that can reach the
    customer."""
    chat_id = contact.chat_id or ""
    if route == "telegram":
        started.append(True)
        sent = await telegram.send_attachment(chat_id, upload.data, "document", upload.mime,
                                              upload.filename)
        if sent != "ok":
            return "uncertain" if sent == "uncertain" else "failed"
        keyboard = telegram.inline_keyboard([[(t, k)] for t, k in _buttons()])
        if not await telegram.send_message(chat_id, text, reply_markup=keyboard):
            # The document is there: it counts as sent; the buttons can be resent by hand.
            logger.warning("Proposal sent on Telegram without its buttons")
        return "ok"
    media_id = await whatsapp.upload_media(upload.data, upload.mime, upload.filename)
    if media_id is None:
        return "failed"
    to = chat_id.removeprefix("wa:")
    template = settings.whatsapp_proposal_template
    started.append(True)
    if route == "window":
        delivery = await whatsapp.send_document_buttons(to, media_id, upload.filename, text,
                                                        _buttons())
        if delivery != "window_closed" or not inbox.template_ready(template):
            return delivery if delivery in ("ok", "uncertain") else "failed"
    delivery = await whatsapp.send_document_template(
        to, template or "", inbox.TEMPLATE_LANGUAGE, media_id, upload.filename, params,
        PAYLOADS)
    return delivery if delivery in ("ok", "uncertain") else "failed"


async def decision(contact: Contact, data: str) -> tuple[str, str]:
    """"Aceitar proposta" or "Não tenho interesse": the label for the history,
    the reply, and the team told. The card stays where it is: closing or
    losing a deal is the team's call (core/services/funnel.py)."""
    accepted = data == "propdoc:accept"
    key = "accept" if accepted else "decline"
    notice = test_marks.team_text(content.render(f"proposal_doc.team_{key}",
                                                 nome=contact.name or "Sem nome"), contact)
    inbox.push_to_team(notice, contact)
    try:
        if not test_marks.quiet(contact):
            await inbox.notify_team(notice, contact.id)
    except Exception:
        logger.exception("The team was not told of a proposal answer; it is in the history")
    return content.text(f"proposal_doc.button_{key}"), content.text(f"proposal_doc.{key}_reply")
