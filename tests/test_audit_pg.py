"""The action audit against a real Postgres (TEST_DATABASE_URL). Billing writes
to it too: the table must accept it, and a refused row must never poison the
transaction of the operation it describes."""
import os
import uuid

import psycopg
import pytest

from core.repositories import audit

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _count(conn, provider: str) -> int:
    cur = await conn.execute("SELECT count(*) FROM workspace_actions WHERE provider = %s", (provider,))
    return (await cur.fetchone())[0]


async def test_a_billing_action_is_recorded(conn):
    await audit.record(conn, provider="asaas", capability="billing", action="create_charge",
                       target="pay_1", detail="user:1")

    assert await _count(conn, "asaas") == 1


@pytest.mark.parametrize("provider, capability", [("google", "calendar"), ("microsoft", "mail")])
async def test_the_workspace_actions_are_still_recorded(conn, provider, capability):
    await audit.record(conn, provider=provider, capability=capability, action="list")

    assert await _count(conn, provider) >= 1


async def test_a_billing_action_is_recorded_in_autocommit_mode_too():
    target = f"pay_{uuid.uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(URL or "", autocommit=True) as plain:
        await audit.record(plain, provider="asaas", capability="billing", action="create_charge",
                           target=target)
        found = await (await plain.execute(
            "SELECT count(*) FROM workspace_actions WHERE target = %s", (target,))).fetchone()
        await plain.execute("DELETE FROM workspace_actions WHERE target = %s", (target,))
    assert found == (1,)


async def test_a_row_the_table_refuses_does_not_abort_the_callers_transaction(conn, caplog):
    await conn.execute("INSERT INTO contacts (chat_id, source) VALUES ('audit-test', 'telegram')")

    await audit.record(conn, provider="nobody", capability="nothing", action="x")

    # The caller's earlier work is intact and the connection is still usable.
    cur = await conn.execute("SELECT count(*) FROM contacts WHERE chat_id = 'audit-test'")
    assert (await cur.fetchone())[0] == 1
    assert "Could not record a workspace action" in caplog.text




# ── The whole charge, against the real tables ───────────────────────────────

from datetime import date, timedelta  # noqa: E402

from core.config import settings  # noqa: E402
from core.integrations import payments  # noqa: E402
from core.services import billing, inbox  # noqa: E402


class _Port:
    def __init__(self):
        self.calls: list[str] = []
        self.tag = uuid.uuid4().hex[:10]  # the tests commit: provider ids must not repeat

    async def find_customer(self, external_ref):
        return None

    async def create_customer(self, customer):
        return payments.Customer(f"cus_{self.tag}", customer.external_ref)

    async def create_charge(self, charge):
        self.calls.append(charge.external_ref)
        return payments.Charge(f"pay_{self.tag}", "pendente", charge.value_cents, charge.due_date,
                               "https://pay.example/i/e2e")

    async def get_charge(self, provider_id):
        return payments.Charge(provider_id, "pendente", 1990, date.today(), "https://pay.example/i/e2e")

    async def cancel_charge(self, provider_id):
        return True


@pytest.fixture
async def desk(conn, monkeypatch: pytest.MonkeyPatch):
    """A contact in a conversation a panel user holds, and a fake Asaas and channel."""
    chat = f"e2e{uuid.uuid4().int % 10**9}"
    user = (await (await conn.execute(
        "INSERT INTO panel_users (email, name, active) VALUES (%s, 'Ana', TRUE) RETURNING id",
        (f"{uuid.uuid4().hex[:8]}@e2e.test",))).fetchone())[0]
    contact = (await (await conn.execute(
        "INSERT INTO contacts (chat_id, source, name) VALUES (%s, 'telegram', 'Cliente E2E') "
        "RETURNING id", (chat,))).fetchone())[0]
    await conn.execute(
        "INSERT INTO handoff_sessions (contact_id, chat_id, team_chat_id, reason, status, "
        "claimed_by_user_id, claimed_by_name, relay_chat_id) "
        "VALUES (%s, %s, 't', 'pedido', 'claimed', %s, 'Ana', 't')", (contact, chat, user))
    port, sent = _Port(), []

    async def send(channel, chat_id, text):
        sent.append(text)
        return "ok"

    monkeypatch.setattr(settings, "asaas_api_key", "$aact_hmlg_E2E")
    monkeypatch.setattr(payments, "payments", lambda: port)
    monkeypatch.setattr(inbox, "send", send)
    yield {"contact": contact, "user": {"user_id": user, "name": "Ana"}, "port": port, "sent": sent}
    # billing.create commits (the charge must outlive a failed send), so this
    # test's rows are removed by hand, in dependency order, even on failure.
    await conn.rollback()
    for sql in (
        "DELETE FROM workspace_actions WHERE contact_id = %s",
        "DELETE FROM billing_charges WHERE customer_id IN "
        "(SELECT id FROM billing_customers WHERE contact_id = %s)",
        "DELETE FROM billing_customers WHERE contact_id = %s",
        "DELETE FROM sessions WHERE contact_id = %s",
        "DELETE FROM handoff_sessions WHERE contact_id = %s",
        "DELETE FROM contacts WHERE id = %s",
    ):
        await conn.execute(sql, (contact,))
    await conn.execute("DELETE FROM panel_users WHERE id = %s", (user,))
    await conn.commit()


def _form() -> billing.ChargeForm:
    return billing.ChargeForm(description="Implantação", value_cents=130000,
                              due_date=date.today() + timedelta(days=2), method="link",
                              tax_id="529.982.247-25")


async def test_a_charge_is_created_sent_recorded_and_audited(conn, desk):
    await billing.create(conn, desk["contact"], desk["user"], _form())

    assert len(desk["sent"]) == 1 and "https://pay.example/i/e2e" in desk["sent"][0]
    (listed,) = (await billing.summary(conn, desk["contact"]))["charges"]
    assert listed["sent"] is True and listed["status"] == "pendente"
    history = await conn.execute(
        "SELECT count(*) FROM sessions WHERE contact_id = %s AND role = 'assistant'", (desk["contact"],))
    assert (await history.fetchone())[0] == 1
    audited = await conn.execute(
        "SELECT action, succeeded FROM workspace_actions WHERE provider = 'asaas' AND target = %s",
        (f"pay_{desk['port'].tag}",))
    assert await audited.fetchall() == [("create_charge", True)]


async def test_cancelling_keeps_the_new_status_and_is_audited(conn, desk):
    await billing.create(conn, desk["contact"], desk["user"], _form())
    (charge,) = (await billing.summary(conn, desk["contact"]))["charges"]

    await billing.cancel(conn, desk["contact"], charge["id"], desk["user"])

    (after,) = (await billing.summary(conn, desk["contact"]))["charges"]
    assert after["status"] == "cancelado"
    audited = await conn.execute(
        "SELECT action FROM workspace_actions WHERE provider = 'asaas' AND action = 'cancel_charge' "
        "AND target = %s", (f"pay_{desk['port'].tag}",))
    assert len(await audited.fetchall()) == 1
