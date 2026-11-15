"""The panel's billing routes: behind the session, off without the Asaas key,
the signed-in person passed to the service, and nothing that would let the
panel show the payment link or the full CPF/CNPJ."""
# ruff: noqa: F811
from datetime import date, timedelta

import pytest

from core.config import settings
from core.services import billing, inbox
from tests.test_panel import (  # noqa: F401
    _value,
    accounts,
    client,
    clock,
    inbox_store,
    signed_in,
)

DUE = (date.today() + timedelta(days=3)).isoformat()
FORM = {"description": "Plano mensal", "value_cents": 1990, "due_date": DUE,
        "method": "link", "tax_id": "529.982.247-25"}
SUMMARY = {"tax_id": "***.982.247-**", "default_due": DUE, "max_due": DUE,
           "charges": [{"id": 10, "method": "link", "value_cents": 1990, "description": "Plano",
                        "due_date": DUE, "status": "pendente", "sent": False,
                        "created_at": None}]}


@pytest.fixture
def billing_calls(monkeypatch, inbox_store):
    calls: list[tuple] = []
    monkeypatch.setattr(settings, "asaas_api_key", "$aact_hmlg_TEST")

    async def create(conn, contact_id, user, form):
        if inbox_store["refuse"]:
            raise inbox.InboxError(*inbox_store["refuse"])
        calls.append(("create", contact_id, user["user_id"], form.value_cents, form.method))

    async def resend(conn, contact_id, charge_id, user):
        calls.append(("resend", contact_id, charge_id, user["user_id"]))

    async def cancel(conn, contact_id, charge_id, user):
        calls.append(("cancel", contact_id, charge_id, user["user_id"]))

    monkeypatch.setattr(billing, "create", create)
    monkeypatch.setattr(billing, "resend", resend)
    monkeypatch.setattr(billing, "cancel", cancel)
    monkeypatch.setattr(billing, "summary", lambda conn, contact_id: _value(SUMMARY))
    return calls


def test_billing_needs_a_session(client, billing_calls):
    assert client.get("/painel/api/inbox/7/billing").status_code == 401
    assert client.post("/painel/api/inbox/7/billing", json=FORM).status_code == 401
    assert billing_calls == []


def test_billing_off_without_the_key(signed_in, billing_calls, monkeypatch):
    monkeypatch.setattr(settings, "asaas_api_key", "")

    assert signed_in.get("/painel/api/inbox/7/billing").status_code == 404
    assert signed_in.post("/painel/api/inbox/7/billing", json=FORM).status_code == 404
    assert signed_in.post("/painel/api/inbox/7/billing/10/resend").status_code == 404
    assert signed_in.post("/painel/api/inbox/7/billing/10/cancel").status_code == 404
    assert billing_calls == []
    assert signed_in.get("/painel/api/inbox/7").json()["contact"]["billing_enabled"] is False


def test_the_conversation_says_billing_is_on(signed_in, billing_calls):
    assert signed_in.get("/painel/api/inbox/7").json()["contact"]["billing_enabled"] is True


def test_actions_go_as_whoever_is_signed_in(signed_in, billing_calls):
    assert signed_in.post("/painel/api/inbox/7/billing", json=FORM).status_code == 200
    assert signed_in.post("/painel/api/inbox/7/billing/10/resend").status_code == 200
    assert signed_in.post("/painel/api/inbox/7/billing/10/cancel").status_code == 200

    assert billing_calls == [("create", 7, 1, 1990, "link"), ("resend", 7, 10, 1),
                             ("cancel", 7, 10, 1)]


def test_a_refusal_is_shown_as_it_is(signed_in, billing_calls, inbox_store):
    inbox_store["refuse"] = (409, "Victor assumiu esta conversa.")

    answer = signed_in.post("/painel/api/inbox/7/billing", json=FORM)

    assert answer.status_code == 409 and answer.json()["detail"] == "Victor assumiu esta conversa."


@pytest.mark.parametrize("change", [{"value_cents": 0}, {"method": "cartao"},
                                    {"description": ""}, {"due_date": "amanhã"}])
def test_an_invalid_form_is_422(signed_in, billing_calls, change):
    assert signed_in.post("/painel/api/inbox/7/billing", json={**FORM, **change}).status_code == 422
    assert billing_calls == []


def test_summary_never_carries_the_link_or_full_tax_id(signed_in, billing_calls):
    answer = signed_in.get("/painel/api/inbox/7/billing")

    assert answer.status_code == 200
    assert answer.json()["tax_id"] == "***.982.247-**"
    assert "invoice_url" not in answer.text and "52998224725" not in answer.text
