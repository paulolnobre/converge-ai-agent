"""The setup charged when the contract is signed: the first payment as the
contract says, in the way the customer chose, once, and the team always hears
how it went."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.config import settings
from core.integrations import payments
from core.models import Contact
from core.repositories import audit
from core.repositories import billing as billing_repo
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as details_repo
from core.repositories import contract_signatures as signatures_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as briefings_repo
from core.services import billing, contract_charge, inbox, pricing

TABLE = SimpleNamespace(parcelas_max=6, taxa_cartao_pct=Decimal("0.0249"), taxa_cartao_fixa=49)


def _terms(setup=1_000_000, forma="metade"):
    return pricing.setup_plan(setup, forma, 6)


@pytest.mark.parametrize(("answer", "method"), [
    ("cartao", "cartao"), ("Cartão de crédito", "cartao"), ("boleto", "boleto"),
    ("Boleto", "boleto"), ("pix", "pix"), ("Pix", "pix"), ("", "link"), (None, "link"),
    ("transferência", "link")])
def test_the_customers_choice_is_the_method(answer, method):
    assert contract_charge.method_of(answer) == method


def test_the_card_fee_leaves_exactly_the_net():
    gross = pricing.with_card_fee(120_000, TABLE)  # type: ignore[arg-type]
    assert gross == 123_115
    after = gross - gross * Decimal("0.0249") - 49
    assert 120_000 <= after < 120_001


def test_half_is_half_and_carries_no_card_fee():
    first = contract_charge.first_payment(_terms(), TABLE, "cartao")  # type: ignore[arg-type]
    assert (first.method, first.net_cents, first.value_cents) == ("cartao", 500_000, 500_000)
    assert "50%" in first.description


def test_installments_by_card_carry_the_card_fee():
    first = contract_charge.first_payment(_terms(forma="parcelado"), TABLE,  # type: ignore[arg-type]
                                          "Cartão de crédito")
    assert first.net_cents == 166_670
    assert first.value_cents == pricing.with_card_fee(166_670, TABLE)  # type: ignore[arg-type]
    assert "1 de 6" in first.description


@pytest.mark.parametrize("answer", ["pix", "boleto", None])
def test_installments_otherwise_carry_none(answer):
    first = contract_charge.first_payment(_terms(forma="parcelado"), TABLE, answer)  # type: ignore[arg-type]
    assert first.value_cents == first.net_cents == 166_670


# ── Charging ────────────────────────────────────────────────────────────────

DETAILS = {"razao_social": "Clínica Sol Ltda", "documento": "11.222.333/0001-81",
           "assinante_email": "ana@sol.com.br"}


class Port:
    def __init__(self, w):
        self.w = w
        self.customers: list[payments.NewCustomer] = []
        self.charges: list[payments.NewCharge] = []
        self.fail: Exception | None = None

    async def find_customer(self, ref):
        return None

    async def create_customer(self, customer):
        self.customers.append(customer)
        return payments.Customer("cus_1", customer.external_ref)

    async def create_charge(self, charge):
        self.charges.append(charge)
        if self.fail:
            raise self.fail
        return payments.Charge("pay_1", "pendente", charge.value_cents, charge.due_date,
                               "https://x/i/1")

    async def find_charge(self, ref):
        return self.w.asaas_charge

    async def get_charge(self, provider_id):
        return self.w.asaas_charge


class _Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Conn:
    def __init__(self, w):
        self.w = w

    async def execute(self, sql, params=None):
        if "pg_try_advisory_lock" in sql:
            self.w.locks.append("lock")
            return _Cursor((self.w.lockable,))
        self.w.locks.append("unlock")
        return _Cursor((True,))

    async def commit(self):
        self.w.commits += 1
        self.w.seq.append("commit")

    async def rollback(self):
        self.w.rollbacks += 1
        self.w.seq.append("rollback")


@pytest.fixture
def w(monkeypatch):
    w = SimpleNamespace(
        contact=Contact(id=9, chat_id="42", name="Ana Souza", email="ana@x.com"),
        briefing=SimpleNamespace(id=4, answers={"pagamento": "Pix"}),
        terms=_terms(), sandbox=True, done=None, asaas_charge=None, customer=None, inserted=[], duplicate=False, statuses=[],
        providers=[], sent=[], recorded=[], marked=[], told=[], emails=[], window=True,
        delivery="ok", templates=[], commits=0, rollbacks=0, details=dict(DETAILS),
        locks=[], lockable=True, seq=[])
    w.port = Port(w)
    monkeypatch.setattr(settings, "asaas_api_key", "k")
    monkeypatch.setattr(settings, "asaas_env", "sandbox")
    monkeypatch.setattr(settings, "whatsapp_charge_template", None)

    async def get_by_id(conn, contact_id):
        return w.contact if contact_id == 9 else None

    async def get_briefing(conn, briefing_id):
        return w.briefing

    async def get_sent(conn, briefing_id):
        return SimpleNamespace(sandbox=w.sandbox, terms=w.terms)

    async def setup_charge(conn, briefing_id, part):
        return w.done

    async def get_details(conn, briefing_id):
        return w.details

    async def customer_of(conn, contact_id):
        return w.customer

    async def save_customer(conn, contact_id, provider_id, tax_id):
        w.customer = {"id": 3, "provider_id": provider_id, "tax_id": tax_id}
        return 3

    async def insert_setup_charge(conn, **values):
        w.inserted.append(values)
        return None if w.duplicate else 11

    async def set_status(conn, charge_id, status):
        w.statuses.append((charge_id, status))

    async def set_provider(conn, charge_id, provider_id, url):
        w.providers.append((charge_id, provider_id))

    async def mark_sent(conn, charge_id):
        w.marked.append(charge_id)

    async def record(conn, **values):
        pass

    async def window_open(conn, contact_id):
        return w.window

    async def deliver(chat_id, text, keyboard=None, parse_mode=None):
        w.sent.append((chat_id, text))
        return w.delivery

    async def send_template(chat_id, name, params, buttons=None, **kw):
        w.templates.append((name, params))
        return "ok"

    async def record_message(conn, contact_id, chat_id, role, text, source, **kw):
        w.recorded.append((contact_id, text, source))

    async def notify_team(text, contact_id):
        w.told.append(text)
        return True

    async def email(conn, contact, kind, values):
        w.emails.append(kind)
        w.seq.append("email")
        return True

    monkeypatch.setattr(payments, "payments", lambda: w.port)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(briefings_repo, "get", get_briefing)
    monkeypatch.setattr(signatures_repo, "get", get_sent)
    monkeypatch.setattr(billing_repo, "setup_charge", setup_charge)
    monkeypatch.setattr(details_repo, "get", get_details)
    monkeypatch.setattr(pricing, "load_table", lambda: TABLE)
    monkeypatch.setattr(billing_repo, "customer_of", customer_of)
    monkeypatch.setattr(billing_repo, "save_customer", save_customer)
    monkeypatch.setattr(billing_repo, "insert_setup_charge", insert_setup_charge)
    monkeypatch.setattr(billing_repo, "set_status", set_status)
    monkeypatch.setattr(billing_repo, "set_provider", set_provider)
    monkeypatch.setattr(billing_repo, "mark_sent", mark_sent)
    monkeypatch.setattr(audit, "record", record)
    monkeypatch.setattr(inbox_repo, "window_open", window_open)
    monkeypatch.setattr(contract_charge.channels, "deliver", deliver)
    monkeypatch.setattr(contract_charge.channels, "is_whatsapp", lambda chat_id: True)
    monkeypatch.setattr(inbox, "send_template", send_template)
    monkeypatch.setattr(inbox, "push_to_team", lambda text, contact: None)
    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(billing, "_email", email)
    return w


async def _run(w, sandbox=True):
    w.sandbox = sandbox
    return await contract_charge.charge_signed(Conn(w), 4, 9)


async def test_signed_contract_charges_half_by_pix(w):
    assert await _run(w) == "sent"
    customer = w.port.customers[0]
    assert (customer.name, customer.tax_id, customer.email) == (
        "Clínica Sol Ltda", "11222333000181", "ana@sol.com.br")
    charge = w.port.charges[0]
    assert (charge.method, charge.value_cents, charge.external_ref) == ("pix", 500_000,
                                                                        "charge:11")
    assert w.inserted[0]["briefing_id"] == 4 and w.inserted[0]["part"] == 1
    assert w.providers == [(11, "pay_1")]
    assert "https://x/i/1" in w.sent[0][1] and "R$ 5.000,00" in w.sent[0][1]
    assert w.marked == [11] and w.recorded[0][2] == "billing"
    after = w.seq[w.seq.index("email") + 1:]
    assert w.emails == ["charge"] and after[0] == "commit"  # the audit line stays
    assert "💳" in w.told[0] and "R$ 5.000,00" in w.told[0]


async def test_card_installments_go_as_card_with_the_fee(w):
    w.terms = _terms(forma="parcelado")
    w.briefing.answers = {"pagamento": "cartao"}
    await _run(w)
    charge = w.port.charges[0]
    assert charge.method == "cartao"
    assert charge.value_cents == pricing.with_card_fee(166_670, TABLE)  # type: ignore[arg-type]


async def test_a_known_customer_is_reused(w):
    w.customer = {"id": 3, "provider_id": "cus_old", "tax_id": "11222333000181"}
    await _run(w)
    assert w.port.customers == [] and w.port.charges[0].customer_id == "cus_old"


async def test_the_same_contract_is_charged_once(w):
    w.duplicate = True
    assert await _run(w) == "duplicate"
    assert w.port.charges == [] and w.told == []


@pytest.mark.parametrize(("sandbox", "test_mode"), [(True, False), (False, True)])
async def test_a_test_contract_never_charges_in_production(w, monkeypatch, sandbox, test_mode):
    monkeypatch.setattr(settings, "asaas_env", "production")
    w.contact = w.contact.model_copy(update={"test_mode": test_mode})
    assert await _run(w, sandbox) == "test"
    assert w.port.charges == [] and w.inserted == []
    # A /teste contact is quiet in the team's group (push only).
    assert w.told == [] if test_mode else "teste" in w.told[0]


async def test_payments_off_tells_the_team(w, monkeypatch):
    monkeypatch.setattr(settings, "asaas_api_key", None)
    assert await _run(w) == "off"
    assert w.inserted == [] and "desligada" in w.told[0]


async def test_a_contract_sent_without_its_terms_tells_the_team(w):
    w.terms = None
    assert await _run(w) == "no_terms"
    assert w.port.charges == [] and "condições" in w.told[0]


async def test_an_invalid_tax_id_tells_the_team(w):
    w.details = {**DETAILS, "documento": "123"}
    assert await _run(w) == "tax_id"
    assert w.port.customers == [] and "CNPJ/CPF" in w.told[0]


async def test_asaas_refusing_cancels_the_row(w):
    w.port.fail = payments.PaymentsRejected("invalid", codes=["invalid_value"])
    assert await _run(w) == "asaas"
    assert w.statuses == [(11, "cancelado")] and "Asaas" in w.told[0]


async def test_asaas_unreachable_keeps_the_row_to_check(w):
    w.port.fail = payments.PaymentsTransient("timeout")
    assert await _run(w) == "asaas"
    assert w.statuses == [] and w.providers == []


async def test_closed_window_without_template_is_created_not_delivered(w):
    w.window = False
    assert await _run(w) == "not_delivered"
    assert w.providers == [(11, "pay_1")] and w.sent == [] and w.marked == []
    assert "Reenviar" in w.told[0]


async def test_closed_window_goes_as_the_charge_template(w, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_charge_template", "cobranca_converge")
    monkeypatch.setattr(inbox, "template_ready", lambda name: bool(name))
    w.window = False
    assert await _run(w) == "sent"
    name, params = w.templates[0]
    assert name == "cobranca_converge" and params[0] == "Ana" and params[4] == "https://x/i/1"
    assert w.marked == [11]


async def test_a_blocked_contact_hears_nothing(w):
    w.contact = w.contact.model_copy(update={"blocked_at": datetime(2026, 11, 1, tzinfo=UTC)})
    assert await _run(w) == "not_delivered"
    assert w.sent == [] and w.emails == [] and w.port.charges


async def test_an_unexpected_error_never_raises(w, monkeypatch):
    async def boom(conn, briefing_id):
        raise RuntimeError("db")

    monkeypatch.setattr(details_repo, "get", boom)
    assert await _run(w) == "failed"
    assert w.rollbacks >= 1 and "falhou" in w.told[0]


# ── Picking up a charge left halfway ────────────────────────────────────────

DUE = datetime(2026, 11, 16).date()
LEFT = {"id": 11, "provider_id": None, "status": "pendente", "method": "pix",
        "value_cents": 500_000, "description": "Implantação", "due_date": DUE, "sent_at": None}


async def test_a_charge_sent_already_is_never_redone(w):
    w.done = {**LEFT, "provider_id": "pay_1", "sent_at": datetime(2026, 11, 13, tzinfo=UTC)}
    assert await _run(w) == "duplicate"
    assert w.port.charges == [] and w.sent == [] and w.told == []


async def test_a_paid_or_cancelled_charge_is_left_alone(w):
    w.done = {**LEFT, "status": "cancelado"}
    assert await _run(w) == "duplicate"
    assert w.port.charges == [] and w.told == []


async def test_a_row_asaas_never_got_is_created_now(w):
    w.customer = {"id": 3, "provider_id": "cus_1", "tax_id": "11222333000181"}
    w.done = dict(LEFT)
    assert await _run(w) == "sent"
    assert w.inserted == []
    charge = w.port.charges[0]
    assert (charge.external_ref, charge.value_cents, charge.due_date) == ("charge:11", 500_000,
                                                                          DUE)
    assert w.providers == [(11, "pay_1")] and w.marked == [11]


async def test_a_lost_answer_is_found_by_reference_not_charged_again(w):
    w.customer = {"id": 3, "provider_id": "cus_1", "tax_id": "11222333000181"}
    w.done = dict(LEFT)
    w.asaas_charge = payments.Charge("pay_9", "pendente", 500_000, DUE, "https://x/i/9")
    assert await _run(w) == "sent"
    assert w.port.charges == [] and w.providers == [(11, "pay_9")]
    assert "https://x/i/9" in w.sent[0][1]


async def test_a_created_charge_not_delivered_is_delivered_now(w):
    w.customer = {"id": 3, "provider_id": "cus_1", "tax_id": "11222333000181"}
    w.done = {**LEFT, "provider_id": "pay_1"}
    w.asaas_charge = payments.Charge("pay_1", "pendente", 500_000, DUE, "https://x/i/1")
    assert await _run(w) == "sent"
    assert w.port.charges == [] and w.providers == [] and w.marked == [11]
    assert "💳" in w.told[0]


async def test_a_charge_no_longer_open_at_asaas_tells_the_team(w):
    w.customer = {"id": 3, "provider_id": "cus_1", "tax_id": "11222333000181"}
    w.done = {**LEFT, "provider_id": "pay_1"}
    w.asaas_charge = payments.Charge("pay_1", "pago", 500_000, DUE, "https://x/i/1")
    assert await _run(w) == "closed"
    assert w.sent == [] and "não está em aberto" in w.told[0]


async def test_a_charge_found_by_reference_but_paid_is_bound_not_sent(w):
    w.customer = {"id": 3, "provider_id": "cus_1", "tax_id": "11222333000181"}
    w.done = dict(LEFT)
    w.asaas_charge = payments.Charge("pay_9", "pago", 500_000, DUE, "https://x/i/9")
    assert await _run(w) == "closed"
    assert w.providers == [(11, "pay_9")] and w.port.charges == [] and w.sent == []


async def test_another_worker_charging_is_left_to_it(w):
    w.lockable = False
    assert await _run(w) == "busy"
    assert w.inserted == [] and w.port.charges == [] and w.told == []
    assert w.locks == ["lock"]


async def test_the_lock_is_released_whatever_happens(w):
    w.port.fail = payments.PaymentsRejected("invalid", codes=["invalid_value"])
    await _run(w)
    assert w.locks == ["lock", "unlock"]
