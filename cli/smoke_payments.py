"""Prove the Asaas key works, before the first real charge (docs/ASAAS.md).

Sandbox: checks the account, creates a test customer and a R$ 5,00 charge,
reads it back and cancels it. Production: checks the account only, unless
`--producao`. Never prints the key, the CPF/CNPJ or the payment link.

    python -m cli.smoke_payments [--producao]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta

from core.config import settings
from core.integrations import payments

# A valid but fictitious CPF, the one Asaas's own examples use.
_TEST_TAX_ID = "52998224725"
_TEST_CUSTOMER = "smoke:converge"
_VALUE_CENTS = 500


async def main(argv: list[str]) -> int:
    if not settings.payments_configured():
        print("pagamentos desligados: ASAAS_API_KEY vazia")
        return 1
    print(f"ambiente:  {settings.asaas_env}")
    port = payments.payments()
    try:
        account = await port.account()
    except payments.PaymentsError as exc:
        print(f"conta:     FALHOU — {exc.code or exc.status or type(exc).__name__}")
        return 1
    print(f"conta:     ok — {account.email}")

    if settings.asaas_env == "production" and "--producao" not in argv:
        print("produção:  só a conta foi conferida; nada criado")
        return 0

    try:
        customer = await port.find_customer(_TEST_CUSTOMER) or await port.create_customer(
            payments.NewCustomer(name="Teste smoke Converge", tax_id=_TEST_TAX_ID,
                                 email=None, phone=None, external_ref=_TEST_CUSTOMER))
        created = await port.create_charge(payments.NewCharge(
            customer_id=customer.provider_id, method="link", value_cents=_VALUE_CENTS,
            due_date=date.today() + timedelta(days=3),
            description="Smoke de integração — pode apagar",
            external_ref=f"smoke:{datetime.now():%Y%m%d%H%M%S}"))
    except payments.PaymentsError as exc:
        print(f"cobrança:  FALHOU — {_failure(exc)}")
        return 1

    # From here a charge exists: whatever happens, try to cancel it.
    problem = None
    try:
        read = await port.get_charge(created.provider_id)
        if read is None or read.status != "pendente" or read.value_cents != _VALUE_CENTS:
            problem = "lida de volta diferente da criada"
    except payments.PaymentsError as exc:
        problem = f"leitura: {_failure(exc)}"
    try:
        cancelled = await port.cancel_charge(created.provider_id)
    except payments.PaymentsError:
        cancelled = False
    if not cancelled:
        print(f"cobrança:  FALHOU — {problem + '; ' if problem else ''}"
              f"não cancelada; apague no Asaas a de {created.due_date:%d/%m}")
        return 1
    if problem:
        print(f"cobrança:  FALHOU — {problem} (cancelada)")
        return 1
    print("cobrança:  ok — criada, lida e cancelada")
    return 0


def _failure(exc: payments.PaymentsError) -> str:
    codes = getattr(exc, "codes", None) or [exc.code or str(exc.status or "")]
    return f"{type(exc).__name__} {', '.join(c for c in codes if c)}".strip()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
