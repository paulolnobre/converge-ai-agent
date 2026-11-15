"""The update worker: what it keeps even when handling an update fails."""
from contextlib import asynccontextmanager

import pytest

from core import agent
from core.handlers import message as handler
from core.integrations import update_queue


class Pool:
    """Connections that say whether they ended normally (committed) or not."""

    def __init__(self):
        self.closed: list[tuple[int, bool]] = []

    @asynccontextmanager
    async def connection(self):
        conn = object()
        try:
            yield conn
        except BaseException:
            self.closed.append((id(conn), False))
            raise
        self.closed.append((id(conn), True))


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, list] = {"acked": [], "retried": []}

    async def acknowledge_update(payload):
        seen["acked"].append(payload)

    async def retry_update(payload, update_id):
        seen["retried"].append(update_id)
        return True

    monkeypatch.setattr(update_queue, "acknowledge_update", acknowledge_update)
    monkeypatch.setattr(update_queue, "retry_update", retry_update)
    return seen


async def test_when_the_customer_wrote_survives_a_failed_update(
    monkeypatch: pytest.MonkeyPatch, queue
):
    """The calendar failing mid-booking rolls the turn back — but the customer
    did write, and WhatsApp's window reopened: that is kept on its own."""
    noted: list = []

    async def note_inbound(conn, update):
        noted.append(id(conn))

    async def failing(conn, update):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(handler, "note_inbound", note_inbound)
    monkeypatch.setattr(agent, "process_callback", failing)
    pool = Pool()

    await agent._handle_one(pool, update_queue_payload())

    assert len(noted) == 1
    assert (noted[0], True) in pool.closed, "noted in a transaction of its own, committed"
    assert any(not ok for _, ok in pool.closed), "the turn itself rolled back"
    assert queue["retried"] == ["wa:wamid.1"]


def update_queue_payload() -> str:
    import json
    return json.dumps({"update_id": "wa:wamid.1", "callback_query": {
        "id": "wa:wamid.1", "data": "period:any:none:none", "date": 1,
        "message": {"chat": {"id": "wa:5591999990000", "type": "private"}}}})


async def test_note_inbound_reads_messages_and_taps(monkeypatch: pytest.MonkeyPatch):
    from core.repositories import contacts as contacts_repo
    calls: list = []

    async def get_or_create(conn, chat_id, name=None, inbound_at=None):
        calls.append((chat_id, inbound_at.timestamp() if inbound_at else None))

    monkeypatch.setattr(contacts_repo, "get_or_create", get_or_create)
    chat = {"id": "wa:5591999990000", "type": "private"}

    await handler.note_inbound(object(), {"message": {"chat": chat, "date": 1000, "text": "oi"}})
    await handler.note_inbound(object(), {"callback_query": {"date": 2000, "message": {"chat": chat}}})
    await handler.note_inbound(object(), {"message": {"chat": {"id": 42}, "date": 3000}})

    assert calls == [("wa:5591999990000", 1000), ("wa:5591999990000", 2000)]
