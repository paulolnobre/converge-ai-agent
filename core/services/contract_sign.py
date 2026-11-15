"""The contract sent to be signed on Autentique, from the panel ("Enviar para
assinatura" on the request's card).

The contract is the one "Conferir contrato" shows (contract_pdf). It goes to
Autentique with two signers: who signs for the customer, from the Flow
`dados_contrato`, and the Converge's representative (CONTRACT_SIGNER_*).
Autentique e-mails both their link; the customer's also goes in the
conversation when it can (WhatsApp's 24h window open). Like everything the
panel writes to a customer, only whoever holds the conversation sends it.

Sandbox while AUTENTIQUE_SANDBOX is on, and always for a contact in /teste.
Signed (the webhook) comes next: spec venda-ate-pagamento, increment 4, part 3B.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from core import content
from core.config import settings
from core.integrations import autentique
from core.models import Contact
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as details_repo
from core.repositories import contract_signatures as signatures_repo
from core.repositories import proposal_briefings as repo
from core.services import contract_pdf, inbox, panel_uploads, proposal_pdf
from core.services.inbox import InboxError

logger = logging.getLogger(__name__)


def ready() -> bool:
    """The button shows only with Autentique and the Converge's signer set."""
    return (autentique.configured() and bool(settings.contract_signer_name.strip())
            and "@" in settings.contract_signer_email)


def _t(key: str) -> str:
    return content.text(f"contract_sign.{key}")


async def send(pool, user: dict, token_hash: str, briefing_id: int) -> dict:
    if not ready():
        raise InboxError(409, _t("off"))
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"contract-sign:{briefing_id}",))
        await panel_uploads.require_live_session(conn, user, token_hash)
        b = await repo.get(conn, briefing_id)
        if b is None:
            raise InboxError(404, "Pedido não encontrado.")
        contact = await contacts_repo.get_by_id(conn, b.contact_id)
        if contact is None or contact.id is None or not contact.chat_id:
            raise InboxError(404, "Contato não encontrado.")
        await inbox.lock_as_owner(conn, contact, user, past_window=True)
        if await signatures_repo.get(conn, briefing_id) is not None:
            raise InboxError(409, _t("already"))
        try:
            pdf, filename, terms = await contract_pdf.generate_with_terms(conn, briefing_id)
        except proposal_pdf.NoQuote:
            raise InboxError(409, content.text("proposal_pdf.sem_orcamento")) from None
        except contract_pdf.NoDetails:
            raise InboxError(409, content.text("contract_pdf.sem_dados")) from None
        details = await details_repo.get(conn, briefing_id) or {}
        sandbox = settings.autentique_sandbox or bool(contact.test_mode)
        attempt = await signatures_repo.claim(conn, briefing_id, user["user_id"],
                                              sandbox=sandbox, terms=terms)
        if attempt is None:
            raise InboxError(409, _t("already"))
    # Committed: a second click meanwhile finds it "sending" and sends nothing.
    email = details.get("assinante_email", "").lower()
    signers = [autentique.Signer(details.get("assinante_nome", ""), email,
                                 details.get("assinante_cpf", "")),
               autentique.Signer(settings.contract_signer_name.strip(),
                                 settings.contract_signer_email.strip().lower())]
    try:
        # A whole bound, well under STALE_MINUTES: an attempt the team can
        # free has surely ended.
        async with asyncio.timeout(_CREATE_SECONDS):
            created = await autentique.create_document(
                filename.removesuffix(".pdf"), pdf, filename, signers,
                message=_t("email_message"), sandbox=sandbox)
    except TimeoutError:
        logger.error("Contract of request %s: Autentique took too long", briefing_id)
        raise InboxError(502, _t("uncertain")) from None
    except autentique.AutentiqueError as exc:
        logger.error("Contract of request %s not sent to Autentique: %s", briefing_id, exc)
        if exc.uncertain:
            # It may be there, e-mailed already: it stays "sending" until the
            # team checks Autentique and frees it (`release`).
            raise InboxError(502, _t("uncertain")) from None
        async with pool.connection() as conn:
            await signatures_repo.release(conn, attempt)
        raise InboxError(502, _t("failed")) from None
    customer = created.signatures.get(email)
    async with pool.connection() as conn:
        kept = await signatures_repo.mark_sent(
            conn, attempt, created.document_id, customer,
            created.signatures.get(settings.contract_signer_email.strip().lower()))
    if not kept:
        # Freed meanwhile and maybe sent again: this document is a second one.
        logger.error("Contract of request %s created on Autentique (%s) after its send was "
                     "freed: cancel one of them there", briefing_id, created.document_id)
        raise InboxError(409, _t("duplicate"))
    linked = customer is not None and await _link_in_conversation(
        pool, user, contact, customer, details.get("assinante_nome", ""), email)
    return {"sent": True, "sandbox": sandbox, "notice": _t("sent" if linked else "sent_email")}


async def _link_in_conversation(pool, user: dict, contact: Contact, signature: str,
                                name: str, email: str) -> bool:
    """The customer's link in the conversation; False when it could not go
    (window closed, conversation let go, Autentique or the channel failing):
    the e-mail from Autentique has it anyway."""
    try:
        link = await autentique.signing_link(signature)
    except autentique.AutentiqueError as exc:
        logger.warning("No signing link for the conversation: %s", exc)
        return False
    first = name.split()[0] if name.split() else ""
    text = content.render("contract_sign.text", nome=first, link=link, email=email)
    assert contact.chat_id is not None and contact.id is not None
    try:
        async with pool.connection() as conn:
            await inbox.lock_as_owner(conn, contact, user)
            if await inbox.send(contact.channel, contact.chat_id, text) != "ok":
                return False
            await contacts_repo.record_message(
                conn, contact.id, contact.chat_id, "assistant", text, "contract_sign",
                author_user_id=user["user_id"])
    except InboxError:
        return False
    return True


# A send still "sending" after this long was interrupted, not under way:
# the call to Autentique is bounded well below it.
STALE_MINUTES = 2
_CREATE_SECONDS = 60


async def release(pool, user: dict, token_hash: str, briefing_id: int) -> dict:
    """"Liberar para reenviar": a send never confirmed, freed once the team
    saw Autentique has no such document."""
    async with pool.connection() as conn:
        await panel_uploads.require_live_session(conn, user, token_hash)
        b = await repo.get(conn, briefing_id)
        contact = await contacts_repo.get_by_id(conn, b.contact_id) if b else None
        if contact is None or contact.id is None or not contact.chat_id:
            raise InboxError(404, "Pedido não encontrado.")
        # Whoever holds the conversation is the one checking Autentique.
        await inbox.lock_as_owner(conn, contact, user, past_window=True)
        if not await signatures_repo.release_stale(conn, briefing_id, STALE_MINUTES):
            raise InboxError(409, _t("not_stale"))
    logger.info("Unconfirmed contract send of request %s released by user %s", briefing_id,
                user["user_id"])
    return {"released": True, "notice": _t("released")}


async def card(conn, briefing_id: int) -> dict | None:
    """The signature block of the request's card, or None before it is sent."""
    sent = await signatures_repo.get(conn, briefing_id)
    if sent is None:
        return None
    if sent.status == "signed":
        status = "assinado"
    elif sent.status == "sent":
        status = "enviado"
    elif sent.created_at is not None and _stale(sent.created_at):
        status = "nao_confirmado"
    else:
        status = "enviando"
    return {"status": status, "sandbox": sent.sandbox, "enviado_em": sent.sent_at,
            "enviado_por": sent.sent_by, "cliente_assinou_em": sent.customer_signed_at,
            "converge_assinou_em": sent.converge_signed_at, "assinado_em": sent.signed_at,
            "arquivo_assinado": sent.signed_file}


def _stale(created_at: datetime) -> bool:
    return datetime.now(UTC) - created_at > timedelta(minutes=STALE_MINUTES)
