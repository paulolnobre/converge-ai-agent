"""The calls the app needs from a payment provider.

A Protocol, like the workspace ports: Asaas is the first implementation; a
clinic's own Asaas account or another gateway is another one, chosen in config.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.integrations.payments.models import Account, Charge, Customer, NewCharge, NewCustomer


@runtime_checkable
class PaymentsPort(Protocol):
    async def account(self) -> Account: ...

    async def find_customer(self, external_ref: str) -> Customer | None: ...

    async def create_customer(self, customer: NewCustomer) -> Customer: ...

    async def create_charge(self, charge: NewCharge) -> Charge: ...

    async def find_charge(self, external_ref: str) -> Charge | None: ...

    async def get_charge(self, provider_id: str) -> Charge | None: ...

    async def cancel_charge(self, provider_id: str) -> bool: ...
