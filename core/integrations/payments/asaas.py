"""Asaas API v3 — the Converge's own account (docs/ASAAS.md).

One lazy HTTP client, like whatsapp.py. Every failure becomes a PaymentsError
that names the call, the status and Asaas's error codes; nothing here logs or
raises the key, the request body or Asaas's descriptions.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from core.config import settings
from core.integrations.payments.errors import (
    PaymentsAuthError,
    PaymentsError,
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

logger = logging.getLogger(__name__)

USER_AGENT = "converge-app"
_CENT = Decimal("0.01")

_BILLING_TYPES: dict[Method, str] = {"link": "UNDEFINED", "boleto": "BOLETO", "pix": "PIX",
                                     "cartao": "CREDIT_CARD"}
_STATUSES: dict[str, Status] = {
    "PENDING": "pendente",
    "RECEIVED": "pago",
    "CONFIRMED": "pago",
    "RECEIVED_IN_CASH": "pago",
    "OVERDUE": "vencido",
    "REFUNDED": "estornado",
    "REFUND_REQUESTED": "estornado",
    "REFUND_IN_PROGRESS": "estornado",
    "CHARGEBACK_REQUESTED": "estornado",
    "CHARGEBACK_DISPUTE": "estornado",
    "AWAITING_CHARGEBACK_REVERSAL": "estornado",
}

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def to_reais(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(_CENT))


def to_cents(value: float | int | str) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _codes(response: httpx.Response) -> list[str]:
    try:
        errors = response.json().get("errors") or []
        return [str(e["code"]) for e in errors if isinstance(e, dict) and e.get("code")]
    except (ValueError, AttributeError, TypeError):
        return []


def _status(payment: dict[str, Any]) -> Status:
    if payment.get("deleted"):
        return "cancelado"
    raw = str(payment.get("status") or "")
    status = _STATUSES.get(raw)
    if status is None:
        logger.warning("Asaas payment status not mapped: %s", raw[:40])
        return "pendente"
    return status


def _charge(payment: dict[str, Any]) -> Charge:
    return Charge(
        provider_id=str(payment["id"]),
        status=_status(payment),
        value_cents=to_cents(payment["value"]),
        due_date=date.fromisoformat(str(payment["dueDate"])),
        invoice_url=payment.get("invoiceUrl"),
    )


def _customer(body: dict[str, Any]) -> Customer:
    return Customer(provider_id=str(body["id"]), external_ref=body.get("externalReference"))


class AsaasPayments:
    async def _call(self, method: str, path: str, *, name: str,
                    json: dict | None = None, params: dict | None = None,
                    missing_ok: bool = False) -> dict[str, Any] | None:
        headers = {
            "access_token": settings.asaas_api_key,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        try:
            response = await _http().request(
                method, f"{settings.asaas_base_url}{path}",
                headers=headers, json=json, params=params)
        except httpx.HTTPError as exc:
            raise PaymentsTransient(f"Asaas {name}: {type(exc).__name__}") from None
        status = response.status_code
        if missing_ok and status == 404:
            return None
        if status in (401, 403):
            codes = _codes(response)
            code = codes[0] if codes else None
            raise PaymentsAuthError(f"Asaas {name}: {status} {code or ''}".strip(),
                                    status, code) from None
        if status == 429 or status >= 500:
            raise PaymentsTransient(f"Asaas {name}: {status}", status) from None
        if status >= 400:
            codes = _codes(response)
            raise PaymentsRejected(f"Asaas {name}: {status} {', '.join(codes)}".strip(),
                                   status, codes) from None
        try:
            return response.json()
        except ValueError:
            raise PaymentsError(f"Asaas {name}: unreadable answer", status) from None

    async def _must(self, method: str, path: str, *, name: str, **kwargs) -> dict[str, Any]:
        body = await self._call(method, path, name=name, **kwargs)
        assert body is not None  # only missing_ok calls answer None
        return body

    async def account(self) -> Account:
        body = await self._must("GET", "/myAccount", name="account")
        return Account(email=str(body.get("email") or ""), name=str(body.get("name") or ""))

    async def find_customer(self, external_ref: str) -> Customer | None:
        body = await self._must("GET", "/customers", name="find_customer",
                                params={"externalReference": external_ref, "limit": 1})
        found = body.get("data") or []
        return _customer(found[0]) if found else None

    async def create_customer(self, customer: NewCustomer) -> Customer:
        body = await self._must("POST", "/customers", name="create_customer", json={
            "name": customer.name,
            "cpfCnpj": customer.tax_id,
            "email": customer.email,
            "mobilePhone": customer.phone,
            "externalReference": customer.external_ref,
            # The desk talks to the payer, on the payer's channel: no second
            # e-mail or SMS from Asaas.
            "notificationDisabled": True,
        })
        return _customer(body)

    async def create_charge(self, charge: NewCharge) -> Charge:
        body = await self._must("POST", "/payments", name="create_charge", json={
            "customer": charge.customer_id,
            "billingType": _BILLING_TYPES[charge.method],
            "value": float(to_reais(charge.value_cents)),
            "dueDate": charge.due_date.isoformat(),
            "description": charge.description,
            "externalReference": charge.external_ref,
        })
        return _charge(body)

    async def find_charge(self, external_ref: str) -> Charge | None:
        """By our reference: how a charge whose answer was lost is found again."""
        body = await self._must("GET", "/payments", name="find_charge",
                                params={"externalReference": external_ref, "limit": 1})
        found = body.get("data") or []
        return _charge(found[0]) if found else None

    async def get_charge(self, provider_id: str) -> Charge | None:
        body = await self._call("GET", f"/payments/{provider_id}", name="get_charge",
                                missing_ok=True)
        return _charge(body) if body is not None else None

    async def cancel_charge(self, provider_id: str) -> bool:
        body = await self._must("DELETE", f"/payments/{provider_id}", name="cancel_charge")
        return bool(body.get("deleted"))
