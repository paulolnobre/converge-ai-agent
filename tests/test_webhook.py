"""The Telegram webhook: a tap is acknowledged the moment it arrives.

Behind a slow model call in the queue, a tapped button kept its loading shimmer
for seconds; acknowledging in the webhook ends it at once.
"""
import pytest
from fastapi.testclient import TestClient

from core.integrations import telegram, update_queue
from core.web import app as web


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch):
    acked: list[str] = []
    queued: list[dict] = []

    async def answer_callback_query(callback_id, text=None, base_url=None):
        acked.append(callback_id)
        return True

    async def enqueue_update(update, *, deferred):
        queued.append(update)
        return True

    monkeypatch.setattr(telegram, "answer_callback_query", answer_callback_query)
    monkeypatch.setattr(update_queue, "enqueue_update", enqueue_update)
    monkeypatch.setattr(web, "_valid_secret", lambda received: True)
    monkeypatch.setattr(web, "is_open", lambda: True)
    # No `with`: the lifespan (database, Redis, workers) never starts.
    return TestClient(web.app), acked, queued


def test_a_tap_is_acknowledged_before_it_is_queued(webhook):
    client, acked, queued = webhook
    update = {"update_id": 1, "callback_query": {
        "id": "cb-1", "data": "period:any:none:none",
        "message": {"chat": {"id": 42, "type": "private"}},
    }}

    assert client.post("/telegram/webhook", json=update).status_code == 200

    assert acked == ["cb-1"]
    assert queued[0]["callback_query"]["_acked"] is True


def test_a_message_is_not_acknowledged_as_a_tap(webhook):
    client, acked, queued = webhook
    update = {"update_id": 2, "message": {"chat": {"id": 42, "type": "private"}, "text": "oi"}}

    assert client.post("/telegram/webhook", json=update).status_code == 200

    assert acked == [] and len(queued) == 1
