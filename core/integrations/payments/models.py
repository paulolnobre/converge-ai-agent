"""What the app says to and hears from a payment provider, in its own terms.

Money is always an int of cents here; reais exist only at the provider's edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

# "link": the provider's invoice page, where the payer picks boleto, Pix or card.
# "cartao": credit card only (the setup, with the card fee added).
Method = Literal["link", "boleto", "pix", "cartao"]
Status = Literal["pendente", "pago", "vencido", "cancelado", "estornado"]


@dataclass(frozen=True)
class NewCustomer:
    name: str
    tax_id: str          # CPF/CNPJ, digits only (tax_id.normalize_tax_id)
    email: str | None
    phone: str | None
    external_ref: str    # our id for this payer, e.g. "contact:7"


@dataclass(frozen=True)
class Customer:
    provider_id: str
    external_ref: str | None


@dataclass(frozen=True)
class NewCharge:
    customer_id: str     # Customer.provider_id
    method: Method
    value_cents: int
    due_date: date
    description: str
    external_ref: str


@dataclass(frozen=True)
class Charge:
    provider_id: str
    status: Status
    value_cents: int
    due_date: date
    invoice_url: str | None


@dataclass(frozen=True)
class Account:
    email: str
    name: str
