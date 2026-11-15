"""The setup charged when the contract is signed (spec venda-ate-pagamento,
increment 5).

The last signature makes the first payment of the setup, as the contract
says: 50% when the team chose "metade", or the first installment when it
chose "parcelado". It goes on the customer's channel in the way they chose to
pay (card, boleto or Pix). By card in installments, the card fee is added, so
the amount of the proposal is what reaches the Converge. The rest of the setup
and the monthly fee come later.

One charge per request and part (migration 0032): a repeated webhook never
charges twice. Every way this stops short is told to the team, who can still
charge from the panel; the contract is signed either way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from core import channels, content
from core.config import settings
from core.integrations import payments
from core.integrations.payments import Method, normalize_tax_id
from core.models import Contact, ProposalBriefing
from core.repositories import audit
from core.repositories import billing as billing_repo
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as details_repo
from core.repositories import contract_signatures as signatures_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as briefings_repo
from core.services import billing, inbox, pricing, test_marks

logger = logging.getLogger(__name__)

FIRST = 1
# The customer's answer, a key or the label of the button they tapped.
_METHODS: dict[str, Method] = {"cartao": "cartao", "cartao de credito": "cartao",
                               "credito": "cartao", "boleto": "boleto", "pix": "pix"}


class Stop(Exception):
    """The charge did not go; `reason` is the key of the team's notice."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def method_of(answer: str | None) -> Method:
    """The way the customer chose; the invoice page (any way) when unknown."""
    return _METHODS.get(pricing.normalize(answer or ""), "link")


@dataclass(frozen=True)
class First:
    """The first payment of the setup: what reaches the Converge and what the
    customer pays (the same, but by card in installments)."""
    method: Method
    net_cents: int
    value_cents: int
    description: str


def first_payment(terms: pricing.SetupPlan, table: pricing.Table, answer: str | None) -> First:
    """From the payments the signed contract states (never a quote edited later)."""
    method = method_of(answer)
    if terms.count == 1:
        description = content.text("contract_charge.description_vista")
    elif terms.forma == "metade":
        description = content.text("contract_charge.description_metade")
    else:
        description = content.render("contract_charge.description_parcela", n=terms.count)
    # The contract adds the card fee to the installments only.
    fee = method == "cartao" and terms.forma == "parcelado" and terms.count > 1
    value = pricing.with_card_fee(terms.first, table) if fee else terms.first
    return First(method, terms.first, value, description)


async def charge_signed(conn, briefing_id: int, contact_id: int) -> str:
    """Charge the first payment of a signed contract; never raises. Again for
    the same contract, it picks up where it stopped (a worker that died
    midway, say) and never makes a second charge."""
    contact = None
    try:
        contact = await contacts_repo.get_by_id(conn, contact_id)
        briefing = await briefings_repo.get(conn, briefing_id)
        sent = await signatures_repo.get(conn, briefing_id)
        if contact is None or briefing is None or sent is None:
            return "unknown"
        # One worker at a time per contract, across the commits below: the
        # unique row stops a second local charge, this a second one at Asaas.
        if not await _lock(conn, briefing_id):
            return "busy"
        try:
            outcome = await _charge(conn, contact, briefing, sent)
        finally:
            await _unlock(conn, briefing_id)
    except Stop as stop:
        await _rollback(conn)
        logger.info("Setup charge of request %s not sent: %s", briefing_id, stop.reason)
        if contact is not None:
            await _tell_team(contact, f"contract_charge.team_{stop.reason}")
        return stop.reason
    except Exception as exc:
        await _rollback(conn)
        logger.exception("Setup charge of request %s failed (%s)", briefing_id,
                         type(exc).__name__)
        if contact is not None:
            await _tell_team(contact, "contract_charge.team_failed")
        return "failed"
    return outcome


def _key(briefing_id: int) -> str:
    return f"contract_charge:{briefing_id}"


async def _lock(conn, briefing_id: int) -> bool:
    """A session lock (it outlives each commit); False while another worker
    holds it, and that one finishes the charge."""
    cur = await conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                             (_key(briefing_id),))
    locked = bool((await cur.fetchone())[0])
    await conn.commit()
    return locked


async def _unlock(conn, briefing_id: int) -> None:
    await _rollback(conn)
    try:
        await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                           (_key(briefing_id),))
        await conn.commit()
    except Exception as exc:
        logger.warning("Setup charge lock not released (%s)", type(exc).__name__)


async def _rollback(conn) -> None:
    try:
        await conn.rollback()
    except Exception:
        pass


async def _charge(conn, contact: Contact, briefing: ProposalBriefing,
                  sent: signatures_repo.Sent) -> str:
    assert contact.id is not None
    done = await billing_repo.setup_charge(conn, briefing.id, FIRST)
    if done is not None and (done["status"] != "pendente" or done["sent_at"] is not None):
        return "duplicate"
    if not settings.payments_configured():
        raise Stop("off")
    if (sent.sandbox or contact.test_mode) and settings.asaas_env == "production":
        raise Stop("test")
    table = pricing.load_table()
    if sent.terms is None or table is None:
        raise Stop("no_terms")
    customer = await _customer(conn, contact, briefing.id)
    if done is None:
        first = first_payment(sent.terms, table, briefing.answers.get("pagamento"))
        due = billing._today() + timedelta(days=billing.DEFAULT_DUE_DAYS)
        charge_id = await billing_repo.insert_setup_charge(
            conn, customer_id=customer["id"], briefing_id=briefing.id, part=FIRST,
            method=first.method, value_cents=first.value_cents,
            description=first.description, due_date=due)
        if charge_id is None:
            return "duplicate"
        # As in the panel: the row first, so a lost answer is found again by
        # charge:<id>, never charged twice.
        await conn.commit()
        method, value, description, provider_id = (first.method, first.value_cents,
                                                   first.description, None)
    else:
        charge_id, method, value = done["id"], done["method"], done["value_cents"]
        description, due, provider_id = done["description"], done["due_date"], done["provider_id"]
    url = await _in_asaas(conn, contact, charge_id, customer, provider_id,
                          payments.NewCharge(customer_id=customer["provider_id"], method=method,
                                             value_cents=value, due_date=due,
                                             description=description,
                                             external_ref=f"charge:{charge_id}"))
    text = billing.charge_message(method, description, value, due, url)
    delivered = await _deliver(conn, contact, charge_id, text,
                               billing._template_params(contact, description, value, due, url))
    if not contact.blocked_at:
        await billing._email(conn, contact, "charge", {"descricao": description, "cobranca": text})
        await conn.commit()  # its audit line, before the lock's rollback
    await _tell_team(contact, "contract_charge.team_sent" if delivered
                     else "contract_charge.team_not_delivered",
                     valor=billing.format_brl(value), descricao=description)
    return "sent" if delivered else "not_delivered"


async def _in_asaas(conn, contact: Contact, charge_id: int, customer: dict,
                    provider_id: str | None, new: payments.NewCharge) -> str:
    """The charge's payment link at Asaas: the one it has (by id, or by our
    reference when the answer to its creation was lost), else a new one."""
    assert contact.id is not None
    port = payments.payments()
    try:
        if provider_id is not None:
            current = await port.get_charge(provider_id)
            if current is None or current.status != "pendente" or not current.invoice_url:
                raise Stop("closed")
            return current.invoice_url
        created = await port.find_charge(new.external_ref)
        if created is not None and (created.status != "pendente" or not created.invoice_url):
            # Made before the worker died, and paid or cancelled since: bound
            # here (its webhook keeps the status), never asked for again.
            await billing_repo.set_provider(conn, charge_id, created.provider_id, None)
            await conn.commit()
            raise Stop("closed")
        if created is None:
            try:
                created = await port.create_charge(new)
            except payments.PaymentsTransient:
                created = await billing._recover(port, charge_id)
                if created is None:
                    raise Stop("asaas") from None
    except payments.PaymentsRejected:
        await billing_repo.set_status(conn, charge_id, "cancelado")
        await conn.commit()
        raise Stop("asaas") from None
    except payments.PaymentsError:
        raise Stop("asaas") from None
    await billing_repo.set_provider(conn, charge_id, created.provider_id, created.invoice_url)
    await audit.record(conn, provider="asaas", capability="billing", action="contract_charge",
                       target=created.provider_id, contact_id=contact.id, succeeded=True)
    await conn.commit()
    return created.invoice_url or ""


async def _customer(conn, contact: Contact, briefing_id: int) -> dict:
    """The contact's Asaas customer, created with the contract's party (its
    name and CNPJ or CPF): never asked of the customer again."""
    assert contact.id is not None
    known = await billing_repo.customer_of(conn, contact.id)
    if known is not None:
        return known
    details = await details_repo.get(conn, briefing_id) or {}
    tax_id = normalize_tax_id(details.get("documento", ""))
    if tax_id is None:
        raise Stop("tax_id")
    ref = f"contact:{contact.id}"
    port = payments.payments()
    name = (details.get("razao_social") or contact.name or "").strip() or f"Contato {contact.id}"
    try:
        found = await port.find_customer(ref) or await port.create_customer(payments.NewCustomer(
            name=name, tax_id=tax_id, email=details.get("assinante_email") or contact.email,
            phone=contact.phone, external_ref=ref))
    except payments.PaymentsError:
        raise Stop("asaas") from None
    customer_id = await billing_repo.save_customer(conn, contact.id, found.provider_id, tax_id)
    return {"id": customer_id, "provider_id": found.provider_id, "tax_id": tax_id}


async def _deliver(conn, contact: Contact, charge_id: int, text: str,
                   params: list[str]) -> bool:
    """On the customer's channel: free text inside the window; past it on
    WhatsApp, the approved charge template, or nothing (the team resends from
    the panel). A blocked contact hears nothing."""
    assert contact.id is not None
    if contact.blocked_at or not contact.chat_id:
        return False
    chat_id = contact.chat_id
    if not channels.is_whatsapp(chat_id) or await inbox_repo.window_open(conn, contact.id):
        result = await channels.deliver(chat_id, text, parse_mode=None)
    elif inbox.template_ready(settings.whatsapp_charge_template):
        result = await inbox.send_template(chat_id, settings.whatsapp_charge_template or "",
                                           params, ["reopen:charge"])
    else:
        return False
    if result != "ok":
        return False
    await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "billing")
    await billing_repo.mark_sent(conn, charge_id)
    await conn.commit()
    return True


async def _tell_team(contact: Contact, template: str, **values: str) -> None:
    """Best effort, like the other notices of the sale."""
    try:
        text = test_marks.team_text(content.render(
            template, nome=contact.name or "Sem nome", **values), contact)
        inbox.push_to_team(text, contact)
        if not test_marks.quiet(contact):
            await inbox.notify_team(text, contact.id)
    except Exception as exc:
        logger.warning("Setup charge team notice failed (%s)", type(exc).__name__)
