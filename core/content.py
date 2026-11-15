"""The organization's own words, loaded from `content/` rather than the source.

Two things live here that must not live in Python. The first is every sentence
the desk says, so that changing a greeting is not a code change and the person
who owns the organization's voice can read the whole of it in one file. The
second is the list of what the desk must never say — for a clinic that is not
style, it is the difference between a receptionist and a liability.

Placeholders are checked once at startup against what the code can actually
supply. A template that says {profissional} when nothing knows who that is
refuses the deployment, instead of reaching a patient with the braces still in.
"""
from __future__ import annotations

import json
import logging
import string
import tomllib
from functools import lru_cache
from pathlib import Path

from core.config import settings

logger = logging.getLogger(__name__)

# What the code passes to each template today. Adding a placeholder to a message
# means adding it here first — which is the point: the list is the contract, and
# it is checked before the first request rather than discovered by a patient.
# The options of the questions answered with buttons (proposal_quote keys).
_PROPOSAL_OPTIONS = (
    [f"canal_{k}" for k in ("whatsapp", "instagram", "site", "telefone", "email")]
    + [f"biz_area_{k}" for k in ("atendimento", "vendas", "financeiro", "documentos_juridico",
                                 "suporte_tecnico", "rh")]
    + [f"sistema_{k}" for k in ("google_agenda", "planilha", "crm", "gestao", "nenhum")]
    + [f"volume_{k}" for k in ("ate_500", "ate_2000", "ate_5000", "mais_5000")]
    + [f"processo_{k}" for k in ("sim", "parcial", "nao")]
    + [f"usuarios_{k}" for k in ("1_2", "3_5", "6_10", "mais_10")]
    + [f"pagamento_{k}" for k in ("cartao", "boleto", "pix")]
    + [f"label_{k}" for k in ("canais_lista", "volume", "sistemas_lista", "processo",
                              "usuarios_faixa", "pagamento")]
)
_PROPOSAL_BUTTON_ASKS = ("ask_canais_lista", "ask_volume", "ask_biz_areas", "ask_sistemas_lista",
                         "ask_processo", "ask_usuarios_faixa", "ask_pagamento", "multi_hint")

_PLACEHOLDERS: dict[str, set[str]] = {
    "voice.tone": set(),
    "reply.welcome": set(),
    "reply.welcome_back": {"nome"},
    "reply.hello_ongoing": set(),
    "reply.hello_resume": {"retomada"},
    "reply.smalltalk_fallback": set(),
    "reply.stay_with_ai": set(),
    "reply.info_topics": set(),
    "reply.button_info": set(),
    "reply.button_start_proposal": set(),
    "reply.panel_link": {"link"},
    "scheduling.upcoming_one": {"quando"},
    "scheduling.upcoming_many": {"lista"},
    "scheduling.none_upcoming": set(),
    "scheduling.meeting_link": {"link"},
    "scheduling.which_to_move": {"lista"},
    "scheduling.which_to_cancel": {"lista"},
    "reply.not_understood": set(),
    "reply.out_of_hours": set(),
    "reply.escalation": set(),
    "reply.escalation_failed": set(),
    "reply.unknown": set(),
    "scheduling.period_question": set(),
    "scheduling.period_morning": set(),
    "scheduling.period_afternoon": set(),
    "scheduling.period_any": set(),
    "scheduling.offer": {"slots"},
    "scheduling.confirmation": {"data", "dia_semana", "horario", "endereco"},
    "scheduling.reminder": {"nome_paciente", "data", "dia_semana", "horario"},
    "scheduling.reminder_email_subject": {"nome_paciente", "data", "dia_semana", "horario"},
    "scheduling.reminder_email": {"nome_paciente", "data", "dia_semana", "horario", "reuniao"},
    "scheduling.reminder_confirm": set(),
    "scheduling.reminder_reschedule": set(),
    "scheduling.reminder_cancel": set(),
    "scheduling.attendance_confirmed": {"data", "dia_semana", "horario"},
    "scheduling.cancel_check": {"data", "dia_semana", "horario"},
    "scheduling.cancel_yes": set(),
    "scheduling.cancel_no": set(),
    "scheduling.kept": set(),
    "scheduling.reminder_stale": set(),
    "scheduling.cancellation": {"data", "horario"},
    "scheduling.no_slots": set(),
    "scheduling.which_day": set(),
    "scheduling.which_day_again": set(),
    "scheduling.day_past": set(),
    "scheduling.day_unavailable": {"dia_semana", "data"},
    "scheduling.offer_dropped": set(),
    "scheduling.slot_taken": set(),
    "scheduling.nothing_to_cancel": set(),
    "scheduling.could_not_book": set(),
    "contact.ask_before_booking": {"faltando"},
    "contact.ask_before_handoff": {"faltando"},
    "contact.ask_before_proposal": {"faltando"},
    "contact.need_both": set(),
    "contact.need_email": set(),
    "contact.need_phone": set(),
    "contact.noted": set(),
    "contact.ask_email": set(),
    "contact.ask_phone": set(),
    "contact.thanks": set(),
    "handoff.button": set(),
    "teste.started_new": set(),
    "teste.started_returning": set(),
    "teste.ended": set(),
    "teste.expired": set(),
    "teste.not_in_test": set(),
    "teste.cancel_failed": {"quando"},
    "booking.days_prompt": set(),
    "booking.flow_intro": set(),
    "booking.flow_cta": set(),
    "booking.flow_fallback": set(),
    "booking.flow_bad_email": set(),
    "booking.days_button": set(),
    "booking.days_count": {"n"},
    "booking.days_one": set(),
    "booking.days_only_morning": set(),
    "booking.days_only_afternoon": set(),
    "booking.days_next": set(),
    "booking.days_which": set(),
    "booking.days_tap": set(),
    "booking.times_prompt": {"dia_semana", "data"},
    "booking.times_button": set(),
    "booking.times_more": set(),
    "booking.times_other_day": set(),
    "booking.day_full": set(),
    "booking.limit_one": {"quando"},
    "booking.ack": {"quando"},
    "booking.limit_many": {"n", "lista"},
    "booking.limit_reschedule": set(),
    "booking.limit_keep": set(),
    "booking.limit_kept": set(),
    "booking.stale_reschedule": set(),
    "booking.team_cancel_failed": {"quando", "cliente", "quando_nova"},
    **{f"proposal.{k}": set() for k in (
        "offer", "button_meet", "button_email", "button_skip", "button_stop", "button_team",
        "button_ok", "button_fix", "fix_which", "prefilled_mark", "done", "stopped", "waiting_email",
        "ask_quem", "ask_desafio", "ask_custo_hoje", "ask_canais", "ask_ferramentas",
        "ask_usuarios", "ask_prazo", "label_quem", "label_desafio", "label_custo_hoje",
        "label_canais", "label_ferramentas", "label_usuarios", "label_prazo",
        "ask_areas", "area_agenda", "area_triagem", "area_prontuario", "area_receitas",
        "area_pagamentos", "area_financeiro", "button_areas_done", "ask_como_hoje",
        "ask_sistemas", "ask_equipe", "clinic_line", "flow_gargalos", "flow_pedido",
        "label_areas", "label_como_hoje",
        "label_sistemas", "label_equipe")},
    **{f"proposal.{k}": set() for k in (*_PROPOSAL_BUTTON_ASKS, *_PROPOSAL_OPTIONS)},
    "proposal.horas_semana": {"horas"},
    "forms.sent_label": set(),
    "proposal.flow_intro": set(),
    "contract.fix": {"campos"},
    "contract.team_received": {"nome"},
    "contract.team_no_flow": {"nome"},
    **{f"contract.{k}": set() for k in (
        "flow_intro", "flow_cta", "received", "not_open", "fallback", "label_razao_social",
        "label_documento", "label_endereco", "label_assinante",
        *(f"campo_{key}" for key in ('razao_social', 'documento', 'cep', 'endereco', 'bairro', 'cidade', 'uf', 'assinante_nome', 'assinante_cpf', 'assinante_email', 'assinante_cargo')))},
    "contract_sign.text": {"email", "link", "nome"},
    **{f"contract_sign.{k}": set() for k in (
        "email_message", "sent", "sent_email", "already", "failed", "uncertain", "off",
        "released", "not_stale", "duplicate")},
    "proposal_info.text": {"nome"},
    "proposal_info.history": {"texto"},
    **{f"proposal_info.{k}": set() for k in ("sent", "no_template", "channel", "failed", "button_team", "button_meet")},
    **{f"proposal_pdf.{k}": set() for k in (
        "title", "subtitle", "label_para", "label_cuidados", "label_emitida", "label_validade",
        "sec_contou", "sec_entrega", "sec_investimento", "sec_passos", "escopo_canais",
        "escopo_volume", "escopo_integracoes", "escopo_usuarios", "escopo_funcoes",
        "escopo_dados", "nenhuma_integracao", "dados_clinica", "dados_empresa", "inclui",
        "implantacao_label", "mensalidade_label", "mensalidade_nota", "fidelidade_label",
        "passo_1", "passo_2", "passo_3", "passo_4", "passo_5", "rodape", "rodape_nota",
        "sem_orcamento", "vista")},
    "proposal_pdf.validade_dias": {"dias"},
    "proposal_pdf.contou_custo": {"texto"},
    "proposal_pdf.contou_equipe": {"texto"},
    "proposal_pdf.contou_areas": {"lista"},
    "proposal_pdf.contou_ferramentas": {"lista"},
    "proposal_pdf.volume": {"n"},
    "proposal_pdf.usuarios": {"n"},
    "proposal_pdf.parcelas": {"n", "primeira", "resto", "valor"},
    "proposal_pdf.metade": {"primeira", "segunda"},
    "proposal_pdf.parcelas_iguais": {"n", "valor"},
    "proposal_pdf.pagamento": {"forma"},
    "proposal_pdf.fidelidade_nota": {"pct"},
    "proposal_pdf.extras": {"conversa", "usuario"},
    "proposal_pdf.arquivo": {"empresa"},
    **{f"contract_pdf.{k}": set() for k in (
        "titulo", "selo", "sec_partes", "contratada", "assinatura_converge", "rotulo_converge",
        "rotulo_contratante", "anexo_i", "ficha", "funcoes_titulo", "nao_incluido",
        "dados_titulo", "anexo_iii", "f_funcoes", "f_canais", "f_integracoes", "f_documentos",
        "f_volume", "f_usuarios", "f_sensiveis", "f_ajustes", "f_prazo", "f_suporte",
        "nenhuma_integracao", "documentos_nenhum", "documentos_pequeno", "documentos_grande",
        "sim", "nao", "ajustes", "prazo", "suporte", "ver_funcoes", "d_finalidades",
        "d_categorias", "d_responsavel", "d_retencao", "d_terceiros", "d_transferencia",
        "categorias_clinica", "categorias_empresa", "responsavel_clinica", "retencao",
        "terceiros_base", "terceiros_meta", "terceiros_google", "terceiros_microsoft",
        "terceiros_asaas", "transferencia", "c_implantacao", "c_mensalidade", "c_volume",
        "c_excedente", "c_usuario", "c_sla", "c_fidelidade", "c_tarifas", "c_validade",
        "vencimento_dia", "excedente_limite", "tarifas", "rodape", "sem_dados")},
    "contract_pdf.contratante": {"assinante", "cpf", "documento", "email", "endereco", "nome", "tipo"},
    "contract_pdf.local_data": {"data"},
    "contract_pdf.assinatura_contratante": {"assinante", "nome"},
    "contract_pdf.aceite": {"lista"},
    "contract_pdf.volume": {"n"},
    "contract_pdf.responsavel": {"contato"},
    "contract_pdf.implantacao_parcelas": {"valor", "n", "primeira", "resto", "parcela"},
    "contract_pdf.implantacao_metade": {"valor", "primeira", "segunda"},
    "contract_pdf.implantacao_vista": {"valor"},
    "contract_pdf.mensalidade": {"dia", "valor"},
    "contract_pdf.excedente": {"limite", "valor"},
    "contract_pdf.usuario": {"valor"},
    "contract_pdf.sla_sim": {"valor"},
    "contract_pdf.sla_nao": {"valor"},
    "contract_pdf.fidelidade": {"pct"},
    "contract_pdf.validade": {"dias"},
    "contract_pdf.terceiros_outro": {"nome"},
    "contract_pdf.pagina": {"pagina"},
    "contract_pdf.arquivo": {"empresa"},
    **{f"proposal.{k}": set() for k in (
        "ask_ramo", "ramo_button", "ramo_saude", "ramo_saude_desc", "ramo_juridico",
        "ramo_construcao", "ramo_comercio", "ramo_servicos", "ramo_outro", "ramo_outro_desc",
        "ask_ramo_typed", "ask_quem_sem_ramo")},
    "proposal.flow_cta": set(),
    "proposal.chosen": {"lista"},
    "proposal.areas_chosen": {"lista"},
    "proposal.ask_como_hoje_areas": {"areas"},
    "proposal.form_volume": {"volume"},
    "proposal.confirm": {"resumo"},
    "proposal.resume": {"pergunta"},
    "proposal.team_sent": {"nome", "empresa"},
    "proposal.team_incomplete": {"nome", "empresa"},
    "contract_sign.team_signed": {"nome", "empresa"},
    "teste.team_prefix": set(),
    "handoff.relay": {"atendente", "mensagem"},
    "handoff.closed_notice": {"nome"},
    "handoff.back_to_bot": set(),
    "handoff.timeout_customer": set(),
    "handoff.timeout_claimed": {"nome", "atendente"},
    "handoff.timeout_open": {"nome"},
    "billing.charge_sent": {"descricao", "valor", "vencimento", "link"},
    "billing.charge_sent_fixed": {"descricao", "valor", "vencimento", "link"},
    "billing.paid_customer": {"descricao", "valor"},
    "billing.paid_team": {"nome", "descricao", "valor"},
    "lead.first_contact_text": {"nome"},
    "lead.first_contact_general_text": {"nome"},
    "lead.yes": set(),
    "lead.no": set(),
    "lead.no_reply": set(),
    "lead.added": set(),
    "lead.sent": set(),
    "lead.no_template": set(),
    "lead.send_failed": set(),
    "lead.exists": set(),
    "proposal_doc.text": {"nome", "empresa"},
    "proposal_doc.team_accept": {"nome"},
    "proposal_doc.team_decline": {"nome"},
    "proposal_doc.history": {"arquivo", "texto"},
    **{f"proposal_doc.{k}": set() for k in (
        "no_company", "button_accept", "button_doubt", "button_decline", "doubt",
        "accept_reply", "decline_reply", "sent", "failed", "no_template", "channel", "uncertain")},
    "form.hello": {"nome"},
    "form.hello_anon": set(),
    "form.challenge_bookings": set(),
    "form.challenge_organization": set(),
    "form.challenge_slow": set(),
    "form.challenge_other": set(),
    "form.next": set(),
    "form.how_it_works": set(),
    "form.button_how": set(),
    "form.legal_pitch": set(),
    "form.legal_next": set(),
    "form.legal_how_it_works": set(),
    "form.construction_pitch": set(),
    "form.construction_next": set(),
    "form.construction_how_it_works": set(),
    "form.general": set(),
    "inbox.reopen_button": set(),
    "inbox.reopen_notice": set(),
    "inbox.reopen_text": {"nome"},
    "inbox.reopen_sent": set(),
    "inbox.reopen_tap": set(),
    "billing.charge_doubt": set(),
    "inbox.open_button": set(),
    "inbox.new_conversation": {"nome", "canal"},
    "inbox.needs_person": {"nome", "canal", "motivo"},
    "inbox.reason_pedido": set(),
    "inbox.reason_botao": set(),
    "inbox.claimed": {"nome", "atendente"},
    "inbox.reassigned": {"nome", "atendente", "anterior"},
    "inbox.released": {"nome", "atendente"},
    "inbox.owner_ping": {"nome"},
    "inbox.unreadable": set(),
    "media.line_image": set(),
    "media.line_document": set(),
    "media.line_video": set(),
    "media.line_audio": set(),
    "media.line_sticker": set(),
    "media.received": set(),
    "media.not_received": set(),
    "media.kind_image": set(),
    "media.kind_document": set(),
    "media.kind_video": set(),
    "media.kind_audio": set(),
    "media.kind_sticker": set(),
    "media.expired": set(),
    "media.too_large": set(),
    "media.failed": set(),
    "media.no_space": set(),
    "media.rate_limited": set(),
    "media.late": set(),
    "media.missing": set(),
    "inbox.reason_media": {"tipo"},
    "inbox.send_failed": {"canal"},
    "attendance.booked_notice": {"nome", "data", "horario"},
    "attendance.rescheduled_notice": {"nome", "data", "horario"},
    "attendance.confirmed_notice": {"nome", "data", "horario"},
    "attendance.cancelled_notice": {"nome", "data", "horario"},
    "attendance.outcome_question": {"nome", "data", "horario"},
    "attendance.outcome_done": set(),
    "attendance.outcome_no_show": set(),
    "attendance.recorded_done": {"nome", "atendente"},
    "attendance.recorded_no_show": {"nome", "atendente"},
    "briefing.header": {"saudacao", "dia_semana", "data"},
    "briefing.good_morning": set(),
    "briefing.good_afternoon": set(),
    "briefing.good_evening": set(),
    "briefing.today": set(),
    "briefing.today_empty": set(),
    "briefing.confirmed": set(),
    "briefing.unconfirmed": set(),
    "briefing.new_contacts": {"total", "mensagens"},
    "briefing.no_new_contacts": {"mensagens"},
    "briefing.handoffs": set(),
    "briefing.unclaimed": set(),
    "briefing.claimed": {"atendente"},
    "briefing.unrecorded": {"total"},
    "briefing.stale_proposals": {"dias"},
    "briefing.followups": {"dias"},
    "panel.invite": {"nome", "link", "horas"},
    "automation.resume_whatsapp_text": {"nome"},
    "automation.resume_yes": set(),
    "automation.resume_no": set(),
    "automation.resume_no_reply": set(),
    "automation.opt_out_footer": set(),
    "automation.opted_out": set(),
    "automation.reason_preco": set(),
    "automation.reason_avaliando": set(),
    "automation.reason_momento": set(),
    "automation.reason_outro": set(),
    "automation.reason_thanks": set(),
    "automation.rebook": set(),
    "automation.awaiting_approval": {"total"},
    "automation.reason_notice": {"nome", "motivo"},
    "automation.no_proposal_notice": {"nome", "data", "dias"},
}


class ContentError(RuntimeError):
    """The content files are missing something the code needs."""


def _root() -> Path:
    path = Path(settings.content_dir)
    return path if path.is_absolute() else Path(__file__).resolve().parent.parent / path


@lru_cache(maxsize=1)
def messages() -> dict:
    path = _root() / "messages.toml"
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ContentError(f"No message file at {path}.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ContentError(f"{path} is not valid TOML: {exc}") from exc


@lru_cache(maxsize=1)
def email_categories() -> list[dict]:
    """The organization's own inbox categories, for triage to classify into."""
    path = _root() / "email_templates.toml"
    if not path.exists():
        return []
    with path.open("rb") as handle:
        return tomllib.load(handle).get("category", [])


@lru_cache(maxsize=1)
def staff() -> list[dict]:
    path = _root() / "staff.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _lookup(path: str) -> str:
    section, _, key = path.partition(".")
    try:
        value = messages()[section][key]
    except KeyError as exc:
        raise ContentError(f"messages.toml has no {path}.") from exc
    if not isinstance(value, str):
        raise ContentError(f"{path} in messages.toml is not text.")
    return value.strip()


def text(path: str) -> str:
    """One message, verbatim. For the ones that take no placeholders."""
    return _lookup(path)


def render(path: str, **values) -> str:
    """One message with its placeholders filled.

    A missing value is a programming error rather than something to paper over:
    rendering "{horario}" into a confirmation is worse than raising here, where
    the tests and the startup check will both catch it.
    """
    body = _lookup(path)
    try:
        return body.format(**values)
    except KeyError as exc:
        raise ContentError(f"{path} needs a value for {exc}.") from exc


def voice_rules() -> str:
    """The tone, how to lead the conversation, and the prohibitions, as the
    answering prompt reads them."""
    block = messages().get("voice", {})
    tone = str(block.get("tone", "")).strip()
    never = block.get("never", [])
    flow = block.get("flow", [])
    if not isinstance(never, list) or not isinstance(flow, list):
        raise ContentError("voice.never and voice.flow in messages.toml must be lists.")
    parts = [tone]
    if flow:
        parts.append("Como conduzir a conversa:\n" + "\n".join(f"- {rule}" for rule in flow))
    if never:
        parts.append("Regras absolutas:\n" + "\n".join(f"- {rule}" for rule in never))
    return "\n\n".join(parts)


def fact(key: str) -> str:
    value = messages().get("facts", {}).get(key)
    if not isinstance(value, str):
        raise ContentError(f"messages.toml has no fact '{key}'.")
    return value.strip()


def validate() -> None:
    """Refuse a deployment whose messages the code cannot fill.

    Called from the startup check, next to the other things this service would
    rather not discover in production.
    """
    problems: list[str] = []
    for path, allowed in _PLACEHOLDERS.items():
        try:
            body = _lookup(path)
        except ContentError as exc:
            problems.append(str(exc))
            continue
        used = {
            name for _, name, _, _ in string.Formatter().parse(body) if name
        }
        unknown = used - allowed
        if unknown:
            problems.append(
                f"{path} uses {sorted(unknown)}, which nothing supplies. "
                f"Available: {sorted(allowed) or 'none'}."
            )
    problems.extend(_whatsapp_limits())
    problems.extend(_proposal_notices())
    from core.services import contract_pdf, forms, pricing  # here: they read settings too

    problems.extend(pricing.problems())
    problems.extend(forms.problems())
    problems.extend(contract_pdf.problems())
    if not messages().get("voice", {}).get("never"):
        problems.append(
            "voice.never is empty. This desk talks to patients; the list of what "
            "it must never say is not optional."
        )
    if problems:
        raise ContentError("Content is not usable:\n  - " + "\n  - ".join(problems))
    logger.info("Content validated: %d messages, %d e-mail categories, %d staff.",
                len(_PLACEHOLDERS), len(email_categories()), len(staff()))


def reset() -> None:
    """Drop the caches, so a test can point at a different content directory."""
    messages.cache_clear()
    email_categories.cache_clear()
    staff.cache_clear()
    from core.services import contract_pdf, forms, pricing

    pricing.reset()
    forms.reset()
    contract_pdf.reset()


# WhatsApp's limits on what a menu shows, as rendered: a list button, a row's
# title, the line under it. Counted with the largest {n} a day can have.
_WHATSAPP_LIMITS = {
    "booking.days_button": 20, "booking.times_button": 20,
    "booking.limit_reschedule": 20, "booking.limit_keep": 20,
    "booking.days_next": 24, "booking.times_more": 24, "booking.times_other_day": 24,
    "booking.days_count": 72, "booking.days_one": 72,
    "booking.days_only_morning": 72, "booking.days_only_afternoon": 72,
    "reply.button_info": 20, "reply.button_start_proposal": 20,
    "proposal.flow_cta": 30,
    "contract.flow_cta": 30,
    "booking.flow_cta": 30,
    **{f"proposal.button_{k}": 20 for k in (
        "meet", "email", "skip", "stop", "team", "ok", "fix", "areas_done")},
    **{f"proposal_doc.button_{k}": 20 for k in ("accept", "doubt", "decline")},
    **{f"proposal.label_{k}": 24 for k in (
        "quem", "desafio", "custo_hoje", "canais", "ferramentas", "usuarios", "prazo",
        "areas", "como_hoje", "sistemas", "equipe")},
    **{f"proposal.area_{k}": 24 for k in (
        "agenda", "triagem", "prontuario", "receitas", "pagamentos", "financeiro")},
    **{f"proposal.{k}": 24 for k in _PROPOSAL_OPTIONS},
    "proposal.ramo_button": 20,
    "proposal_info.button_team": 20,
    "proposal_info.button_meet": 20,
    **{f"proposal.ramo_{k}": 24 for k in (
        "saude", "juridico", "construcao", "comercio", "servicos", "outro")},
    **{f"proposal.ramo_{k}_desc": 72 for k in ("saude", "outro")},
}


def _proposal_notices() -> list[str]:
    """The team's proposal notices drop " · {empresa}" whole when the company
    is unknown (core/services/proposal_briefing.py), so it must be there as is."""
    problems = []
    for path in ("proposal.team_sent", "proposal.team_incomplete", "contract_sign.team_signed"):
        try:
            body = _lookup(path)
        except ContentError:
            continue  # reported by the placeholder check above
        if " · {empresa}" not in body:
            problems.append(f'{path} must write the company as " · {{empresa}}".')
    return problems


def _whatsapp_limits() -> list[str]:
    problems = []
    for path, limit in _WHATSAPP_LIMITS.items():
        try:
            rendered = _lookup(path).format(n=99)
        except (ContentError, KeyError, IndexError, ValueError):
            continue  # reported by the placeholder check above
        if len(rendered) > limit:
            problems.append(f"{path} has {len(rendered)} characters; WhatsApp shows at most {limit}.")
    return problems
