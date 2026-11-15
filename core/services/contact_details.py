"""E-mail and phone, read out of what a person writes — by pattern, not by model.

Asked for once, when a booking makes them useful, and never asked again. A
reply like "paulo@x.com, 91 90000-0001" must be read exactly, and a model has
no business rounding a phone number; so this is arithmetic over the text, and
anything that does not look like a real e-mail or a Brazilian phone is left
alone rather than guessed at.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Digits with the separators people type in a phone number — spaces, hyphens,
# parentheses, a leading +. No dots and no slashes: those are CPFs, dates and
# prices ("123.456.789-00", "24/09", "R$ 749,90"), never phones here.
_PHONE = re.compile(r"\+?\(?\d[\d\s()\-]{7,18}\d")
# Words that only frame the data ("meu e-mail é …"), so a reply made of them
# and the data is recognisably just the answer to the question.
_FILLER = {
    "meu", "minha", "e", "é", "o", "a", "os", "as", "de", "do", "da", "segue",
    "aqui", "ok", "email", "e-mail", "mail", "telefone", "tel", "fone",
    "celular", "cel", "fixo", "whats", "whatsapp", "zap", "numero", "número", "contato",
    "com", "ddd", "pode", "ser", "esse", "este", "isso", "sim", "claro",
    "obrigado", "obrigada", "valeu", "certo", "beleza", "blz",
}


@dataclass(frozen=True)
class Details:
    email: str | None = None
    phone: str | None = None
    # Whether the message was nothing but the details (and words framing them).
    only_details: bool = False

    @property
    def any(self) -> bool:
        return bool(self.email or self.phone)


def _normalise_phone(raw: str) -> str | None:
    """+55 DD NNNNN-NNNN, or None if these digits are not a Brazilian phone."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return None
    ddd, number = digits[:2], digits[2:]
    # DDDs run 11–99 with no zero in either place.
    if "0" in ddd:
        return None
    # A mobile has nine digits starting with 9; a landline eight starting 2–5.
    if len(number) == 9 and number[0] != "9":
        return None
    if len(number) == 8 and number[0] not in "2345":
        return None
    return f"+55 {ddd} {number[:-4]}-{number[-4:]}"


def extract(text: str) -> Details:
    email_match = _EMAIL.search(text)
    email = email_match.group(0).lower() if email_match else None
    rest = _EMAIL.sub(" ", text)

    phone = None
    for match in _PHONE.finditer(rest):
        phone = _normalise_phone(match.group(0))
        if phone:
            rest = rest.replace(match.group(0), " ")
            break

    words = [
        word for word in re.findall(r"[\wà-ú-]+", _fold(rest)) if word not in _FILLER
    ]
    return Details(email=email, phone=phone, only_details=bool(email or phone) and not words)


def _fold(text: str) -> str:
    """Lower-case, and 'é' kept as a word — "meu e-mail é x" is all filler."""
    return unicodedata.normalize("NFC", text.lower())


def missing(email: str | None, phone: str | None) -> tuple[str, ...]:
    return tuple(name for name, value in (("email", email), ("phone", phone)) if not value)
