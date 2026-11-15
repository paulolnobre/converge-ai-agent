"""Real commits/locks: competing events send one receipt and survive redelivery."""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock, Mock

import httpx
import psycopg
import pytest

from core import agent, channels
from core.config import settings
from core.integrations import update_queue
from core.repositories import billing as repo
from core.services import billing, push
from core.web import app as web
from tests.test_queue_sealing import redis_queue  # noqa: F401

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def payment(monkeypatch):
    delivered = AsyncMock(return_value="ok")
    monkeypatch.setattr(channels, "deliver", delivered)
    monkeypatch.setattr(push, "schedule_team", Mock())
    unique = uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(URL or "") as conn:
        cur = await conn.execute(
            "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
            (unique[:16],))
        row = await cur.fetchone()
        assert row is not None
        contact_id = row[0]
        customer_id = await repo.save_customer(conn, contact_id, f"cus_{unique}", "52998224725")
        charge_id = await repo.insert_charge(conn, customer_id=customer_id, origin="painel",
            method="link", value_cents=1990, description="Plano", due_date=date(2026, 11, 5),
            status="pendente", requested_by=None)
        await repo.set_provider(conn, charge_id, f"pay_{unique}", None)
    payload = {"id": f"evt_{unique}", "event": "PAYMENT_RECEIVED",
               "payment": {"id": f"pay_{unique}"}}
    try:
        yield charge_id, payload, delivered
    finally:
        async with await psycopg.AsyncConnection.connect(URL or "") as conn:
            await conn.execute("DELETE FROM workspace_actions WHERE contact_id = %s", (contact_id,))
            await conn.execute("DELETE FROM sessions WHERE contact_id = %s", (contact_id,))
            await conn.execute("DELETE FROM billing_charges WHERE id = %s", (charge_id,))
            await conn.execute("DELETE FROM billing_customers WHERE id = %s", (customer_id,))
            await conn.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
            await conn.execute("DELETE FROM billing_events WHERE event_id = %s OR event_id = %s",
                               (payload["id"], payload["id"] + "-other"))


async def apply(payload):
    async with await psycopg.AsyncConnection.connect(URL or "") as conn:
        return await billing.handle_event(conn, payload)


async def test_same_event_concurrently_is_applied_once(payment):
    charge_id, payload, delivered = payment
    outcomes = await asyncio.wait_for(asyncio.gather(apply(payload), apply(payload)), timeout=10)
    assert sorted(outcomes) == ["applied", "duplicate"]
    delivered.assert_awaited_once()
    async with await psycopg.AsyncConnection.connect(URL or "") as conn:
        cur = await conn.execute("SELECT status, paid_at FROM billing_charges WHERE id = %s",
                                 (charge_id,))
        row = await cur.fetchone()
        assert row is not None
        status, stamp = row
        assert status == "pago" and stamp is not None
        cur = await conn.execute("SELECT COUNT(*) FROM workspace_actions WHERE action = %s "
                                 "AND target = %s", ("webhook_pago", payload["payment"]["id"]))
        row = await cur.fetchone()
        assert row is not None and row[0] == 1


async def test_distinct_paid_events_concurrently_send_one_receipt(payment):
    _, payload, delivered = payment
    confirmed = {**payload, "id": payload["id"] + "-other", "event": "PAYMENT_CONFIRMED"}
    outcomes = await asyncio.wait_for(asyncio.gather(apply(payload), apply(confirmed)), timeout=10)
    assert sorted(outcomes) == ["applied", "stale"]
    delivered.assert_awaited_once()


async def test_history_failure_does_not_lose_payment_or_repeat_receipt(payment, monkeypatch):
    from core.repositories import contacts

    charge_id, payload, delivered = payment

    async def failing_history(conn, *args, **kwargs):
        await conn.execute("SELECT 1/0")

    monkeypatch.setattr(contacts, "record_message", failing_history)
    assert await apply(payload) == "applied"
    assert await apply(payload) == "duplicate"
    delivered.assert_awaited_once()
    async with await psycopg.AsyncConnection.connect(URL or "") as conn:
        cur = await conn.execute("SELECT status FROM billing_charges WHERE id = %s", (charge_id,))
        row = await cur.fetchone()
        assert row is not None and row[0] == "pago"


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="TEST_REDIS_URL not set")
async def test_http_to_encrypted_queue_to_worker_and_database(
    payment, redis_queue, monkeypatch  # noqa: F811
):
    charge_id, payload, delivered = payment
    monkeypatch.setattr(settings, "asaas_webhook_token", "test-webhook-token")
    noted = AsyncMock()
    monkeypatch.setattr(agent.handler, "note_inbound", noted)

    class Pool:
        @asynccontextmanager
        async def connection(self):
            async with await psycopg.AsyncConnection.connect(URL or "") as conn:
                yield conn

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://test") as client:
        response = await client.post("/asaas/webhook", json=payload,
            headers={"asaas-access-token": "test-webhook-token"})
        assert response.status_code == 200 and response.json()["queued"] is True
        [sealed] = await redis_queue.lrange(settings.queue_active_name, 0, -1)
        assert payload["payment"]["id"] not in sealed
        reserved = await update_queue.reserve_update(deferred=False)
        assert reserved is not None
        await agent._handle_one(Pool(), reserved)
        assert await redis_queue.llen(settings.queue_processing_name) == 0
        assert await redis_queue.llen(settings.queue_active_name) == 0
        assert await redis_queue.llen(settings.queue_dead_letter_name) == 0
        delivered.assert_awaited_once()
        noted.assert_not_awaited()
        response = await client.post("/asaas/webhook", json=payload,
            headers={"asaas-access-token": "test-webhook-token"})
        assert response.status_code == 200 and response.json()["queued"] is False
    async with await psycopg.AsyncConnection.connect(URL or "") as conn:
        cur = await conn.execute("SELECT status FROM billing_charges WHERE id = %s", (charge_id,))
        row = await cur.fetchone()
        assert row is not None and row[0] == "pago"
        cur = await conn.execute("SELECT COUNT(*) FROM sessions WHERE contact_id = "
            "(SELECT contact_id FROM billing_customers WHERE id = "
            "(SELECT customer_id FROM billing_charges WHERE id = %s))", (charge_id,))
        assert await cur.fetchone() == (1,)
