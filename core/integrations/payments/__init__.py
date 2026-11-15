"""One façade over the payment provider.

Call `payments()`; which provider answers is a configuration question. Today
only the Converge's own Asaas account exists — a clinic's account or another
gateway is another PaymentsPort.
"""
from __future__ import annotations

from core.config import settings
from core.integrations.payments.asaas import AsaasPayments
from core.integrations.payments.asaas import close as _close_asaas
from core.integrations.payments.base import PaymentsPort
from core.integrations.payments.errors import (
    PaymentsAuthError,
    PaymentsError,
    PaymentsNotConfigured,
    PaymentsRejected,
    PaymentsTransient,
)
from core.integrations.payments.models import (
    Account,
    Charge,
    Customer,
    Method,
    NewCharge,
    NewCustomer,
    Status,
)
from core.integrations.payments.tax_id import normalize_tax_id

__all__ = [
    "Account",
    "Charge",
    "Customer",
    "Method",
    "NewCharge",
    "NewCustomer",
    "PaymentsAuthError",
    "PaymentsError",
    "PaymentsNotConfigured",
    "PaymentsPort",
    "PaymentsRejected",
    "PaymentsTransient",
    "Status",
    "close",
    "normalize_tax_id",
    "payments",
    "reset",
]

_instance: AsaasPayments | None = None


def payments() -> PaymentsPort:
    global _instance
    if not settings.payments_configured():
        raise PaymentsNotConfigured("ASAAS_API_KEY is empty: payments are off.")
    if _instance is None:
        _instance = AsaasPayments()
    return _instance


def reset() -> None:
    global _instance
    _instance = None


async def close() -> None:
    await _close_asaas()
