"""What the contract needs from the customer, asked after "Aceitar proposta":
the party (legal name, CNPJ or CPF, address) and who signs (name, CPF, e-mail,
role). On WhatsApp, as the Flow `dados_contrato` (content/forms), opened with
what is already known; anything wrong comes back with the same Flow, filled
with what was sent, and only the wrong fields named.

Kept sealed (core/repositories/contract_details.py). The contract itself and
its signature come next (docs: spec venda-ate-pagamento, increment 4).
"""
from __future__ import annotations

import logging
import re

from core import channels, content
from core.integrations.payments import normalize_tax_id
from core.models import Contact, ProposalBriefing
from core.repositories import billing as billing_repo
from core.repositories import contract_details as details_repo
from core.repositories import proposal_briefings as repo
from core.services import contact_details, forms, inbox, test_marks

logger = logging.getLogger(__name__)

FLOW = "dados_contrato"
TOKEN = "contrato:"
COMPANY = ("razao_social", "documento", "cep", "endereco", "bairro", "cidade", "uf")
SIGNER = ("assinante_nome", "assinante_cpf", "assinante_email", "assinante_cargo")
KEYS = COMPANY + SIGNER
UFS = frozenset("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP "
                "SE TO".split())
_SEP = " · "
_VALUE_MAX = 200
_ID_DIGITS = 18
_EMAIL_MAX = 254


def published() -> forms.Form | None:
    try:
        form = forms.load(FLOW)
    except forms.FormError as exc:
        logger.error("The contract Flow is broken: %s", exc)
        return None
    return form if form is not None and form.flow_id else None


def known(b: ProposalBriefing, contact: Contact) -> dict[str, str]:
    """What the request and the contact already say: never asked twice."""
    parts = [p.strip() for p in b.answers.get("quem", "").split(_SEP)]
    who = dict(zip(("nome", "empresa", "ramo", "cargo"), parts)) if len(parts) == 4 else {}
    values = {"razao_social": who.get("empresa") or contact.organization or "",
              "assinante_nome": who.get("nome") or contact.name or "",
              "assinante_email": contact.email or "",
              "assinante_cargo": who.get("cargo", "") if who.get("cargo") != "-" else ""}
    return {k: v for k, v in values.items() if v}


def _message(form: forms.Form, briefing_id: int, values: dict[str, str],
             text: str) -> tuple[str, dict]:
    keyboard = channels.flow_keyboard(
        flow_id=form.flow_id or "", token=f"{TOKEN}{briefing_id}",
        cta=content.text("contract.flow_cta"), screen=form.screen,
        data={key: values.get(key, "")[:_EMAIL_MAX] for key in form.prefill})
    # WhatsApp refused the Flow: the person is told the team takes it from here.
    keyboard["_fallback"] = (content.text("contract.fallback"), None)
    return text, keyboard


async def after_accept(conn, contact: Contact) -> tuple[str, dict] | None:
    """The Flow for the contract's data, after "Aceitar proposta". None when
    there is nothing to send: no proposal sent, or no Flow to send it with (not
    WhatsApp, not published), and then the team is told to ask by hand."""
    if contact.id is None:
        return None
    b = await repo.proposal_sent_for(conn, contact.id)
    if b is None:
        return None
    form = published()
    if form is None or not channels.is_whatsapp(contact.chat_id or ""):
        await tell_team_no_flow(contact)
        return None
    values = await _known_before(conn, b, contact)
    return _message(form, b.id, values, content.text("contract.flow_intro"))


async def _known_before(conn, b: ProposalBriefing, contact: Contact) -> dict[str, str]:
    """Everything already given, the most formal last: the request and the
    contact, the CNPJ or CPF of a charge, an earlier contract, and what was
    sent for this one."""
    values = known(b, contact)
    customer = await billing_repo.customer_of(conn, contact.id) if contact.id else None
    if customer and customer.get("tax_id"):
        values["documento"] = customer["tax_id"]
    if contact.id is not None:
        values.update(_texts(await details_repo.latest_of_contact(conn, contact.id, b.id)))
    values.update(_texts(await details_repo.get(conn, b.id)))
    return values


def _texts(data: dict | None) -> dict[str, str]:
    return {k: v for k, v in (data or {}).items() if k in KEYS and isinstance(v, str) and v}


def _clean(value: object) -> str:
    """Whole, never cut: a value too long is wrong, not shortened into another."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _email(value: str) -> str | None:
    if len(value) > _EMAIL_MAX:
        return None
    found = contact_details.extract(value).email
    return found if found and found == value.lower() else None


def check(values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The data as kept (documents in digits, UF in capitals) and the fields
    that are wrong or missing, by their keys."""
    kept = dict(values)
    wrong: list[str] = []
    for key in KEYS:
        limit = _EMAIL_MAX if key == "assinante_email" else _VALUE_MAX
        if not values.get(key) or len(values[key]) > limit:
            wrong.append(key)
    document = normalize_tax_id(values.get("documento", ""))
    if values.get("documento") and document is None:
        wrong.append("documento")
    kept["documento"] = document or values.get("documento", "")
    cpf = normalize_tax_id(values.get("assinante_cpf", ""))
    if values.get("assinante_cpf") and (cpf is None or len(cpf) != 11):
        wrong.append("assinante_cpf")
    kept["assinante_cpf"] = cpf or values.get("assinante_cpf", "")
    email = _email(values.get("assinante_email", ""))
    if values.get("assinante_email") and email is None:
        wrong.append("assinante_email")
    kept["assinante_email"] = email or values.get("assinante_email", "")
    cep = re.sub(r"\D", "", values.get("cep", ""))
    if values.get("cep") and len(cep) != 8:
        wrong.append("cep")
    kept["cep"] = cep if len(cep) == 8 else values.get("cep", "")
    uf = values.get("uf", "").upper()
    if values.get("uf") and uf not in UFS:
        wrong.append("uf")
    kept["uf"] = uf
    return kept, list(dict.fromkeys(k for k in KEYS if k in wrong))


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} e {items[-1]}"


async def tell_team_no_flow(contact: Contact) -> None:
    """The Flow did not go: the team collects the data by hand."""
    notice = test_marks.team_text(content.render(
        "contract.team_no_flow", nome=contact.name or "Sem nome"), contact)
    inbox.push_to_team(notice, contact)
    try:
        if not test_marks.quiet(contact):
            await inbox.notify_team(notice, contact.id)
    except Exception:
        logger.exception("The team was not told that the contract Flow did not go")


async def apply(conn, contact: Contact, token: str, payload: dict) -> tuple[str, dict | None]:
    """The Flow, answered. Kept when right; otherwise the same Flow again, with
    what was sent, and the wrong fields named."""
    raw_id = token.removeprefix(TOKEN)
    plain = raw_id.isascii() and raw_id.isdigit() and len(raw_id) <= _ID_DIGITS
    b = await repo.get(conn, int(raw_id)) if plain else None
    if b is None or b.contact_id != contact.id or b.status != "proposal_sent":
        return content.text("contract.not_open"), None
    values = {key: _clean(payload.get(key)) for key in KEYS}
    kept, wrong = check(values)
    if wrong:
        form = published()
        fields = _and([content.text(f"contract.campo_{key}") for key in wrong])
        text = content.render("contract.fix", campos=fields)
        return _message(form, b.id, values, text) if form is not None else (text, None)
    await details_repo.save(conn, b.id, kept)
    notice = test_marks.team_text(content.render(
        "contract.team_received", nome=contact.name or "Sem nome"), contact)
    inbox.push_to_team(notice, contact)
    try:
        if not test_marks.quiet(contact):
            await inbox.notify_team(notice, contact.id)
    except Exception:
        logger.exception("The team was not told of the contract data; it is in the panel")
    return content.text("contract.received"), None
