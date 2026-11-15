"""Charges from the Inbox: who may charge, what reaches the customer, and that
a charge Asaas already has is never lost on the way."""
import asyncio
from datetime import date

import pytest

from core import content
from core.services import billing


@pytest.mark.parametrize("cents, text", [
    (123456, "R$ 1.234,56"), (5, "R$ 0,05"), (1990, "R$ 19,90"), (100000000, "R$ 1.000.000,00"),
])
def test_format_brl(cents, text):
    assert billing.format_brl(cents) == text


def test_mask_tax_id():
    assert billing.mask_tax_id("52998224725") == "***.982.247-**"
    assert billing.mask_tax_id("11222333000181") == "**.222.333/0001-**"


def test_link_message_offers_every_way_to_pay():
    text = billing.charge_message("link", "Plano mensal", 1990, date(2026, 11, 5), "https://x/i/1")
    assert text == content.render("billing.charge_sent", descricao="Plano mensal",
                                  valor="R$ 19,90", vencimento="05/11/2026", link="https://x/i/1")
    assert "boleto, Pix ou cartão" in text


@pytest.mark.parametrize("method", ["boleto", "pix"])
def test_fixed_method_message(method):
    text = billing.charge_message(method, "Plano", 500, date(2026, 11, 5), "https://x/i/1")
    assert text == content.render("billing.charge_sent_fixed", descricao="Plano",
                                  valor="R$ 5,00", vencimento="05/11/2026", link="https://x/i/1")


# ── The service ─────────────────────────────────────────────────────────────

from datetime import timedelta  # noqa: E402

from core.config import settings  # noqa: E402
from core.integrations import payments  # noqa: E402
from core.models import Contact, HandoffSession  # noqa: E402
from core.repositories import audit  # noqa: E402
from core.repositories import billing as billing_repo  # noqa: E402
from core.repositories import contacts as contacts_repo  # noqa: E402
from core.repositories import handoffs as handoffs_repo  # noqa: E402
from core.repositories import inbox as inbox_repo  # noqa: E402
from core.services import inbox  # noqa: E402

PAULO = {"user_id": 1, "email": "paulo@converge.test", "name": "Paulo Nobre"}
CPF = "52998224725"
DUE = date.today() + timedelta(days=3)


async def _value(value):
    return value


class Conn:
    def __init__(self, world):
        self.world = world

    async def commit(self):
        self.world.events.append("commit")


class Port:
    def __init__(self, world):
        self.w = world
        self.calls: list[str] = []
        self.charges: list[payments.NewCharge] = []
        self.customers: list[payments.NewCustomer] = []
        self.existing: payments.Customer | None = None
        self.lost: payments.Charge | None = None   # what find_charge finds
        self.fail_charge: Exception | None = None  # only create_charge fails
        self.missing = False                       # get_charge finds nothing
        self.fail: Exception | None = None
        self.status: payments.Status = "pendente"

    def _check(self):
        if self.fail is not None:
            raise self.fail

    async def find_customer(self, external_ref):
        self.calls.append("find_customer")
        self._check()
        return self.existing

    async def create_customer(self, customer):
        self.calls.append("create_customer")
        self._check()
        self.customers.append(customer)
        return payments.Customer("cus_1", customer.external_ref)

    async def create_charge(self, charge):
        self.calls.append("create_charge")
        self.w.events.append("create_charge")
        self._check()
        if self.fail_charge is not None:
            self.charges.append(charge)
            raise self.fail_charge
        self.charges.append(charge)
        return payments.Charge("pay_1", "pendente", charge.value_cents, charge.due_date,
                               "https://x/i/1")

    async def find_charge(self, external_ref):
        self.calls.append("find_charge")
        self._check()
        return self.lost

    async def get_charge(self, provider_id):
        self.calls.append("get_charge")
        self._check()
        if self.missing:
            return None
        return payments.Charge(provider_id, self.status, 1990, DUE, "https://x/i/new")

    async def cancel_charge(self, provider_id):
        self.calls.append("cancel_charge")
        self._check()
        return True


class World:
    def __init__(self):
        self.contact = Contact(id=7, chat_id="42", name="Ana Souza", email="ana@x.com",
                               phone="91999990000")
        self.owner: int | None = 1
        self.window = True
        self.delivery = "ok"
        self.customer: dict | None = None
        self.charges: dict[int, dict] = {}
        self.sent: list[tuple[str, str]] = []
        self.recorded: list[tuple] = []
        self.audited: list[dict] = []
        self.events: list[str] = []
        self.port = Port(self)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(settings, "asaas_api_key", "$aact_hmlg_TEST")
    monkeypatch.setattr(settings, "asaas_env", "sandbox")
    monkeypatch.setattr(payments, "payments", lambda: w.port)

    async def get_by_id(conn, contact_id):
        return w.contact if contact_id == w.contact.id else None

    async def lock_for_reply(conn, chat_id, user_id):
        return 5 if w.owner == user_id else None

    async def active_session(conn, chat_id):
        if w.owner is None:
            return None
        return HandoffSession(id=5, chat_id="42", team_chat_id="t", relay_chat_id="t", reason="pedido",
                              status="claimed", claimed_by_user_id=w.owner,
                              claimed_by_name="Victor")

    async def send(channel, chat_id, text):
        if w.delivery == "ok":
            w.sent.append((chat_id, text))
        return w.delivery

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        w.recorded.append((role, message, intent, author_user_id))

    async def customer_of(conn, contact_id):
        return w.customer

    async def save_customer(conn, contact_id, provider_id, tax_id):
        w.customer = {"id": 3, "provider_id": provider_id, "tax_id": tax_id}
        return 3

    async def insert_charge(conn, **values):
        charge_id = len(w.charges) + 10
        w.charges[charge_id] = {**values, "id": charge_id, "provider_id": None, "sent": False}
        return charge_id

    async def set_provider(conn, charge_id, provider_id, invoice_url):
        w.charges[charge_id].update(provider_id=provider_id, invoice_url=invoice_url)

    async def mark_sent(conn, charge_id):
        w.charges[charge_id]["sent"] = True
        w.events.append("sent")

    async def set_status(conn, charge_id, status):
        w.charges[charge_id]["status"] = status

    async def charge_of(conn, contact_id, charge_id):
        return w.charges.get(charge_id) if contact_id == w.contact.id else None

    async def record(conn, **kwargs):
        w.audited.append(kwargs)

    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(handoffs_repo, "lock_for_reply", lock_for_reply)
    monkeypatch.setattr(handoffs_repo, "active_session", active_session)
    monkeypatch.setattr(handoffs_repo, "touch", lambda conn, sid: _value(None))
    monkeypatch.setattr(handoffs_repo, "lock_chat", lambda conn, chat_id: _value(None))
    monkeypatch.setattr(inbox_repo, "window_open", lambda conn, cid: _value(w.window))
    monkeypatch.setattr(inbox, "send", send)
    for name, fn in [("customer_of", customer_of), ("save_customer", save_customer),
                     ("insert_charge", insert_charge), ("set_provider", set_provider),
                     ("mark_sent", mark_sent), ("set_status", set_status),
                     ("charge_of", charge_of)]:
        monkeypatch.setattr(billing_repo, name, fn)
    monkeypatch.setattr(audit, "record", record)
    return w


def _form(**overrides) -> billing.ChargeForm:
    values: dict = dict(description="Plano mensal", value_cents=1990, due_date=DUE, method="link",
                  tax_id="529.982.247-25")
    values.update(overrides)
    return billing.ChargeForm(**values)


async def _refused(world, status, coro, contains=""):
    with pytest.raises(inbox.InboxError) as caught:
        await coro
    assert caught.value.status == status
    assert contains in caught.value.detail
    return caught.value


async def test_only_the_owner_can_charge(world):
    world.owner = None

    await _refused(world, 409, billing.create(Conn(world), 7, PAULO, _form()), "Assuma")
    assert world.port.calls == []


async def test_someone_elses_conversation_names_them(world):
    world.owner = 2

    await _refused(world, 409, billing.create(Conn(world), 7, PAULO, _form()), "Victor")


async def test_closed_whatsapp_window_refuses_before_asaas(world):
    world.contact = world.contact.model_copy(update={"channel": "whatsapp"})
    world.window = False

    await _refused(world, 409, billing.create(Conn(world), 7, PAULO, _form()),
                   content.text("inbox.window_closed"))
    assert world.port.calls == []


@pytest.mark.parametrize("tax_id", [None, "", "529.982.247-24"])
async def test_first_charge_needs_a_valid_tax_id(world, tax_id):
    await _refused(world, 422, billing.create(Conn(world), 7, PAULO, _form(tax_id=tax_id)),
                   "CPF/CNPJ")
    assert world.port.calls == []


async def test_first_charge_creates_customer_and_sends_the_link(world):
    await billing.create(Conn(world), 7, PAULO, _form())

    assert world.port.calls == ["find_customer", "create_customer", "create_charge"]
    customer = world.port.customers[0]
    assert customer == payments.NewCustomer(name="Ana Souza", tax_id=CPF, email="ana@x.com",
                                            phone="91999990000", external_ref="contact:7")
    assert world.customer == {"id": 3, "provider_id": "cus_1", "tax_id": CPF}
    [charge_id] = world.charges
    assert world.port.charges[0].external_ref == f"charge:{charge_id}"
    assert world.port.charges[0].customer_id == "cus_1"
    [(chat_id, text)] = world.sent
    assert chat_id == "42"
    assert text == billing.charge_message("link", "Plano mensal", 1990, DUE, "https://x/i/1")
    assert world.recorded == [("assistant", text, "billing", 1)]
    assert world.charges[charge_id]["sent"] is True
    assert world.charges[charge_id]["requested_by"] == 1
    assert world.charges[charge_id]["origin"] == "painel"
    assert world.events.index("commit") < world.events.index("sent")
    assert world.audited[-1]["action"] == "create_charge"
    assert world.audited[-1]["succeeded"] is True


async def test_nameless_contact_is_named_by_id(world):
    world.contact = world.contact.model_copy(update={"name": None, "email": None, "phone": None})

    await billing.create(Conn(world), 7, PAULO, _form())

    assert world.port.customers[0].name == "Contato 7"
    assert world.port.customers[0].email is None


async def test_second_charge_reuses_the_customer(world):
    world.customer = {"id": 3, "provider_id": "cus_1", "tax_id": CPF}

    await billing.create(Conn(world), 7, PAULO, _form(tax_id=None))

    assert world.port.calls == ["create_charge"]


async def test_existing_asaas_customer_is_found_not_duplicated(world):
    world.port.existing = payments.Customer("cus_old", "contact:7")

    await billing.create(Conn(world), 7, PAULO, _form())

    assert "create_customer" not in world.port.calls
    assert world.customer is not None and world.customer["provider_id"] == "cus_old"


@pytest.mark.parametrize("error", [
    payments.PaymentsTransient("Asaas create_charge: 502", 502),
    payments.PaymentsAuthError("Asaas: 401", 401, "invalid_access_token"),
])
async def test_asaas_down_sends_nothing(world, error):
    world.port.fail = error

    await _refused(world, 502, billing.create(Conn(world), 7, PAULO, _form()),
                   "Não foi possível falar com o Asaas.")
    assert world.sent == [] and "commit" not in world.events


async def test_rejected_shows_asaas_codes(world):
    world.port.fail = payments.PaymentsRejected("x", 400, ["invalid_cpfCnpj"])

    await _refused(world, 422, billing.create(Conn(world), 7, PAULO, _form()), "invalid_cpfCnpj")


async def test_send_failure_keeps_the_charge(world):
    world.delivery = "failed"

    await _refused(world, 502, billing.create(Conn(world), 7, PAULO, _form()), "Reenviar")

    [charge] = world.charges.values()
    assert charge["provider_id"] == "pay_1" and charge["sent"] is False
    assert "commit" in world.events
    assert world.recorded == []


async def test_resend_sends_the_current_invoice_url(world):
    world.customer = {"id": 3, "provider_id": "cus_1", "tax_id": CPF}
    world.delivery = "failed"
    with pytest.raises(inbox.InboxError):
        await billing.create(Conn(world), 7, PAULO, _form(tax_id=None))
    [charge_id] = world.charges
    world.delivery = "ok"

    await billing.resend(Conn(world), 7, charge_id, PAULO)

    [(_, text)] = world.sent
    assert "https://x/i/new" in text
    assert world.charges[charge_id]["sent"] is True


@pytest.mark.parametrize("status", ["pago", "cancelado"])
async def test_resend_refuses_what_is_not_open(world, status):
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": status, "method": "link",
                         "value_cents": 1990, "description": "x", "due_date": DUE, "sent": True}

    await _refused(world, 409, billing.resend(Conn(world), 7, 10, PAULO))
    assert world.port.calls == []


async def test_resend_needs_the_conversation(world):
    world.owner = None
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente",
                         "method": "link", "value_cents": 1990, "description": "x",
                         "due_date": DUE, "sent": False}

    await _refused(world, 409, billing.resend(Conn(world), 7, 10, PAULO), "Assuma")


async def test_cancel_open_charge(world):
    world.owner = None  # cancelling sends nothing: no need to hold the conversation
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente",
                         "method": "link", "value_cents": 1990, "description": "x",
                         "due_date": DUE, "sent": True}

    await billing.cancel(Conn(world), 7, 10, PAULO)

    assert world.port.calls == ["get_charge", "cancel_charge"]
    assert world.charges[10]["status"] == "cancelado"
    assert world.sent == []
    assert world.audited[-1]["action"] == "cancel_charge"


@pytest.mark.parametrize("status", ["pago", "cancelado", "estornado"])
async def test_cancel_refuses_what_is_not_open(world, status):
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": status, "method": "link",
                         "value_cents": 1990, "description": "x", "due_date": DUE, "sent": True}

    await _refused(world, 409, billing.cancel(Conn(world), 7, 10, PAULO), "não está em aberto")
    assert world.port.calls == []


async def test_unknown_charge_is_404(world):
    await _refused(world, 404, billing.cancel(Conn(world), 7, 99, PAULO))


async def test_test_contact_in_production_is_refused(world, monkeypatch):
    monkeypatch.setattr(settings, "asaas_env", "production")
    world.contact = world.contact.model_copy(update={"test_mode": "novo"})

    await _refused(world, 409, billing.create(Conn(world), 7, PAULO, _form()), "sandbox")
    assert world.port.calls == []


async def test_payments_off_is_404(world, monkeypatch):
    monkeypatch.setattr(settings, "asaas_api_key", "")

    await _refused(world, 404, billing.create(Conn(world), 7, PAULO, _form()))


@pytest.mark.parametrize("overrides", [
    {"value_cents": 0}, {"value_cents": 10_000_001},
    {"description": ""}, {"description": "x" * 201},
])
def test_form_limits(overrides):
    with pytest.raises(ValueError):
        _form(**overrides)


@pytest.mark.parametrize("days", [-1, 366])
async def test_due_date_limits(world, days):
    # The office's today, as billing reads it: the runner's own date is the
    # next day in UTC from 21h in Belém, and "yesterday" would be today.
    due = billing._today() + timedelta(days=days)
    await _refused(world, 422, billing.create(Conn(world), 7, PAULO, _form(due_date=due)),
                   "Vencimento")
    assert world.port.calls == []


async def test_summary_masks_and_bounds_the_dates(world, monkeypatch):
    world.customer = {"id": 3, "provider_id": "cus_1", "tax_id": CPF}
    monkeypatch.setattr(billing_repo, "charges_of", lambda conn, cid, limit=20: _value([]))

    found = await billing.summary(Conn(world), 7)

    today = billing._today()
    assert found["tax_id"] == "***.982.247-**"
    assert found["min_due"] == today.isoformat()
    assert found["default_due"] == (today + timedelta(days=3)).isoformat()
    assert found["max_due"] == (today + timedelta(days=365)).isoformat()


def _lost(**overrides):
    values: dict = dict(provider_id="pay_9", status="pendente", value_cents=1990, due_date=DUE,
                        invoice_url="https://x/i/9")
    values.update(overrides)
    return payments.Charge(**values)


async def test_the_charge_is_kept_before_asaas_is_asked(world):
    await billing.create(Conn(world), 7, PAULO, _form())

    assert world.events.index("commit") < world.events.index("create_charge")


async def test_a_lost_answer_is_recovered_by_reference(world):
    world.port.fail_charge = payments.PaymentsTransient("Asaas create_charge: ReadTimeout")
    world.port.lost = _lost()

    await billing.create(Conn(world), 7, PAULO, _form())

    [charge_id] = world.charges
    assert world.port.calls[-1] == "find_charge"
    assert world.charges[charge_id]["provider_id"] == "pay_9"
    assert world.charges[charge_id]["sent"] is True
    assert "https://x/i/9" in world.sent[0][1]


async def test_an_unconfirmed_charge_is_kept_for_later(world):
    world.port.fail_charge = payments.PaymentsTransient("Asaas create_charge: ReadTimeout")

    await _refused(world, 502, billing.create(Conn(world), 7, PAULO, _form()), "confirmar")

    [charge] = world.charges.values()
    assert charge["provider_id"] is None and charge["status"] == "pendente"
    assert "commit" in world.events and world.sent == []


async def test_a_refused_charge_is_closed_not_left_open(world):
    world.port.fail_charge = payments.PaymentsRejected("x", 400, ["invalid_dueDate"])

    await _refused(world, 422, billing.create(Conn(world), 7, PAULO, _form()), "invalid_dueDate")

    [charge] = world.charges.values()
    assert charge["status"] == "cancelado"
    assert world.events[-1] == "commit"


async def test_resend_confirms_an_unconfirmed_charge_first(world):
    world.charges[10] = {"id": 10, "provider_id": None, "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "Plano", "due_date": DUE,
                         "sent": False}
    world.port.lost = _lost()

    await billing.resend(Conn(world), 7, 10, PAULO)

    assert world.port.calls[:1] == ["find_charge"]
    assert world.charges[10]["provider_id"] == "pay_9" and world.charges[10]["sent"] is True


async def test_resend_of_a_charge_asaas_never_got_says_so(world):
    world.charges[10] = {"id": 10, "provider_id": None, "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "Plano", "due_date": DUE,
                         "sent": False}

    await _refused(world, 409, billing.resend(Conn(world), 7, 10, PAULO), "não chegou ao Asaas")
    assert world.sent == []


async def test_cancel_of_an_unconfirmed_charge(world):
    world.charges[10] = {"id": 10, "provider_id": None, "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "Plano", "due_date": DUE,
                         "sent": False}
    world.port.lost = _lost()

    await billing.cancel(Conn(world), 7, 10, PAULO)

    assert world.port.calls == ["find_charge", "get_charge", "cancel_charge"]
    assert world.charges[10]["status"] == "cancelado"


async def test_cancel_of_a_charge_asaas_never_got_closes_it_here(world):
    world.charges[10] = {"id": 10, "provider_id": None, "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "Plano", "due_date": DUE,
                         "sent": False}

    await billing.cancel(Conn(world), 7, 10, PAULO)

    assert world.port.calls == ["find_charge"]
    assert world.charges[10]["status"] == "cancelado"


async def test_resend_to_a_test_contact_in_production_is_refused(world, monkeypatch):
    monkeypatch.setattr(settings, "asaas_env", "production")
    world.contact = world.contact.model_copy(update={"test_mode": "novo"})
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente",
                         "method": "link", "value_cents": 1990, "description": "x",
                         "due_date": DUE, "sent": False}

    await _refused(world, 409, billing.resend(Conn(world), 7, 10, PAULO), "sandbox")
    assert world.port.calls == [] and world.sent == []


async def test_cancelling_what_asaas_already_cancelled_just_closes_it_here(world):
    world.port.status = "cancelado"
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "x", "due_date": DUE, "sent": True}

    await billing.cancel(Conn(world), 7, 10, PAULO)

    assert world.port.calls == ["get_charge"]          # nothing left to cancel there
    assert world.charges[10]["status"] == "cancelado"


async def test_cancelling_what_was_already_paid_is_refused(world):
    world.port.status = "pago"
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "x", "due_date": DUE, "sent": True}

    await _refused(world, 409, billing.cancel(Conn(world), 7, 10, PAULO), "não está mais em aberto")
    assert world.charges[10]["status"] == "pendente" and "cancel_charge" not in world.port.calls


async def test_a_charge_this_asaas_account_does_not_have_is_not_called_cancelled(world):
    world.port.missing = True      # another account or environment: nothing under that id
    world.charges[10] = {"id": 10, "provider_id": "pay_1", "status": "pendente", "method": "link",
                         "value_cents": 1990, "description": "x", "due_date": DUE, "sent": True}

    await _refused(world, 409, billing.cancel(Conn(world), 7, 10, PAULO), "Não encontrei esta cobrança")
    assert world.charges[10]["status"] == "pendente" and "cancel_charge" not in world.port.calls


# ── The charge by e-mail too ───────────────────────────────────────────────

class _Mailer:
    def __init__(self, fail: Exception | None = None):
        self.sent: list = []
        self.fail = fail

    async def send_message(self, mailbox, message):
        if self.fail is not None:
            raise self.fail
        self.sent.append((mailbox, message))
        return "<id@getconverge.me>"


@pytest.fixture
def mailer(monkeypatch):
    from core.services import billing as service
    box = _Mailer()
    monkeypatch.setattr(settings, "billing_emails", True)
    monkeypatch.setattr(service.workspace, "sender_mailbox", lambda: "team@getconverge.me")
    monkeypatch.setattr(service.workspace, "mail_sender", lambda: box)
    monkeypatch.setattr(service.workspace, "mail_sender_provider", lambda: "spacemail")
    return box


async def test_the_charge_also_goes_by_e_mail(world, mailer):
    await billing.create(Conn(world), 7, PAULO, _form())

    [(mailbox, message)] = mailer.sent
    assert mailbox == "team@getconverge.me" and message.to == ["ana@x.com"]
    assert message.subject == "Cobrança Converge: Plano mensal"
    assert message.body_text.startswith("Olá, Ana!")
    assert "https://x/i/1" in message.body_text and "R$ 19,90" in message.body_text
    [(_, text)] = world.sent
    assert text in message.body_text
    mail_audit = [a for a in world.audited if a["capability"] == "mail"]
    assert mail_audit == [{"provider": "spacemail", "capability": "mail",
                           "action": "billing_charge_email", "contact_id": 7,
                           "succeeded": True}]


async def test_no_e_mail_on_file_means_no_e_mail(world, mailer):
    world.contact = world.contact.model_copy(update={"email": None})
    await billing.create(Conn(world), 7, PAULO, _form())
    assert mailer.sent == [] and len(world.sent) == 1


async def test_off_by_default(world, mailer, monkeypatch):
    monkeypatch.setattr(settings, "billing_emails", False)
    await billing.create(Conn(world), 7, PAULO, _form())
    assert mailer.sent == [] and len(world.sent) == 1


async def test_a_failing_mailbox_never_stops_the_charge(world, mailer):
    mailer.fail = RuntimeError("smtp down")
    await billing.create(Conn(world), 7, PAULO, _form())
    [charge] = world.charges.values()
    assert charge["sent"] is True and len(world.sent) == 1
    assert [a["succeeded"] for a in world.audited if a["capability"] == "mail"] == [False]


async def test_resend_e_mails_the_current_link(world, mailer):
    world.customer = {"id": 3, "provider_id": "cus_1", "tax_id": CPF}
    world.delivery = "failed"
    with pytest.raises(inbox.InboxError):
        await billing.create(Conn(world), 7, PAULO, _form(tax_id=None))
    [charge_id] = world.charges
    world.delivery = "ok"
    await billing.resend(Conn(world), 7, charge_id, PAULO)
    # Only after the channel took it: the failed send above e-mailed nothing.
    [(_, message)] = mailer.sent
    assert "https://x/i/new" in message.body_text


async def test_a_test_contact_s_e_mail_says_test(world, mailer):
    world.contact = world.contact.model_copy(update={"test_mode": "novo"})
    await billing.create(Conn(world), 7, PAULO, _form())
    assert mailer.sent[0][1].subject == "[TESTE] Cobrança Converge: Plano mensal"


async def test_a_mailbox_that_hangs_gives_up(world, mailer, monkeypatch):
    from core.services import billing as service

    class Slow:
        async def send_message(self, mailbox, message):
            await asyncio.sleep(5)

    monkeypatch.setattr(service, "_EMAIL_SECONDS", 0.01)
    monkeypatch.setattr(service.workspace, "mail_sender", lambda: Slow())
    await billing.create(Conn(world), 7, PAULO, _form())
    assert [a["succeeded"] for a in world.audited if a["capability"] == "mail"] == [False]


# ── Past the 24h window, with the approved template ─────────────────────────

@pytest.fixture
def charge_template(world, monkeypatch: pytest.MonkeyPatch):
    """Ana on WhatsApp, quiet for more than 24h, and cobranca_enviada approved."""
    monkeypatch.setattr(settings, "whatsapp_charge_template", "cobranca_enviada")
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id", "whatsapp_access_token",
                 "whatsapp_webhook_verify_token"):
        monkeypatch.setattr(settings, name, "x")
    world.contact = world.contact.model_copy(update={"channel": "whatsapp",
                                                     "chat_id": "wa:5591999990000"})
    world.window = False
    world.delivery = "window_closed"
    world.templates = []

    async def send_template(chat_id, name, params, payloads=None):
        world.templates.append((chat_id, name, params, payloads))
        return "ok"

    monkeypatch.setattr(inbox, "send_template", send_template)
    return world


async def test_past_the_window_the_charge_goes_as_the_template(charge_template):
    await billing.create(Conn(charge_template), 7, PAULO, _form())

    assert charge_template.templates == [(
        "wa:5591999990000", "cobranca_enviada",
        ["Ana", "Plano mensal", "R$ 19,90", DUE.strftime("%d/%m/%Y"), "https://x/i/1"],
        ["reopen:charge"])]
    assert charge_template.charges[10]["sent"] is True
    assert charge_template.recorded[-1][1] == billing.charge_message(
        "link", "Plano mensal", 1990, DUE, "https://x/i/1")


async def test_resend_past_the_window_uses_the_current_link(charge_template):
    await billing.create(Conn(charge_template), 7, PAULO, _form())
    charge_template.templates.clear()

    await billing.resend(Conn(charge_template), 7, 10, PAULO)

    assert charge_template.templates[0][2][-1] == "https://x/i/new"


async def test_a_closed_local_window_goes_straight_to_the_template(charge_template):
    """Meta may take free text with a 200 and refuse it later (131047): our
    own clock decides, so the charge is never marked sent on a text that dies."""
    charge_template.delivery = "ok"

    await billing.create(Conn(charge_template), 7, PAULO, _form())

    assert charge_template.sent == [] and len(charge_template.templates) == 1
