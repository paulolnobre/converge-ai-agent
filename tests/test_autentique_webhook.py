"""Autentique's webhook: only a body signed with the secret is queued, and the
queue keeps the event, the document and the signature, never the signers'
personal data."""
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from core.config import settings
from core.integrations import update_queue
from core.services import contract_signed
from core.web import app as web

SECRET = "webhook-secret"
SIGNED = {"id": "wh", "object": "webhook", "event": {
    "id": "9488ce11-70bf-4db7-858b-d76f1dd290dc", "type": "signature.accepted",
    "data": {"public_id": "f8911dcd-dfcd-11ef-9465-42010a2b610e", "object": "signature",
             "user": {"name": "Ana", "email": "ana@cliente.com", "cpf": "52998224725"},
             "document": "f48a8b465d02dd87559e08f06c41e3b6d548c4d7ad835eb0f",
             "signed": "2025-01-31T12:22:30.000000Z"}}}
FINISHED = {"id": "wh", "event": {
    "id": "e2", "type": "document.finished", "created_at": "2025-01-31T12:30:00.5Z",
    "data": {"object": {"id": "doc1", "object": "document",
                        "signatures": [{"user": {"cpf": "52998224725"}}],
                        "files": {"signed": "https://painel.autentique.com.br/documentos/doc1/assinado.pdf"}}}}}


class _Pool:
    @asynccontextmanager
    async def connection(self):
        yield None


@pytest.fixture
def wait(monkeypatch):
    mock = AsyncMock(return_value=False)
    monkeypatch.setattr(contract_signed, "must_wait", mock)
    return mock


@pytest.fixture
def enqueue(monkeypatch, wait):
    monkeypatch.setattr(settings, "autentique_webhook_secret", SECRET)
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(update_queue, "enqueue_update", mock)
    monkeypatch.setattr(web, "get_pool", AsyncMock(return_value=_Pool()))
    return mock


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def post(payload=SIGNED, *, signature: str | None = None, raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-Autentique-Signature": signature if signature is not None else _sign(body)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                 base_url="http://test") as client:
        return await client.post("/autentique/webhook", content=body, headers=headers)


async def test_off_without_the_secret(enqueue, monkeypatch):
    monkeypatch.setattr(settings, "autentique_webhook_secret", "")
    assert (await post()).status_code == 404
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("signature", ["", "00" * 32, _sign(b"other body")])
async def test_a_wrong_signature_is_refused(enqueue, signature):
    assert (await post(signature=signature)).status_code == 401
    enqueue.assert_not_awaited()


async def test_signed_with_another_secret_is_refused(enqueue):
    body = json.dumps(SIGNED).encode()
    assert (await post(raw=body, signature=_sign(body, "other"))).status_code == 401


async def test_the_raw_body_is_what_is_signed(enqueue):
    # Spaced differently from a re-serialization: only the bytes sent count.
    body = json.dumps(SIGNED, indent=3).encode()
    assert (await post(raw=body)).status_code == 200
    enqueue.assert_awaited_once()


async def test_the_signature_is_queued_without_personal_data(enqueue):
    response = await post()

    assert response.status_code == 200 and response.json()["queued"] is True
    queued = enqueue.await_args.args[0]
    assert queued == {"update_id": "autentique:9488ce11-70bf-4db7-858b-d76f1dd290dc",
                      "autentique_event": {
                          "id": "9488ce11-70bf-4db7-858b-d76f1dd290dc",
                          "type": "signature.accepted",
                          "document": "f48a8b465d02dd87559e08f06c41e3b6d548c4d7ad835eb0f",
                          "signature": "f8911dcd-dfcd-11ef-9465-42010a2b610e",
                          "at": "2025-01-31T12:22:30.000000Z"}}
    assert "52998224725" not in json.dumps(queued) and "ana@" not in json.dumps(queued)


async def test_the_finished_document_keeps_the_signed_copy(enqueue):
    await post(FINISHED)

    assert enqueue.await_args.args[0]["autentique_event"] == {
        "id": "e2", "type": "document.finished", "document": "doc1",
        "file": "https://painel.autentique.com.br/documentos/doc1/assinado.pdf",
        "at": "2025-01-31T12:30:00.5Z"}


async def test_a_signed_copy_elsewhere_is_not_kept(enqueue):
    payload = json.loads(json.dumps(FINISHED))
    payload["event"]["data"]["object"]["files"]["signed"] = "https://evil.example/x.pdf"
    await post(payload)
    assert enqueue.await_args.args[0]["autentique_event"]["file"] is None


async def test_other_events_are_acknowledged_and_not_queued(enqueue):
    payload = {"event": {"id": "e3", "type": "signature.viewed", "data": {}}}
    response = await post(payload)
    assert response.status_code == 200 and response.json()["queued"] is False
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", json.dumps(
    {"event": {"id": "e", "type": "document.finished", "data": {"object": {}}}}).encode()])
async def test_not_an_event_is_refused(enqueue, raw):
    assert (await post(raw=raw)).status_code == 400
    enqueue.assert_not_awaited()


async def test_a_queue_failure_asks_autentique_to_retry(enqueue):
    enqueue.side_effect = RuntimeError("down")
    assert (await post()).status_code == 503


async def test_a_document_still_being_sent_asks_autentique_to_try_again(enqueue, wait):
    wait.return_value = True
    assert (await post()).status_code == 503
    wait.assert_awaited_once_with(None, "f48a8b465d02dd87559e08f06c41e3b6d548c4d7ad835eb0f")
    enqueue.assert_not_awaited()


async def test_the_database_down_asks_autentique_to_try_again(enqueue, wait):
    wait.side_effect = RuntimeError("db down")
    assert (await post()).status_code == 503
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("signed", ["ontem", "2025-01-31T12:22:30", 5, "9" * 50])
def test_a_time_that_is_not_one_is_left_out(signed):
    payload = {"event": {"id": "e", "type": "signature.accepted", "data": {
        "public_id": "sig-1", "document": "doc-1", "signed": signed}}}
    assert contract_signed.slim(payload)["at"] is None  # type: ignore[index]


def test_the_signature_is_also_found_under_data_object():
    payload = {"event": {"id": "e", "type": "signature.accepted", "data": {"object": {
        "public_id": "sig-1", "document": {"id": "doc-1"}}}}}
    assert contract_signed.slim(payload) == {
        "id": "e", "type": "signature.accepted", "document": "doc-1", "signature": "sig-1",
        "at": None}
