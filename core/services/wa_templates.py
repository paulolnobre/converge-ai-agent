"""The WhatsApp templates this agent knows, in one place.

Each one is named in `.env` once Meta approves it (docs/WHATSAPP.md). Some
belong to a flow and carry that flow's data (a charge, a proposal, a
reminder): only their own button sends them. The others ask only for the
person's first name, and the team may send them by hand past the 24h window,
with the same buttons and the same history line as the flow they come from.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.config import settings

DEFAULT_LANGUAGE = "pt_BR"


@dataclass(frozen=True)
class Template:
    key: str
    label: str
    setting: str                      # the Settings field holding the approved name
    purpose: str                      # where the agent uses it
    manual: bool = False              # the team may send it from the Inbox past 24h
    payloads: tuple[str, ...] = ()    # one per quick-reply button, in order
    history: str | None = None        # the messages.toml key that mirrors its body
    language_setting: str | None = None


CATALOG: tuple[Template, ...] = (
    Template("team", "Resposta da equipe", "whatsapp_team_template",
             "Inbox: reabrir a conversa para a equipe responder",
             manual=True, payloads=("reopen:team",), history="inbox.reopen_text",
             language_setting="whatsapp_team_language"),
    Template("resume", "Retomar conversa parada", "whatsapp_resume_template",
             "Automação \"Parou no meio\"",
             manual=True, payloads=("resume:yes:manual", "resume:no:manual"),
             history="automation.resume_whatsapp_text",
             language_setting="whatsapp_resume_language"),
    Template("lead_general", "Contato por interesse", "whatsapp_lead_general_template",
             "Novo lead (indicação, Instagram, site...)",
             manual=True, payloads=("lead:yes", "lead:no"),
             history="lead.first_contact_general_text"),
    Template("lead_email", "Contato por e-mail", "whatsapp_lead_template",
             "Novo lead que veio por e-mail",
             manual=True, payloads=("lead:yes", "lead:no"), history="lead.first_contact_text"),
    Template("reminder", "Lembrete de conversa", "whatsapp_reminder_template",
             "Lembrete antes da reunião, com confirmar, remarcar e cancelar"),
    Template("charge", "Cobrança enviada", "whatsapp_charge_template",
             "Cobrança criada ou reenviada no painel"),
    Template("paid", "Pagamento recebido", "whatsapp_paid_template",
             "Pagamento confirmado pelo Asaas", language_setting="whatsapp_paid_language"),
    Template("proposal", "Proposta em PDF", "whatsapp_proposal_template",
             "Aba Propostas: Enviar proposta (PDF)"),
)


def name(t: Template) -> str | None:
    return (getattr(settings, t.setting, None) or "").strip() or None


def language(t: Template) -> str:
    chosen = getattr(settings, t.language_setting, None) if t.language_setting else None
    return (chosen or "").strip() or DEFAULT_LANGUAGE


def ready(t: Template) -> bool:
    """Named on this server, and WhatsApp itself set up."""
    return name(t) is not None and settings.whatsapp_configured()


def manual_ready() -> list[Template]:
    """What the team can send by hand right now, in catalog order."""
    return [t for t in CATALOG if t.manual and ready(t)]


def manual(key: str | None) -> Template | None:
    """The template the team asked for, only if it may be sent by hand now."""
    return next((t for t in manual_ready() if t.key == key), None)


def overview() -> list[dict]:
    """Every template, for the Automações tab: what it is for and whether it is
    named on this server (Meta's own approval status is not read here)."""
    return [{"key": t.key, "label": t.label, "name": name(t), "purpose": t.purpose,
             "manual": t.manual, "active": ready(t)} for t in CATALOG]
