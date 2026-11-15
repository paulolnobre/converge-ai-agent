"""The payments smoke proves the key on the real path and cleans up after
itself; in production it only reads the account unless told otherwise."""
from datetime import date

import pytest

from cli import smoke_payments
from core.config import settings
from core.integrations import payments

KEY = "$aact_hmlg_SMOKE-S3CRET"


class FakePort:
    def __init__(self, auth_error: str | None = None):
        self.calls: list[str] = []
        self.created: list[payments.NewCharge] = []
        self.customers: list[payments.NewCustomer] = []
        self.auth_error = auth_error

    async def account(self):
        self.calls.append("account")
        if self.auth_error:
            raise payments.PaymentsAuthError("Asaas account: 401", 401, self.auth_error)
        return payments.Account(email="owner@example.com", name="Converge")

    async def find_customer(self, external_ref):
        self.calls.append("find_customer")
        return None

    async def create_customer(self, customer):
        self.calls.append("create_customer")
        self.customers.append(customer)
        return payments.Customer(provider_id="cus_1", external_ref=customer.external_ref)

    async def create_charge(self, charge):
        self.calls.append("create_charge")
        self.created.append(charge)
        return payments.Charge("pay_1", "pendente", charge.value_cents, charge.due_date,
                               "https://x/i/secret-link")

    async def get_charge(self, provider_id):
        self.calls.append("get_charge")
        charge = self.created[-1]
        return payments.Charge(provider_id, "pendente", charge.value_cents, charge.due_date,
                               "https://x/i/secret-link")

    async def cancel_charge(self, provider_id):
        self.calls.append("cancel_charge")
        return True


@pytest.fixture
def port(monkeypatch):
    fake = FakePort()
    monkeypatch.setattr(payments, "payments", lambda: fake)
    monkeypatch.setattr(settings, "asaas_api_key", KEY)
    monkeypatch.setattr(settings, "asaas_env", "sandbox")
    return fake


async def test_without_key_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(settings, "asaas_api_key", "")

    assert await smoke_payments.main([]) == 1
    assert "ASAAS_API_KEY" in capsys.readouterr().out


async def test_auth_error_prints_code_and_returns_1(port, capsys):
    port.auth_error = "invalid_access_token"

    assert await smoke_payments.main([]) == 1

    out = capsys.readouterr().out
    assert "invalid_access_token" in out
    assert KEY not in out
    assert port.calls == ["account"]


async def test_production_checks_account_only(port, monkeypatch):
    monkeypatch.setattr(settings, "asaas_env", "production")

    assert await smoke_payments.main([]) == 0

    assert port.calls == ["account"]
    assert port.created == []


async def test_production_with_flag_creates(port, monkeypatch):
    monkeypatch.setattr(settings, "asaas_env", "production")

    assert await smoke_payments.main(["--producao"]) == 0

    assert "cancel_charge" in port.calls


async def test_sandbox_creates_reads_and_cancels(port, capsys):
    assert await smoke_payments.main([]) == 0

    assert port.calls == ["account", "find_customer", "create_customer",
                          "create_charge", "get_charge", "cancel_charge"]
    charge = port.created[0]
    assert charge.value_cents == 500 and charge.method == "link"
    assert (charge.due_date - date.today()).days == 3
    assert port.customers[0].tax_id == "52998224725"
    out = capsys.readouterr().out
    assert "52998224725" not in out
    assert "secret-link" not in out
    assert KEY not in out
    assert "owner@example.com" in out


async def test_a_failed_read_still_cancels_the_charge(port, capsys):
    async def broken_read(provider_id):
        port.calls.append("get_charge")
        raise payments.PaymentsTransient("Asaas get_charge: ReadTimeout")
    port.get_charge = broken_read

    assert await smoke_payments.main([]) == 1

    assert port.calls[-1] == "cancel_charge"
    assert "FALHOU" in capsys.readouterr().out


async def test_a_failed_cancel_says_to_clean_up_by_hand(port, capsys):
    async def broken_cancel(provider_id):
        port.calls.append("cancel_charge")
        raise payments.PaymentsTransient("Asaas cancel_charge: 502", 502)
    port.cancel_charge = broken_cancel

    assert await smoke_payments.main([]) == 1

    assert "apague no Asaas" in capsys.readouterr().out
