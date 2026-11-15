"""CPF and CNPJ: accepted with or without the mask, refused unless the check
digits hold. Only ASCII digits count — "５" is a digit to Python, not to Asaas."""
from __future__ import annotations

import re

_MASK = re.compile(r"[.\-/\s]")
_CNPJ_WEIGHTS = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def _cpf_ok(digits: list[int]) -> bool:
    for size in (9, 10):
        total = sum(d * (size + 1 - i) for i, d in enumerate(digits[:size]))
        if (total * 10) % 11 % 10 != digits[size]:
            return False
    return True


def _cnpj_ok(digits: list[int]) -> bool:
    for size in (12, 13):
        weights = _CNPJ_WEIGHTS[13 - size:]
        rest = sum(d * w for d, w in zip(digits[:size], weights)) % 11
        if (0 if rest < 2 else 11 - rest) != digits[size]:
            return False
    return True


def normalize_tax_id(text: str) -> str | None:
    """Digits only, if `text` is a valid CPF (11) or CNPJ (14); else None."""
    raw = _MASK.sub("", text or "")
    if not raw or not all("0" <= c <= "9" for c in raw) or len(set(raw)) == 1:
        return None
    digits = [int(c) for c in raw]
    if len(raw) == 11 and _cpf_ok(digits):
        return raw
    if len(raw) == 14 and _cnpj_ok(digits):
        return raw
    return None
