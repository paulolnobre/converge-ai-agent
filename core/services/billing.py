"""Charges from the Inbox (Asaas, the Converge's own account).

Whoever holds the conversation charges, on the customer's channel, within the
same rules as a reply. Once Asaas has a charge, it is committed here before
anything else can fail: a charge the provider holds is never lost locally.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from core import channels, content
from core.config import settings
from core.integrations import payments, telegram, workspace
from core.integrations.payments import Method, normalize_tax_id
from core.integrations.workspace.models import OutgoingMail
from core.models import Contact
from core.repositories import audit
from core.repositories import billing as billing_repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.services import inbox, push
from core.services.inbox import InboxError

logger = logging.getLogger(__name__)

_EVENT_STATUS = {
    "PAYMENT_RECEIVED": "pago", "PAYMENT_CONFIRMED": "pago",
    "PAYMENT_OVERDUE": "vencido", "PAYMENT_DELETED": "cancelado",
    "PAYMENT_REFUNDED": "estornado",
}
# An e-mail goes last and gives up after this: it must never hold up the rest.
_EMAIL_SECONDS = 20
_TRANSITIONS = {
    "pendente": {"pago", "vencido", "cancelado", "estornado"},
    "vencido": {"pago", "cancelado", "estornado"},
    "pago": {"estornado"},
}


def event_id(payload: dict) -> str | None:
    """A stable id shared by the durable queue and the database deduplication."""
    key = payload.get("id")
    if key is not None:
        return key if isinstance(key, str) and key.strip() else None
    kind, payment = payload.get("event"), payload.get("payment")
    if not isinstance(kind, str) or not kind.strip() or not isinstance(payment, dict):
        return None
    provider_id = payment.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    return f"{kind}:{provider_id}"


async def handle_event(conn, payload: dict) -> str:
    """Apply one payment event, then attempt notices independently.

    Status, audit and event id commit together before external sends. Notices
    are best effort, like Inbox team notices: a lost reply to a send must not
    trigger duplicate messages or roll back a payment already received.
    """
    if not isinstance(payload, dict):
        return "ignored"
    kind, payment = payload.get("event"), payload.get("payment")
    if not isinstance(kind, str) or not isinstance(payment, dict):
        return "ignored"
    provider_id = payment.get("id")
    key = event_id(payload)
    if not isinstance(provider_id, str) or not provider_id.strip() or key is None:
        return "ignored"
    status = _EVENT_STATUS.get(kind)
    if status is None:
        logger.debug("Asaas event ignored")
        return "ignored"
    if not await billing_repo.claim_event(conn, key):
        return "duplicate"
    reference = payment.get("externalReference")
    match = re.fullmatch(r"charge:([1-9][0-9]{0,18})", reference) if isinstance(reference, str) else None
    local_id = int(match[1]) if match else None
    if local_id is not None and local_id > 2**63 - 1:
        local_id = None
    charge = await billing_repo.charge_for_event(conn, provider_id=provider_id, local_id=local_id)
    if charge is None:
        logger.info("Asaas event has no local charge")
        return "unknown"
    if status not in _TRANSITIONS.get(charge["status"], set()):
        return "stale"
    if charge["provider_id"] is None:
        # No invoice URL is needed for an acknowledgement; never copy or log
        # customer/payment-link fields from an inbound event.
        await billing_repo.set_provider(conn, charge["id"], provider_id, None)
    if status == "pago":
        await billing_repo.set_paid(conn, charge["id"])
    else:
        await billing_repo.set_status(conn, charge["id"], status)
    await audit.record(conn, provider="asaas", capability="billing", action=f"webhook_{status}",
                       target=provider_id, contact_id=charge["contact_id"], succeeded=True)
    await conn.commit()
    if status == "pago":
        await _paid_notices(conn, charge)
    return "applied"


async def _paid_customer(conn, charge: dict, contact: Contact | None, text: str) -> str:
    """The thanks, free-form inside the window; past it on WhatsApp, the
    approved template, or nothing when there is none."""
    if (not channels.is_whatsapp(charge["chat_id"]) or
            await inbox_repo.window_open(conn, charge["contact_id"])):
        return await channels.deliver(charge["chat_id"], text, parse_mode=None)
    template = settings.whatsapp_paid_template
    if not inbox.template_ready(template):
        return "window_closed"
    name = inbox.first_name(contact.name) if contact and contact.name else "tudo bem"
    return await inbox.send_template(charge["chat_id"], template or "",
                                     [name, format_brl(charge["value_cents"]),
                                      charge["description"]],
                                     language=settings.whatsapp_paid_language or "pt_BR")


async def _paid_notices(conn, charge: dict) -> None:
    contact = None
    try:
        # A savepoint keeps an optional history write from poisoning the
        # connection; the payment above is already durable.
        async with conn.transaction():
            contact = await contacts_repo.get_by_id(conn, charge["contact_id"])
            # Blocked in the panel: nothing to the customer. The team still
            # hears of the payment below.
            if charge["chat_id"] and not (contact and contact.blocked_at):
                text = content.render("billing.paid_customer", descricao=charge["description"],
                                      valor=format_brl(charge["value_cents"]))
                result = await _paid_customer(conn, charge, contact, text)
                if result == "ok":
                    await contacts_repo.record_message(conn, charge["contact_id"],
                        charge["chat_id"], "assistant", text, "billing")
    except Exception as exc:
        logger.warning("Asaas customer notice failed (%s)", type(exc).__name__)
    team_text = content.render("billing.paid_team", nome=(contact.name if contact else None)
                               or "Sem nome", descricao=charge["description"],
                               valor=format_brl(charge["value_cents"]))
    if settings.team_chat_id:
        try:
            await telegram.send_message(settings.team_chat_id, team_text, parse_mode=None)
        except Exception as exc:
            logger.warning("Asaas team notice failed (%s)", type(exc).__name__)
    push.schedule_team(title=team_text, url=f"/painel/#inbox/{charge['contact_id']}",
                       tag=f"billing-{charge['id']}", chat_id=charge["chat_id"])
    if not (contact and contact.blocked_at):
        await _email(conn, contact, "paid", {"descricao": charge["description"],
                                             "valor": format_brl(charge["value_cents"])})


async def _email(conn, contact: Contact | None, kind: str, values: dict) -> bool:
    """The charge or the payment by e-mail too, best effort: when it is on, the
    contact has an e-mail and there is a mailbox. A failure is logged and
    audited, never raised: the charge itself already stands. A test contact's
    e-mail says so in the subject."""
    mailbox = workspace.sender_mailbox()
    if (not settings.billing_emails or not mailbox or contact is None
            or contact.id is None or not contact.email):
        return False
    values = {"nome": (contact.name or "").split(" ")[0] or "tudo bem", **values}
    subject = content.render(f"billing.{kind}_email_subject", **values)
    if contact.test_mode:
        subject = f"[TESTE] {subject}"
    sent = False
    try:
        async with asyncio.timeout(_EMAIL_SECONDS):
            await workspace.mail_sender().send_message(mailbox, OutgoingMail(
                to=[contact.email], subject=subject,
                body_text=content.render(f"billing.{kind}_email", **values).strip(),
            ))
        sent = True
    except Exception as exc:
        # The address is not logged: only what failed.
        logger.warning("Billing e-mail (%s) not sent: %s", kind, type(exc).__name__)
    await audit.record(conn, provider=workspace.mail_sender_provider(), capability="mail",
                       action=f"billing_{kind}_email", contact_id=contact.id, succeeded=sent)
    return sent


def format_brl(cents: int) -> str:
    reais, rest = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{rest:02d}"


def mask_tax_id(digits: str) -> str:
    if len(digits) == 14:
        return f"**.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-**"
    return f"***.{digits[3:6]}.{digits[6:9]}-**"


def charge_message(method: Method, description: str, cents: int, due: date, link: str) -> str:
    path = "billing.charge_sent" if method == "link" else "billing.charge_sent_fixed"
    return content.render(path, descricao=description, valor=format_brl(cents),
                          vencimento=due.strftime("%d/%m/%Y"), link=link)


def _charge_template_ready() -> bool:
    return inbox.template_ready(settings.whatsapp_charge_template)


def _template_params(contact: Contact, description: str, cents: int, due: date,
                     link: str) -> list[str]:
    """The approved charge template's {{1}} to {{5}}, in its order."""
    return [inbox.first_name(contact.name) if contact.name else "tudo bem", description,
            format_brl(cents), due.strftime("%d/%m/%Y"), link]


# ── Creating, resending and cancelling ──────────────────────────────────────

MAX_DUE_DAYS = 365
DEFAULT_DUE_DAYS = 3


# The panel charges by link, boleto or Pix; a card only goes with the setup of a
# signed contract (contract_charge), with the card fee.
PanelMethod = Literal["link", "boleto", "pix"]


class ChargeForm(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    value_cents: int = Field(ge=1, le=10_000_000)
    due_date: date
    method: PanelMethod = "link"
    tax_id: str | None = Field(default=None, max_length=30)

    @field_validator("description")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Escreva a descrição.")
        return value


def _today() -> date:
    return datetime.now(ZoneInfo(settings.office_timezone)).date()


def _payments_on() -> None:
    if not settings.payments_configured():
        raise InboxError(404, "Cobrança desligada.")


def _asaas_error(exc: payments.PaymentsError) -> InboxError:
    if isinstance(exc, payments.PaymentsRejected):
        return InboxError(422, "O Asaas recusou: " + (", ".join(exc.codes) or "pedido inválido"))
    return InboxError(502, "Não foi possível falar com o Asaas.")


async def _audit(conn, action: str, contact_id: int, user: dict, target: str | None,
                 succeeded: bool) -> None:
    await audit.record(conn, provider="asaas", capability="billing", action=action,
                       target=target, contact_id=contact_id, succeeded=succeeded,
                       detail=f"user:{user['user_id']}")


async def _customer(conn, contact: Contact, tax_id_text: str | None) -> dict:
    assert contact.id is not None
    known = await billing_repo.customer_of(conn, contact.id)
    if known is not None:
        return known
    tax_id = normalize_tax_id(tax_id_text or "")
    if tax_id is None:
        raise InboxError(422, "CPF/CNPJ inválido.")
    ref = f"contact:{contact.id}"
    port = payments.payments()
    found = await port.find_customer(ref) or await port.create_customer(payments.NewCustomer(
        name=(contact.name or "").strip() or f"Contato {contact.id}", tax_id=tax_id,
        email=contact.email, phone=contact.phone, external_ref=ref))
    customer_id = await billing_repo.save_customer(conn, contact.id, found.provider_id, tax_id)
    return {"id": customer_id, "provider_id": found.provider_id, "tax_id": tax_id}


async def _deliver(conn, contact: Contact, user: dict, session_id: int, charge_id: int,
                   text: str, template_params: list[str] | None = None) -> None:
    """Send the charge; recorded only once the channel took it. Past the 24h
    window on WhatsApp it goes as the approved template, when there is one."""
    assert contact.id is not None and contact.chat_id is not None
    template = settings.whatsapp_charge_template
    use_template = template_params is not None and inbox.template_ready(template)
    # Our clock decides first: past it, Meta may take free text with a 200 and
    # refuse it later (131047), and the charge would read as sent.
    delivery = "window_closed"
    if not use_template or await inbox._window_open(conn, contact):
        delivery = await inbox.send(contact.channel, contact.chat_id, text)
    if delivery == "window_closed" and use_template:
        delivery = await inbox.send_template(contact.chat_id, template or "",
                                             template_params or [], ["reopen:charge"])
    if delivery == "window_closed":
        raise InboxError(409, content.text("inbox.window_closed"))
    if delivery != "ok":
        raise InboxError(502, "Cobrança criada, mas a mensagem não chegou. Use Reenviar.")
    await contacts_repo.record_message(conn, contact.id, contact.chat_id, "assistant", text,
                                       "billing", author_user_id=user["user_id"])
    await billing_repo.mark_sent(conn, charge_id)
    await handoffs_repo.touch(conn, session_id)


async def create(conn, contact_id: int, user: dict, form: ChargeForm) -> None:
    _payments_on()
    today = _today()
    if not today <= form.due_date <= today + timedelta(days=MAX_DUE_DAYS):
        raise InboxError(422, "Vencimento: de hoje até um ano.")
    contact = await inbox._contact(conn, contact_id)
    _not_in_production_test(contact)
    await inbox.lock_as_owner(conn, contact, user, past_window=_charge_template_ready())
    try:
        customer = await _customer(conn, contact, form.tax_id)
    except payments.PaymentsError as exc:
        raise _asaas_error(exc) from None
    charge_id = await billing_repo.insert_charge(
        conn, customer_id=customer["id"], origin="painel", method=form.method,
        value_cents=form.value_cents, description=form.description,
        due_date=form.due_date, status="pendente", requested_by=user["user_id"])
    # Saved before Asaas is asked: if its answer is lost, this row and the
    # reference charge:<id> are how the charge is found again — never a second one.
    await conn.commit()
    port = payments.payments()
    try:
        created = await port.create_charge(payments.NewCharge(
            customer_id=customer["provider_id"], method=form.method,
            value_cents=form.value_cents, due_date=form.due_date,
            description=form.description, external_ref=f"charge:{charge_id}"))
    except payments.PaymentsTransient:
        created = await _recover(port, charge_id)
        if created is None:
            raise InboxError(502, "Não foi possível confirmar com o Asaas. A cobrança ficou "
                                  "como não enviada; use Reenviar para conferir.") from None
    except payments.PaymentsError as exc:
        await billing_repo.set_status(conn, charge_id, "cancelado")
        await conn.commit()
        raise _asaas_error(exc) from None
    await billing_repo.set_provider(conn, charge_id, created.provider_id, created.invoice_url)
    await conn.commit()
    session_id = await inbox.lock_as_owner(conn, contact, user, past_window=_charge_template_ready())
    text = charge_message(form.method, form.description, form.value_cents, form.due_date,
                          created.invoice_url or "")
    try:
        await _deliver(conn, contact, user, session_id, charge_id, text,
                       _template_params(contact, form.description, form.value_cents,
                                        form.due_date, created.invoice_url or ""))
    except InboxError:
        await _audit(conn, "create_charge", contact_id, user, created.provider_id, False)
        await conn.commit()
        raise
    await _audit(conn, "create_charge", contact_id, user, created.provider_id, True)
    await _email(conn, contact, "charge", {"descricao": form.description, "cobranca": text})


async def _recover(port, charge_id: int) -> payments.Charge | None:
    """The charge Asaas may hold under our reference; None if it has none or
    cannot say right now."""
    try:
        return await port.find_charge(f"charge:{charge_id}")
    except payments.PaymentsError:
        return None


async def _confirmed(conn, charge: dict) -> str | None:
    """The charge's Asaas id, looking it up by reference if the answer to its
    creation was lost. None: Asaas does not have it."""
    if charge["provider_id"]:
        return charge["provider_id"]
    try:
        found = await payments.payments().find_charge(f"charge:{charge['id']}")
    except payments.PaymentsError as exc:
        raise _asaas_error(exc) from None
    if found is None:
        return None
    await billing_repo.set_provider(conn, charge["id"], found.provider_id, found.invoice_url)
    return found.provider_id


def _not_in_production_test(contact: Contact) -> None:
    if contact.test_mode and settings.asaas_env == "production":
        raise InboxError(409, "Em teste, cobranças só no sandbox.")


async def resend(conn, contact_id: int, charge_id: int, user: dict) -> None:
    _payments_on()
    contact = await inbox._contact(conn, contact_id)
    _not_in_production_test(contact)
    session_id = await inbox.lock_as_owner(conn, contact, user,
                                           past_window=_charge_template_ready())
    charge = await billing_repo.charge_of(conn, contact_id, charge_id)
    if charge is None:
        raise InboxError(404, "Cobrança não encontrada.")
    if charge["status"] != "pendente":
        raise InboxError(409, "Esta cobrança não está em aberto.")
    provider_id = await _confirmed(conn, charge)
    if provider_id is None:
        raise InboxError(409, "Esta cobrança não chegou ao Asaas. Cancele e crie de novo.")
    try:
        current = await payments.payments().get_charge(provider_id)
    except payments.PaymentsError as exc:
        raise _asaas_error(exc) from None
    if current is None or current.status != "pendente" or not current.invoice_url:
        raise InboxError(409, "Esta cobrança não está mais em aberto no Asaas.")
    text = charge_message(charge["method"], charge["description"], charge["value_cents"],
                          charge["due_date"], current.invoice_url)
    await _deliver(conn, contact, user, session_id, charge_id, text,
                   _template_params(contact, charge["description"], charge["value_cents"],
                                    charge["due_date"], current.invoice_url))
    await _audit(conn, "resend_charge", contact_id, user, provider_id, True)
    await _email(conn, contact, "charge", {"descricao": charge["description"], "cobranca": text})


async def cancel(conn, contact_id: int, charge_id: int, user: dict) -> None:
    """Sends nothing to the customer: anyone on the team may cancel."""
    _payments_on()
    charge = await billing_repo.charge_of(conn, contact_id, charge_id)
    if charge is None:
        raise InboxError(404, "Cobrança não encontrada.")
    if charge["status"] != "pendente":
        raise InboxError(409, "Esta cobrança não está em aberto.")
    provider_id = await _confirmed(conn, charge)
    if provider_id is not None:
        # Ask Asaas first: a charge already cancelled there (by hand) just
        # closes here; one already paid is not cancelled; and one this account
        # does not have (another account or environment) is never called
        # cancelled on a guess.
        port = payments.payments()
        try:
            current = await port.get_charge(provider_id)
            if current is None:
                raise InboxError(409, "Não encontrei esta cobrança nesta conta do Asaas "
                                      "(ambiente ou conta trocados?). Confira antes de cancelar.")
            if current.status == "pendente":
                await port.cancel_charge(provider_id)
            elif current.status != "cancelado":
                raise InboxError(409, "Esta cobrança não está mais em aberto no Asaas.")
        except payments.PaymentsError as exc:
            raise _asaas_error(exc) from None
    await billing_repo.set_status(conn, charge_id, "cancelado")
    await _audit(conn, "cancel_charge", contact_id, user, provider_id, True)


async def summary(conn, contact_id: int) -> dict:
    customer = await billing_repo.customer_of(conn, contact_id)
    return {
        "tax_id": mask_tax_id(customer["tax_id"]) if customer and customer["tax_id"] else None,
        "charges": await billing_repo.charges_of(conn, contact_id),
        "min_due": _today().isoformat(),
        "default_due": (_today() + timedelta(days=DEFAULT_DUE_DAYS)).isoformat(),
        "max_due": (_today() + timedelta(days=MAX_DUE_DAYS)).isoformat(),
    }
