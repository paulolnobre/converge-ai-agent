"""Meta's webhook: signed, verified once, and queued like a Telegram update —
the same out-of-hours rule, and a 503 so Meta retries when the queue is down."""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.integrations import update_queue, whatsapp
from core.web import app as web

NUMBER = "5591999990000"


def _payload(*messages: dict, statuses: list | None = None) -> dict:
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "PN1"},
        "contacts": [{"profile": {"name": "Ana"}, "wa_id": NUMBER}],
    }
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "W", "changes": [{"field": "messages", "value": value}]}]}


def _text(mid: str = "wamid.1", body: str = "oi") -> dict:
    return {"from": NUMBER, "id": mid, "timestamp": str(int(time.time())), "type": "text",
            "text": {"body": body}}


class Hook:
    def __init__(self, client: TestClient):
        self.client = client
        self.queued: list[tuple[dict, bool]] = []
        self.texts: list[tuple[str, str]] = []
        self.new = True
        self.queue_down = False

    def post(self, body: dict, secret: str = "s3cret"):
        raw = json.dumps(body).encode()
        signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post("/whatsapp/webhook", content=raw, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": signature,
        })


@pytest.fixture
def hook(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "whatsapp_app_secret", "s3cret")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PN1")
    monkeypatch.setattr(settings, "whatsapp_access_token", "t")
    monkeypatch.setattr(settings, "whatsapp_webhook_verify_token", "vt")
    state = Hook(TestClient(web.app))

    async def enqueue_update(update, *, deferred):
        if state.queue_down:
            raise RuntimeError("Update queue unavailable")
        state.queued.append((update, deferred))
        return state.new

    async def send_text(to, text):
        state.texts.append((to, text))
        return "ok"

    monkeypatch.setattr(update_queue, "enqueue_update", enqueue_update)
    monkeypatch.setattr(whatsapp, "send_text", send_text)
    monkeypatch.setattr(web, "is_open", lambda: True)
    return state


def test_meta_verification_handshake(hook, monkeypatch: pytest.MonkeyPatch):
    query = {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "123"}

    ok = hook.client.get("/whatsapp/webhook", params=query)
    assert ok.status_code == 200 and ok.text == "123"

    wrong = hook.client.get("/whatsapp/webhook", params={**query, "hub.verify_token": "no"})
    assert wrong.status_code == 403

    monkeypatch.setattr(settings, "whatsapp_access_token", None)
    assert hook.client.get("/whatsapp/webhook", params=query).status_code == 503


def test_a_bad_signature_queues_nothing(hook):
    assert hook.post(_payload(_text()), secret="wrong").status_code == 401
    assert hook.queued == []


def test_not_configured_is_503(hook, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "whatsapp_access_token", None)

    assert hook.post(_payload(_text())).status_code == 503
    assert hook.queued == []


def test_a_text_is_queued_as_a_whatsapp_update(hook):
    response = hook.post(_payload(_text()))

    assert response.status_code == 200 and response.json()["queued"] == 1
    update, deferred = hook.queued[0]
    assert update["message"]["chat"]["id"] == f"wa:{NUMBER}"
    assert deferred is False
    assert hook.texts == []


def test_out_of_hours_on_whatsapp(hook, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web, "is_open", lambda: False)

    hook.post(_payload(_text()))
    assert hook.queued[0][1] is True
    assert hook.texts == [(NUMBER, web.out_of_hours_message())]

    hook.new = False  # Meta delivering the same message again
    hook.post(_payload(_text()))
    assert len(hook.texts) == 1


def test_queue_down_is_503_so_meta_retries(hook):
    hook.queue_down = True

    assert hook.post(_payload(_text())).status_code == 503


def test_statuses_only_is_200_with_nothing_queued(hook):
    failed = [{"id": "wamid.9", "status": "failed", "errors": [{"code": 131026}]}]

    response = hook.post(_payload(statuses=failed))

    assert response.status_code == 200 and response.json()["queued"] == 0
    assert hook.queued == []


def test_invalid_json_is_400(hook):
    raw = b"not json"
    signature = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()

    response = hook.client.post("/whatsapp/webhook", content=raw,
                                headers={"X-Hub-Signature-256": signature})

    assert response.status_code == 400


def test_odd_characters_are_refused_not_a_crash(hook):
    """Anyone can call these routes: a token or signature with accents must be
    a plain refusal, not a server error."""
    query = {"hub.mode": "subscribe", "hub.verify_token": "çã", "hub.challenge": "1"}
    assert hook.client.get("/whatsapp/webhook", params=query).status_code == 403

    response = hook.client.post("/whatsapp/webhook", content=b"{}",
                                headers={"X-Hub-Signature-256": "sha256=çã".encode("latin-1")})
    assert response.status_code == 401



def test_an_old_whatsapp_message_gets_no_out_of_hours_notice(hook, monkeypatch: pytest.MonkeyPatch):
    """Delivered by Meta more than 24h after it was written: still queued, but
    no free-form notice, which Meta would refuse."""
    monkeypatch.setattr(web, "is_open", lambda: False)
    old = str(int(time.time()) - 30 * 3600)
    tap = {"from": NUMBER, "id": "wamid.t", "timestamp": old, "type": "interactive",
           "interactive": {"type": "button_reply", "button_reply": {"id": "x", "title": "X"}}}

    hook.post(_payload({**_text("wamid.o"), "timestamp": old}, tap))

    assert len(hook.queued) == 2 and all(deferred for _, deferred in hook.queued)
    assert hook.texts == []
