"""The panel's "Pedidos de proposta" tab: the requests the team owes a
proposal on, with the deadline (the request plus two working days) and the
"Marcar proposta como enviada" action.

No HTTP here: core/web/panel.py only calls these and answers.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import content
from core.config import settings
from core.models import Contact, ProposalBriefing
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as contract_repo
from core.repositories import funnel as funnel_repo
from core.repositories import panel_auth as auth_repo
from core.repositories import proposal_briefings as repo
from core.repositories import proposal_quotes as quotes_repo
from core.services import contract_sign, pricing, proposal_quote
from core.services.proposal_briefing import BUTTON_STEPS, is_clinic, shown_answer, shown_steps

# What the table still needs (pricing.REQUIRED), as the team reads it.
logger = logging.getLogger(__name__)

_MISSING_LABEL = {"canais": "label_canais_lista", "sistemas": "label_sistemas_lista",
                  "volume": "label_volume", "areas": "label_areas", "roteiro": "label_processo"}
# The table's own defaults used for what the person did not say (Scope.premissas).
_PREMISE = {"usuarios": "Usuários do painel: padrão da tabela",
            "documentos": "Documentos: padrão da tabela", "sla": "SLA: padrão da tabela"}
_STATUS = {"sent": "novo", "incomplete_sent": "incompleto", "proposal_sent": "proposta enviada"}
_OPEN = ("novo", "incompleto")
_SEPARATOR = " · "


def _office_day(moment: datetime) -> date:
    return moment.astimezone(ZoneInfo(settings.office_timezone)).date()


def deadline(submitted_at: datetime, days: int = 2) -> date:
    """`days` working days after the request, counted from the next day in the
    office's time zone. With no working day configured, plain days."""
    workdays = settings.workdays() or set(range(1, 8))
    day, left = _office_day(submitted_at), days
    while left > 0:
        day += timedelta(days=1)
        if day.isoweekday() in workdays:
            left -= 1
    return day


def _requested_at(b: ProposalBriefing) -> datetime:
    """When the request reached the team: sent by the person, or, for one
    abandoned before that, when the team was told."""
    return b.submitted_at or b.incomplete_notified_at or b.updated_at


def _who(quem: str, contact: Contact | None) -> tuple[str | None, str | None, str | None]:
    """Name, company and line of business. A prefilled "quem" is
    "nome · empresa · ramo · cargo"; anything else is free text, left as is."""
    parts = [p.strip() for p in quem.split(_SEPARATOR)] if _SEPARATOR in quem else []
    name = contact.name if contact else None
    company = contact.organization if contact else None
    if len(parts) >= 3:
        return parts[0] or name, parts[1] or company, parts[2] or None
    return name, company, None


async def _row(conn, b: ProposalBriefing, today: date) -> dict:
    contact = await contacts_repo.get_by_id(conn, b.contact_id)
    quem = b.answers.get("quem", "")
    name, company, line = _who(quem, contact)
    status = _STATUS.get(b.status, b.status)
    requested = _requested_at(b)
    due = deadline(requested)
    still_open = status in _OPEN
    return {
        "id": b.id,
        "contact_id": b.contact_id,
        "nome": name,
        "empresa": company,
        "ramo": line,
        "quem": quem,
        "email": contact.email if contact else None,
        "telefone": contact.phone if contact else None,
        "status": status,
        "pedido_em": requested,
        "prazo": due,
        "atrasado": still_open and today > due,
        "vence_hoje": still_open and today == due,
        "proposta_enviada_em": b.proposal_sent_at,
    }


def _order(row: dict) -> tuple:
    if row["status"] in _OPEN:
        return (0, row["prazo"].toordinal(), row["pedido_em"].timestamp(), row["id"])
    sent = row["proposta_enviada_em"] or row["pedido_em"]
    return (1, -sent.timestamp(), 0, -row["id"])


def _today(now: datetime | None) -> date:
    return _office_day(now or datetime.now(ZoneInfo(settings.office_timezone)))


async def listing(conn, *, now: datetime | None = None) -> list[dict]:
    """Every request sent, abandoned or answered: the open ones first, the
    nearest deadline on top; the ones already answered last, newest first."""
    today = _today(now)
    rows = [await _row(conn, b, today) for b in await repo.requests(conn)]
    return sorted(rows, key=_order)


async def detail(conn, briefing_id: int, *, now: datetime | None = None) -> dict | None:
    b = await repo.get(conn, briefing_id)
    if b is None or b.status not in _STATUS:
        return None
    row = await _row(conn, b, _today(now))
    row["respostas"] = [
        {"rotulo": content.text(f"proposal.label_{step}"), "resposta": shown_answer(b.answers, step),
         "do_preenchimento": step in b.prefilled}
        for step in shown_steps(b.answers)
    ]
    row["proposta_enviada_por"] = await _user_name(conn, b.proposal_sent_by)
    row["orcamento"] = await _quote(conn, b)
    row["contrato"] = await _contract(conn, b.id)
    row["assinatura"] = await _signature(conn, b.id)
    row["assinatura_pronta"] = contract_sign.ready()
    return row


async def _signature(conn, briefing_id: int) -> dict | None:
    """Optional, like the quote: a failing read never hides the card."""
    try:
        found = await contract_sign.card(conn, briefing_id)
    except Exception:
        logger.exception("The signature of request %s could not be shown", briefing_id)
        return None
    if found is not None:
        found["enviado_por"] = await _user_name(conn, found["enviado_por"])
    return found


def _document(digits: str) -> str:
    if len(digits) == 11 and digits.isdigit():
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if len(digits) == 14 and digits.isdigit():
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return digits


async def _contract(conn, briefing_id: int) -> list[dict] | None:
    """What the customer sent for the contract, as the team reads it; None
    before they send it. A failing read never takes the card down."""
    try:
        data = await contract_repo.get(conn, briefing_id)
    except Exception:
        logger.exception("The contract data of request %s could not be shown", briefing_id)
        return None
    if not data:
        return None
    cep = data.get("cep", "")
    cep = f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep
    address = (f"{data.get('endereco', '')}, {data.get('bairro', '')}, "
               f"{data.get('cidade', '')}/{data.get('uf', '')}, CEP {cep}")
    signer = (f"{data.get('assinante_nome', '')} ({data.get('assinante_cargo', '')}), "
              f"CPF {_document(data.get('assinante_cpf', ''))}, {data.get('assinante_email', '')}")
    return [
        {"rotulo": content.text("contract.label_razao_social"),
         "valor": data.get("razao_social", "")},
        {"rotulo": content.text("contract.label_documento"),
         "valor": _document(data.get("documento", ""))},
        {"rotulo": content.text("contract.label_endereco"), "valor": address},
        {"rotulo": content.text("contract.label_assinante"), "valor": signer},
    ]


async def _user_name(conn, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await auth_repo.user_by_id(conn, user_id)
    return user["name"] if user else None


async def _quote(conn, b: ProposalBriefing) -> dict | None:
    """The quote block of the card. Optional: a broken table or a failing read
    shows "unavailable", never takes the request's answers down with it."""
    try:
        return await _quote_block(conn, b)
    except Exception:
        logger.exception("The preliminary quote of request %s could not be shown", b.id)
        return {"indisponivel": True}


async def _quote_block(conn, b: ProposalBriefing) -> dict | None:
    """The preliminary quote, for the team; or what the table still needs for
    one; None without a table, or for a request from before the buttons."""
    table = pricing.load_table()
    if table is None:
        return None
    saved = await quotes_repo.get(conn, b.id)
    if saved is None:
        if not any(step in b.answers for step in BUTTON_STEPS):
            return None
        computed = proposal_quote.compute(b.answers, is_clinic(b.answers))
        if not isinstance(computed, proposal_quote.Missing):
            return None
        return {"faltam": [content.text(f"proposal.{_MISSING_LABEL[k]}")
                           for k in computed.faltam]}
    warnings = []
    if saved.setup_cents < table.piso_implantacao:
        warnings.append("Implantação abaixo do piso da tabela")
    if saved.monthly_cents < table.piso_mensalidade:
        warnings.append("Mensalidade abaixo do piso da tabela")
    return {
        "calculado": {"implantacao": saved.calc_setup_cents,
                      "faixa_min": saved.calc_setup_low_cents,
                      "faixa_max": saved.calc_setup_high_cents,
                      "mensalidade": saved.calc_monthly_cents},
        "atual": {"implantacao": saved.setup_cents, "mensalidade": saved.monthly_cents,
                  "forma": saved.forma_implantacao},
        "parcelas_max": table.parcelas_max,
        "plano": dataclasses.asdict(pricing.setup_plan(
            saved.setup_cents, saved.forma_implantacao, table.parcelas_max)),
        "detalhe": saved.detail,
        "premissas": [_PREMISE.get(p, p) for p in saved.premises],
        "observacao": saved.note,
        "editado_por": await _user_name(conn, saved.edited_by),
        "editado_em": saved.edited_at,
        "avisos": warnings,
    }


async def edit_quote(conn, briefing_id: int, user: dict, *, setup_cents: int,
                     monthly_cents: int, note: str | None,
                     forma: str | None = None) -> bool:
    """The team's values. False when the request has no quote."""
    return await quotes_repo.edit(conn, briefing_id, setup_cents=setup_cents,
                                  monthly_cents=monthly_cents, note=note,
                                  user_id=user["user_id"], forma=forma)


async def mark_sent(conn, briefing_id: int, user: dict) -> bool:
    """Record who marked the proposal sent and move the card to "proposta".
    False when the request is not waiting on a proposal (already marked, or
    not a request at all); then nothing moves."""
    if not await repo.mark_proposal_sent(conn, briefing_id, user["user_id"]):
        return False
    b = await repo.get(conn, briefing_id)
    if b is not None:
        await funnel_repo.move_stage(conn, b.contact_id, "proposta",
                                     ("novo", "respondido", "agendado"))
    return True
