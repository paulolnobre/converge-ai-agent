"""Payment updates, reordered/redelivered events and independent notifications."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from core.config import settings
from core.models import Contact
from core.services import billing


class Connection:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    @asynccontextmanager
    async def transaction(self):
        yield


def event(kind="PAYMENT_RECEIVED", event_id="evt_1", **payment):
    return {"id": event_id, "event": kind, "payment": {"id": "pay_1", **payment}}


@pytest.fixture
def state(monkeypatch):
    from core import channels
    from core.integrations import telegram
    from core.repositories import audit, contacts, inbox
    from core.services import push

    charge: dict = dict(id=7, provider_id="pay_1", status="pendente", description="Plano",
                  value_cents=1990, contact_id=42, chat_id="wa:5591999990000")
    seen = set()

    async def claim(conn, key):
        if key in seen:
            return False
        seen.add(key)
        return True

    async def paid(conn, charge_id):
        charge["status"] = "pago"

    async def status(conn, charge_id, value):
        charge["status"] = value

    repo = billing.billing_repo
    mocks = dict(claim_event=AsyncMock(side_effect=claim),
                 charge_for_event=AsyncMock(return_value=charge),
                 set_paid=AsyncMock(side_effect=paid), set_status=AsyncMock(side_effect=status),
                 set_provider=AsyncMock())
    for name, mock in mocks.items():
        monkeypatch.setattr(repo, name, mock)
    mocks.update(deliver=AsyncMock(return_value="ok"), record=AsyncMock(), audit=AsyncMock(),
                 team=AsyncMock(return_value=True), push=Mock(),
                 contact=AsyncMock(return_value=Contact(id=42, name="Ana",
                     chat_id=charge["chat_id"], channel="whatsapp")),
                 window=AsyncMock(return_value=True))
    monkeypatch.setattr(channels, "deliver", mocks["deliver"])
    monkeypatch.setattr(contacts, "get_by_id", mocks["contact"])
    monkeypatch.setattr(contacts, "record_message", mocks["record"])
    monkeypatch.setattr(inbox, "window_open", mocks["window"])
    monkeypatch.setattr(audit, "record", mocks["audit"])
    monkeypatch.setattr(telegram, "send_message", mocks["team"])
    monkeypatch.setattr(push, "schedule_team", mocks["push"])
    monkeypatch.setattr(settings, "team_chat_id", "-123")
    return charge, mocks, Connection()


@pytest.mark.parametrize("kind,status", [
    ("PAYMENT_RECEIVED", "pago"), ("PAYMENT_CONFIRMED", "pago"),
    ("PAYMENT_OVERDUE", "vencido"), ("PAYMENT_DELETED", "cancelado"),
    ("PAYMENT_REFUNDED", "estornado"),
])
async def test_event_updates_status_and_audits(state, kind, status):
    charge, mocks, conn = state
    assert await billing.handle_event(conn, event(kind)) == "applied"
    assert charge["status"] == status
    assert mocks["audit"].call_args.kwargs == dict(
        provider="asaas", capability="billing", action=f"webhook_{status}",
        target="pay_1", contact_id=42, succeeded=True)
    if status != "pago":
        mocks["deliver"].assert_not_awaited()
        mocks["team"].assert_not_awaited()


async def test_paid_customer_history_and_team(state):
    _, mocks, conn = state
    assert await billing.handle_event(conn, event()) == "applied"
    mocks["deliver"].assert_awaited_once_with("wa:5591999990000",
        "Recebemos o seu pagamento de R$ 19,90 (Plano). Obrigado!", parse_mode=None)
    assert mocks["record"].call_args.args[1:] == (42, "wa:5591999990000", "assistant",
        "Recebemos o seu pagamento de R$ 19,90 (Plano). Obrigado!", "billing")
    mocks["team"].assert_awaited_once_with("-123",
        "💰 Pagamento recebido: Ana · Plano · R$ 19,90", parse_mode=None)
    assert mocks["push"].call_args.kwargs["url"] == "/painel/#inbox/42"
    assert conn.commits >= 1


async def test_same_event_and_confirmation_after_receipt_send_once(state):
    _, mocks, conn = state
    assert await billing.handle_event(conn, event()) == "applied"
    assert await billing.handle_event(conn, event()) == "duplicate"
    assert await billing.handle_event(conn, event("PAYMENT_CONFIRMED", "evt_2")) == "stale"
    mocks["deliver"].assert_awaited_once()
    mocks["audit"].assert_awaited_once()


@pytest.mark.parametrize("status,kind", [
    ("pago", "PAYMENT_OVERDUE"), ("pago", "PAYMENT_DELETED"),
    ("cancelado", "PAYMENT_RECEIVED"), ("estornado", "PAYMENT_RECEIVED"),
    ("aguardando_aprovacao", "PAYMENT_RECEIVED"), ("recusada", "PAYMENT_OVERDUE"),
])
async def test_stale_events_cannot_regress_or_bypass_approval(state, status, kind):
    charge, mocks, conn = state
    charge["status"] = status
    assert await billing.handle_event(conn, event(kind)) == "stale"
    assert charge["status"] == status
    mocks["deliver"].assert_not_awaited()
    mocks["audit"].assert_not_awaited()


@pytest.mark.parametrize("status", ["vencido", "pago"])
async def test_refund_is_allowed_after_overdue_or_paid(state, status):
    charge, _, conn = state
    charge["status"] = status
    assert await billing.handle_event(conn, event("PAYMENT_REFUNDED")) == "applied"
    assert charge["status"] == "estornado"


async def test_lost_creation_response_recovers_provider_from_local_reference(state):
    charge, mocks, conn = state
    charge["provider_id"] = None
    assert await billing.handle_event(conn, event(externalReference="charge:7")) == "applied"
    mocks["charge_for_event"].assert_awaited_once_with(conn, provider_id="pay_1", local_id=7)
    mocks["set_provider"].assert_awaited_once_with(conn, 7, "pay_1", None)


@pytest.mark.parametrize("reference", [None, [], 7, "charge:-1", "charge:0", "charge:1;DROP",
    "charge:9223372036854775808", "charge:" + "9" * 100, " charge:7"])
async def test_malformed_local_reference_does_not_break_provider_lookup(state, reference):
    _, mocks, conn = state
    assert await billing.handle_event(conn, event(externalReference=reference)) == "applied"
    mocks["charge_for_event"].assert_awaited_once_with(conn, provider_id="pay_1", local_id=None)


async def test_nonlocal_charge_is_ignored_without_notices(state):
    _, mocks, conn = state
    mocks["charge_for_event"].return_value = None
    assert await billing.handle_event(conn, event()) == "unknown"
    mocks["deliver"].assert_not_awaited()
    mocks["audit"].assert_not_awaited()


@pytest.mark.parametrize("payload", [None, [], {}, {"event": 5},
    {"event": "PAYMENT_RECEIVED", "payment": []},
    {"event": "PAYMENT_RECEIVED", "payment": {"id": 4}},
    {"event": "PAYMENT_RECEIVED", "payment": {"id": ""}},
    event("PAYMENT_CREATED"), {**event(), "id": []}])
async def test_invalid_or_unhandled_event_does_not_claim_id(state, payload):
    _, mocks, conn = state
    assert await billing.handle_event(conn, payload) == "ignored"
    mocks["claim_event"].assert_not_awaited()


async def test_missing_event_id_has_stable_fallback(state):
    _, mocks, conn = state
    payload = event()
    del payload["id"]
    assert await billing.handle_event(conn, payload) == "applied"
    assert await billing.handle_event(conn, payload) == "duplicate"
    assert mocks["claim_event"].call_args.args[1] == "PAYMENT_RECEIVED:pay_1"


@pytest.mark.parametrize("provider_result", ["window_closed", "failed"])
async def test_rejected_customer_delivery_keeps_payment_and_notifies_team(state, provider_result):
    charge, mocks, conn = state
    mocks["deliver"].return_value = provider_result
    assert await billing.handle_event(conn, event()) == "applied"
    assert charge["status"] == "pago"
    mocks["record"].assert_not_awaited()
    mocks["team"].assert_awaited_once()


async def test_closed_local_whatsapp_window_sends_no_customer_message(state):
    charge, mocks, conn = state
    mocks["window"].return_value = False
    assert await billing.handle_event(conn, event()) == "applied"
    assert charge["status"] == "pago"
    mocks["deliver"].assert_not_awaited()
    mocks["record"].assert_not_awaited()
    mocks["team"].assert_awaited_once()


async def test_notification_exception_does_not_rollback_payment_or_block_push(state):
    charge, mocks, conn = state

    async def failed(*args, **kwargs):
        assert conn.commits >= 1, "payment is durable before any external send"
        raise RuntimeError("network unavailable")

    mocks["deliver"].side_effect = failed
    mocks["team"].side_effect = failed
    assert await billing.handle_event(conn, event()) == "applied"
    assert charge["status"] == "pago"
    mocks["push"].assert_called_once()
    assert await billing.handle_event(conn, event()) == "duplicate"


async def test_push_without_telegram_group(state, monkeypatch):
    _, mocks, conn = state
    monkeypatch.setattr(settings, "team_chat_id", "")
    await billing.handle_event(conn, event())
    mocks["team"].assert_not_awaited()
    mocks["push"].assert_called_once()


async def test_payment_received_is_also_e_mailed_once_even_with_the_window_closed(
        state, monkeypatch):
    _, mocks, conn = state
    sent = []

    class Mailer:
        async def send_message(self, mailbox, message):
            sent.append(message)

    monkeypatch.setattr(settings, "billing_emails", True)
    monkeypatch.setattr(billing.workspace, "sender_mailbox", lambda: "team@getconverge.me")
    monkeypatch.setattr(billing.workspace, "mail_sender", lambda: Mailer())
    monkeypatch.setattr(billing.workspace, "mail_sender_provider", lambda: "spacemail")
    mocks["contact"].return_value = Contact(id=42, name="Ana Souza", email="ana@x.com",
                                            chat_id="wa:5591999990000", channel="whatsapp")
    mocks["window"].return_value = False
    assert await billing.handle_event(conn, event()) == "applied"
    assert await billing.handle_event(conn, event()) == "duplicate"
    [message] = sent
    assert message.to == ["ana@x.com"] and message.subject == "Pagamento recebido: Plano"
    assert "Recebemos o seu pagamento de R$ 19,90 (Plano)" in message.body_text
    mocks["deliver"].assert_not_awaited()


async def test_closed_window_with_the_paid_template_thanks_by_template(state, monkeypatch):
    from core.services import inbox as inbox_service

    charge, mocks, conn = state
    mocks["window"].return_value = False
    sent = AsyncMock(return_value="ok")
    monkeypatch.setattr(settings, "whatsapp_paid_template", "pagamento_recebido")
    monkeypatch.setattr(settings, "whatsapp_paid_language", "en")
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id", "whatsapp_access_token",
                 "whatsapp_webhook_verify_token"):
        monkeypatch.setattr(settings, name, "x")
    monkeypatch.setattr(inbox_service, "send_template", sent)

    assert await billing.handle_event(conn, event()) == "applied"

    mocks["deliver"].assert_not_awaited()
    sent.assert_awaited_once_with("wa:5591999990000", "pagamento_recebido",
                                  ["Ana", "R$ 19,90", "Plano"], language="en")
    mocks["record"].assert_awaited_once()
