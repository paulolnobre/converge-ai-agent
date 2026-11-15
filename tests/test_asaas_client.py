"""The Asaas client: what goes to Asaas, how its answers read back, and that
the key and the payer's CPF/CNPJ never reach a log or an exception."""
import json
import logging
from datetime import date

import httpx
import pytest

from core.config import settings
from core.integrations import payments
from core.integrations.payments import asaas
from core.integrations.payments.asaas import to_cents, to_reais

KEY = "$aact_hmlg_TEST-S3CRET"
CPF = "52998224725"
SANDBOX = "https://api-sandbox.asaas.com/v3"


class FakeAsaas:
    """Records every request; answers what the test set for (method, path)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], tuple[int, dict]] = {}
        self.raise_error: Exception | None = None

    def answer(self, method: str, path: str, status: int, body: dict) -> None:
        self.answers[(method, path)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        path = request.url.path.removeprefix("/v3")
        status, body = self.answers.get((request.method, path), (200, {}))
        return httpx.Response(status, json=body)

    def sent(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    fake = FakeAsaas()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(asaas, "_http", lambda: client)
    monkeypatch.setattr(settings, "asaas_api_key", KEY)
    monkeypatch.setattr(settings, "asaas_env", "sandbox")
    return fake


def _charge(**overrides) -> payments.NewCharge:
    values = dict(customer_id="cus_1", method="link", value_cents=1990,
                  due_date=date(2026, 11, 5), description="Plano mensal",
                  external_ref="charge:1")
    values.update(overrides)
    return payments.NewCharge(**values)  # type: ignore[arg-type]


PAYMENT = {"id": "pay_1", "status": "PENDING", "value": 19.9,
           "dueDate": "2026-11-05", "invoiceUrl": "https://x/i/1"}


async def test_headers_and_base_url(fake):
    fake.answer("GET", "/myAccount", 200, {"email": "a@b.c", "name": "Converge"})

    account = await payments.payments().account()

    request = fake.requests[0]
    assert str(request.url) == f"{SANDBOX}/myAccount"
    assert request.headers["access_token"] == KEY
    assert request.headers["User-Agent"] == "converge-app"
    assert account == payments.Account(email="a@b.c", name="Converge")


async def test_production_base_url(fake, monkeypatch):
    monkeypatch.setattr(settings, "asaas_env", "production")
    fake.answer("GET", "/myAccount", 200, {"email": "a@b.c", "name": "C"})

    await payments.payments().account()

    assert fake.requests[0].url.host == "api.asaas.com"


async def test_create_customer_disables_asaas_notifications(fake):
    fake.answer("POST", "/customers", 200, {"id": "cus_1", "externalReference": "contact:7"})

    customer = await payments.payments().create_customer(payments.NewCustomer(
        name="Maria", tax_id=CPF, email="m@x.com", phone="91999990000",
        external_ref="contact:7"))

    assert customer == payments.Customer(provider_id="cus_1", external_ref="contact:7")
    sent = fake.sent()
    assert sent["notificationDisabled"] is True
    assert sent["cpfCnpj"] == CPF
    assert sent["externalReference"] == "contact:7"
    assert sent["name"] == "Maria"
    assert sent["mobilePhone"] == "91999990000"


async def test_find_customer_by_external_reference(fake):
    fake.answer("GET", "/customers", 200,
                {"data": [{"id": "cus_9", "externalReference": "contact:7"}]})

    found = await payments.payments().find_customer("contact:7")

    assert found == payments.Customer(provider_id="cus_9", external_ref="contact:7")
    assert fake.requests[0].url.params["externalReference"] == "contact:7"


async def test_find_customer_none_when_empty(fake):
    fake.answer("GET", "/customers", 200, {"data": []})

    assert await payments.payments().find_customer("contact:7") is None


async def test_link_is_undefined_billing_type(fake):
    fake.answer("POST", "/payments", 200, PAYMENT)

    charge = await payments.payments().create_charge(_charge())

    sent = fake.sent()
    assert sent["billingType"] == "UNDEFINED"
    assert sent["value"] == 19.9
    assert sent["dueDate"] == "2026-11-05"
    assert sent["customer"] == "cus_1"
    assert sent["externalReference"] == "charge:1"
    assert charge == payments.Charge(provider_id="pay_1", status="pendente", value_cents=1990,
                                     due_date=date(2026, 11, 5), invoice_url="https://x/i/1")


@pytest.mark.parametrize("method, billing_type", [("boleto", "BOLETO"), ("pix", "PIX")])
async def test_boleto_and_pix_billing_types(fake, method, billing_type):
    fake.answer("POST", "/payments", 200, PAYMENT)

    await payments.payments().create_charge(_charge(method=method))

    assert fake.sent()["billingType"] == billing_type


@pytest.mark.parametrize("raw, status", [
    ("PENDING", "pendente"),
    ("RECEIVED", "pago"), ("CONFIRMED", "pago"), ("RECEIVED_IN_CASH", "pago"),
    ("OVERDUE", "vencido"),
    ("REFUNDED", "estornado"), ("REFUND_REQUESTED", "estornado"),
    ("REFUND_IN_PROGRESS", "estornado"), ("CHARGEBACK_REQUESTED", "estornado"),
    ("CHARGEBACK_DISPUTE", "estornado"), ("AWAITING_CHARGEBACK_REVERSAL", "estornado"),
    ("SOMETHING_NEW", "pendente"),
])
async def test_status_mapping(fake, raw, status):
    fake.answer("GET", "/payments/pay_1", 200, {**PAYMENT, "status": raw})

    charge = await payments.payments().get_charge("pay_1")

    assert charge is not None and charge.status == status


async def test_deleted_payment_is_cancelled(fake):
    fake.answer("GET", "/payments/pay_1", 200, {**PAYMENT, "deleted": True})

    charge = await payments.payments().get_charge("pay_1")

    assert charge is not None and charge.status == "cancelado"


def test_cents_round_trip():
    assert to_cents(19.9) == 1990
    assert to_cents(0.1 + 0.2) == 30
    assert to_cents("10") == 1000
    assert to_cents(10) == 1000
    assert to_reais(1990) == "19.90"
    assert to_reais(5) == "0.05"


@pytest.mark.parametrize("code", ["invalid_access_token", "invalid_environment"])
async def test_401_is_auth_error_with_code(fake, code):
    fake.answer("GET", "/myAccount", 401,
                {"errors": [{"code": code, "description": "A chave é inválida"}]})

    with pytest.raises(payments.PaymentsAuthError) as caught:
        await payments.payments().account()

    assert caught.value.code == code
    assert caught.value.status == 401


async def test_400_is_rejected_with_codes_only(fake):
    fake.answer("POST", "/customers", 400, {"errors": [
        {"code": "invalid_cpfCnpj", "description": f"O CPF {CPF} é inválido"}]})

    with pytest.raises(payments.PaymentsRejected) as caught:
        await payments.payments().create_customer(payments.NewCustomer(
            name="Maria", tax_id=CPF, email=None, phone=None, external_ref="contact:7"))

    assert caught.value.codes == ["invalid_cpfCnpj"]
    assert CPF not in str(caught.value)
    assert KEY not in str(caught.value)


async def test_5xx_is_transient(fake):
    fake.answer("GET", "/myAccount", 502, {})

    with pytest.raises(payments.PaymentsTransient):
        await payments.payments().account()


async def test_timeout_is_transient(fake):
    fake.raise_error = httpx.ReadTimeout("slow")

    with pytest.raises(payments.PaymentsTransient):
        await payments.payments().account()


async def test_get_charge_404_is_none(fake):
    fake.answer("GET", "/payments/pay_x", 404, {"errors": [{"code": "not_found"}]})

    assert await payments.payments().get_charge("pay_x") is None


async def test_cancel_charge(fake):
    fake.answer("DELETE", "/payments/pay_1", 200, {"deleted": True, "id": "pay_1"})

    assert await payments.payments().cancel_charge("pay_1") is True
    assert fake.requests[0].method == "DELETE"


async def test_secrets_never_in_logs(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.answer("POST", "/customers", 400, {"errors": [
        {"code": "invalid_cpfCnpj", "description": f"O CPF {CPF} é inválido"}]})
    fake.answer("GET", "/myAccount", 401, {"errors": [{"code": "invalid_access_token"}]})

    with pytest.raises(payments.PaymentsRejected):
        await payments.payments().create_customer(payments.NewCustomer(
            name="Maria", tax_id=CPF, email=None, phone=None, external_ref="contact:7"))
    with pytest.raises(payments.PaymentsAuthError):
        await payments.payments().account()

    assert KEY not in caplog.text
    assert CPF not in caplog.text


def test_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "asaas_api_key", "")

    with pytest.raises(payments.PaymentsNotConfigured):
        payments.payments()


def test_the_asaas_client_satisfies_the_port(fake):
    assert isinstance(payments.payments(), payments.PaymentsPort)


async def test_find_charge_by_external_reference(fake):
    fake.answer("GET", "/payments", 200, {"data": [PAYMENT]})

    found = await payments.payments().find_charge("charge:10")

    assert found is not None and found.provider_id == "pay_1"
    assert fake.requests[0].url.params["externalReference"] == "charge:10"


async def test_find_charge_none_when_empty(fake):
    fake.answer("GET", "/payments", 200, {"data": []})

    assert await payments.payments().find_charge("charge:10") is None

