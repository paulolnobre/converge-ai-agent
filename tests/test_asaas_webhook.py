"""The authenticated endpoint persists work before acknowledging Asaas."""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from core import agent
from core.config import settings
from core.integrations import update_queue
from core.services import billing
from core.web import app as web

PAYLOAD = {"id": "evt_1", "event": "PAYMENT_RECEIVED", "payment": {"id": "pay_1"}}


@pytest.fixture
def enqueue(monkeypatch):
    monkeypatch.setattr(settings, "asaas_webhook_token", "webhook-test-token")
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(update_queue, "enqueue_update", mock)
    monkeypatch.setattr(web, "is_open", lambda: False)
    return mock


async def post(payload=PAYLOAD, token="webhook-test-token", raw=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://test") as client:
        headers = {} if token is None else {"asaas-access-token": token}
        return await client.post("/asaas/webhook", headers=headers,
            **({"content": raw} if raw is not None else {"json": payload}))


async def test_disabled_without_token(enqueue, monkeypatch):
    monkeypatch.setattr(settings, "asaas_webhook_token", "")
    assert (await post()).status_code == 404
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_invalid_auth_does_not_queue(enqueue, token):
    assert (await post(token=token)).status_code == 401
    enqueue.assert_not_awaited()


async def test_non_ascii_token_is_refused_without_crashing(enqueue):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://test") as client:
        response = await client.post("/asaas/webhook", json=PAYLOAD,
            headers={b"asaas-access-token": "çã".encode("latin-1")})
    assert response.status_code == 401
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("payload", [[], {}, {"event": None}, {"event": ""}, {"event": 1}])
async def test_bad_payload(enqueue, payload):
    assert (await post(payload)).status_code == 400
    enqueue.assert_not_awaited()


async def test_invalid_json(enqueue):
    assert (await post(raw=b"not-json")).status_code == 400
    enqueue.assert_not_awaited()


async def test_event_queued_immediately_even_after_hours(enqueue):
    response = await post()
    assert response.status_code == 200 and response.json() == {"ok": True, "queued": True}
    enqueue.assert_awaited_once_with({"update_id": "asaas:evt_1", "asaas_event": PAYLOAD},
                                    deferred=False)
    enqueue.return_value = False
    assert (await post()).json() == {"ok": True, "queued": False}


async def test_idless_event_uses_event_and_payment_id(enqueue):
    payload = {k: v for k, v in PAYLOAD.items() if k != "id"}
    assert (await post(payload)).status_code == 200
    assert enqueue.call_args.args[0]["update_id"] == "asaas:PAYMENT_RECEIVED:pay_1"


@pytest.mark.parametrize("payload", [
    {**PAYLOAD, "id": []}, {"event": "PAYMENT_RECEIVED"},
    {"event": "PAYMENT_RECEIVED", "payment": {"id": []}},
])
async def test_missing_or_malformed_identity_is_400(enqueue, payload):
    assert (await post(payload)).status_code == 400
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("exc", [RuntimeError("redis down"), ValueError("cannot seal")])
async def test_queue_failure_is_retryable(enqueue, exc):
    enqueue.side_effect = exc
    assert (await post()).status_code == 503


async def test_worker_routes_payment_without_marking_customer_inbound(monkeypatch):
    payload = json.dumps({"update_id": "asaas:evt_1", "asaas_event": PAYLOAD})
    handle = AsyncMock(return_value="applied")
    note = AsyncMock()
    process = AsyncMock()
    ack = AsyncMock()
    retry = AsyncMock()
    conn = object()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield conn

    monkeypatch.setattr(billing, "handle_event", handle)
    monkeypatch.setattr(agent.handler, "note_inbound", note)
    monkeypatch.setattr(agent, "process_message", process)
    monkeypatch.setattr(update_queue, "acknowledge_update", ack)
    monkeypatch.setattr(update_queue, "retry_update", retry)
    await agent._handle_one(Pool(), payload)
    handle.assert_awaited_once_with(conn, PAYLOAD)
    note.assert_not_awaited()
    process.assert_not_awaited()
    ack.assert_awaited_once_with(payload)
    retry.assert_not_awaited()


async def test_worker_retries_database_failure_without_acknowledging(monkeypatch):
    payload = json.dumps({"update_id": "asaas:evt_1", "asaas_event": PAYLOAD})
    handle = AsyncMock(side_effect=RuntimeError("database unavailable"))
    ack, retry, note = AsyncMock(), AsyncMock(), AsyncMock()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield object()

    monkeypatch.setattr(billing, "handle_event", handle)
    monkeypatch.setattr(agent.handler, "note_inbound", note)
    monkeypatch.setattr(update_queue, "acknowledge_update", ack)
    monkeypatch.setattr(update_queue, "retry_update", retry)
    await agent._handle_one(Pool(), payload)
    ack.assert_not_awaited()
    note.assert_not_awaited()
    retry.assert_awaited_once_with(payload, "asaas:evt_1")
