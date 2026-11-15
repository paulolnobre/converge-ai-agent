"""The message Meta's lead form sends for the person: read by code, answered by code.

The form already says who they are and what they need. Asking "quer conhecer
nossas soluções?" after that wastes the first turn, and the model, given a list
of products, once offered a construction tool to a clinic. So the first reply
is fixed text, built from what they told us, with the ways forward as buttons.

Which product they came for is read from the words they used: a clinic, a law
office, a construction company. Unclear, or more than one, gets a plain hello
that only says the details were noted.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from core import content

_OPENING = "preenchi seu formulario"
_LINE = re.compile(r"^\s*([^:\n]{3,80}?)\s*:\s*(.+?)\s*$", re.MULTILINE)
# The challenge answers the form offers, by the word that tells them apart.
_CHALLENGES = (
    ("agendamento", "form.challenge_bookings"),
    ("organizacao", "form.challenge_organization"),
    ("demora", "form.challenge_slow"),
)
# What tells the products apart, matched on folded text (no accents, lower).
_CLINIC = re.compile(
    r"clinica|consultorio|paciente|odonto|dentist|medic|saude|estetica|fisioterap"
    r"|psicolog|nutricion|hospital|laboratorio")
_LEGAL = re.compile(r"advog|advocacia|juridic|\boab\b|\bdireito\b|tribuna|peticao|jurisprud")
_CONSTRUCTION = re.compile(
    r"constru|engenh|\bobras?\b|empreiteir|incorporador|canteiro|mestre de obra")
# "Escritório" alone is a law office only when nothing else said otherwise.
_OFFICE = re.compile(r"escritorio")
PRODUCTS = ("clinic", "legal", "construction", "general")


@dataclass(frozen=True)
class FormLead:
    name: str | None
    challenge: str  # a form.challenge_* key
    # For the proposal questionnaire: asked again would be asked twice.
    clinic: str | None = None
    role: str | None = None
    volume: str | None = None
    # Who they came for, by what they wrote: one of PRODUCTS.
    product: str = "clinic"

    @property
    def for_clinic(self) -> bool:
        return self.product == "clinic"


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def product_of(text: str) -> str:
    """The product the words point to, or "general" when none or several do."""
    folded = _fold(text)
    found = [name for name, words in (("clinic", _CLINIC), ("legal", _LEGAL),
                                      ("construction", _CONSTRUCTION))
             if words.search(folded)]
    if not found and _OFFICE.search(folded):
        return "legal"
    return found[0] if len(found) == 1 else "general"


def read(body: str) -> FormLead | None:
    """The form's answers, or None if this is not the form's message."""
    if _OPENING not in _fold(body):
        return None
    answers = {_fold(label): value for label, value in _LINE.findall(body)}
    if not answers:
        return None

    def answer(*words: str) -> str | None:
        for label, value in answers.items():
            if any(word in label for word in words):
                return value
        return None

    said = _fold(answer("desafio") or "")
    challenge = next((key for word, key in _CHALLENGES if word in said), "form.challenge_other")
    return FormLead(name=answer("full name", "nome completo"), challenge=challenge,
                    clinic=answer("nome da clinica"), role=answer("sua funcao"),
                    volume=answer("quantas mensagens"),
                    product=product_of(body))


def opening(lead: FormLead, fallback_name: str | None) -> str:
    """Hello by first name, the challenge tied to what the desk does, and the
    choice. The form's name first: the WhatsApp profile name is often a nickname."""
    words = (lead.name or fallback_name or "").split()
    hello = (content.render("form.hello", nome=words[0].capitalize()) if words
             else content.text("form.hello_anon"))
    if lead.product == "legal":
        return f"{hello} {content.text('form.legal_pitch')}\n\n{content.text('form.legal_next')}"
    if lead.product == "construction":
        return (f"{hello} {content.text('form.construction_pitch')}"
                f"\n\n{content.text('form.construction_next')}")
    if lead.product == "general":
        return f"{hello} {content.text('form.general')}"
    return f"{hello} {content.text(lead.challenge)}\n\n{content.text('form.next')}"
