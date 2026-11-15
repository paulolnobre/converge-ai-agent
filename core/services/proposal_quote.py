"""The preliminary quote of a proposal request, for the team only.

The questionnaire's button answers are stored as the labels the person saw
(", " between several), like the clinic's areas. Here they are read back into
the table's inputs (`pricing.Answers`) by the current labels in
`content/messages.toml`; the scope and the price are `pricing.derive` and
`pricing.quote`. The model never prices anything, and nothing here reaches the
customer.
"""
from __future__ import annotations

from dataclasses import dataclass

from core import content
from core.services import pricing

CANAIS = ("whatsapp", "instagram", "site", "telefone", "email")
BIZ_AREAS = pricing.AREAS
SISTEMAS = ("google_agenda", "planilha", "crm", "gestao", "nenhum")
VOLUMES = ("ate_500", "ate_2000", "ate_5000", "mais_5000")
PROCESSOS = ("sim", "parcial", "nao")
USUARIOS = {"1_2": 2, "3_5": 5, "6_10": 10, "mais_10": 15}
CLINIC_TO_PRICING = {
    "agenda": "atendimento", "triagem": "atendimento",
    "pagamentos": "financeiro", "financeiro": "financeiro",
    "prontuario": "documentos_juridico", "receitas": "documentos_juridico",
}
_CLINIC_DOCUMENTS = ("prontuario", "receitas")
# The name each system button is priced by: a known alias is a simple
# integration; "CRM" or "Sistema de gestão" without a name is a hard one.
_SYSTEM_NAMES = {"google_agenda": "google agenda", "planilha": "planilha google",
                 "crm": "crm", "gestao": "sistema de gestao"}
SEP = ", "


@dataclass(frozen=True)
class Computed:
    answers: pricing.Answers
    scope: pricing.Scope
    quote: pricing.Quote
    premissas: tuple[str, ...]


@dataclass(frozen=True)
class Missing:
    faltam: tuple[str, ...]


def _items(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(SEP) if item.strip()]


def _by_label(group: str, keys) -> dict[str, str]:
    return {content.text(f"proposal.{group}_{key}"): key for key in keys}


def _one(answers: dict[str, str], step: str, group: str, keys) -> str | None:
    return _by_label(group, keys).get((answers.get(step) or "").strip())


# An area typed by hand that the table cannot price: kept as a premise, so the
# team reads it and the proposal PDF never promises it at the quoted price.
UNPRICED_AREA = "área não calculada: "


def answers_for(answers: dict[str, str], clinic: bool) -> tuple[pricing.Answers, tuple[str, ...]]:
    """The table's inputs, and the areas typed by hand that the table cannot price."""
    channels = tuple(_items(answers.get("canais_lista"))) or None

    areas: tuple[str, ...] | None = None
    typed: list[str] = []
    documents: str | None = None
    if _items(answers.get("areas")):
        labels = (_by_label("area", CLINIC_TO_PRICING) if clinic
                  else _by_label("biz_area", BIZ_AREAS))
        picked: list[str] = []
        for item in _items(answers.get("areas")):
            key = labels.get(item)
            if key is None:
                typed.append(item)
                continue
            if clinic and key in _CLINIC_DOCUMENTS:
                documents = "pequeno"
            priced = CLINIC_TO_PRICING[key] if clinic else key
            if priced not in picked:
                picked.append(priced)
        areas = tuple(picked)

    systems: tuple[pricing.System, ...] | None = None
    if _items(answers.get("sistemas_lista")):
        labels = _by_label("sistema", SISTEMAS)
        found: list[pricing.System] = []
        for item in _items(answers.get("sistemas_lista")):
            key = labels.get(item)
            if key == "nenhum":
                continue
            found.append(pricing.System(_SYSTEM_NAMES[key] if key else item))
        systems = tuple(found)

    process = _one(answers, "processo", "processo", PROCESSOS)
    users = _one(answers, "usuarios_faixa", "usuarios", USUARIOS)
    return pricing.Answers(
        canais=channels,
        sistemas=systems,
        areas=areas,
        volume_faixa=_one(answers, "volume", "volume", VOLUMES),
        roteiro_definido=None if process is None else process == "sim",
        documentos=documents,
        dado_sensivel=True if clinic else None,
        usuarios=USUARIOS[users] if users else None,
    ), tuple(typed)


def compute(answers: dict[str, str], clinic: bool) -> Computed | Missing | None:
    """The quote; what is still missing for one; None without a price table."""
    table = pricing.load_table()
    if table is None:
        return None
    inputs, typed = answers_for(answers, clinic)
    gaps = pricing.missing(inputs)
    if gaps:
        return Missing(faltam=tuple(gaps))
    scope = pricing.derive(inputs, table)
    premissas = scope.premissas + tuple(f"{UNPRICED_AREA}{item}" for item in typed)
    return Computed(answers=inputs, scope=scope, quote=pricing.quote(scope, table),
                    premissas=premissas)
