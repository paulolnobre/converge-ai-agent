"""The Redis queues hold envelopes, never a customer's message in clear.

The queue tests run against a real Redis (Lua scripts and WATCH are the point)
and are skipped unless TEST_REDIS_URL points at a throwaway one. The worker and
/ready tests stub the queue and always run."""
import asyncio
import json
import os
import uuid

import pytest

from core import agent, crypto
from core.config import settings
from core.integrations import update_queue
from core.integrations.update_queue import QueueItemError

REDIS = os.environ.get("TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS, reason="TEST_REDIS_URL not set")
UPDATE = {"update_id": 7, "message": {"chat": {"id": "wa:5591988887777"},
                                      "text": "quero agendar com a Dra. Ana"}}


@pytest.fixture
async def redis_queue(monkeypatch: pytest.MonkeyPatch):
    """Queue names of their own, on the throwaway Redis, for each test."""
    prefix = f"t{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "redis_url", REDIS or "")
    monkeypatch.setattr(settings, "cache_prefix", prefix)
    for name in ("active", "deferred", "processing", "dead_letter", "quarantine"):
        monkeypatch.setattr(settings, f"queue_{name}_name", f"{prefix}:queue:{name}")
    update_queue._pending.clear()
    await update_queue.close()
    client = update_queue._redis()
    yield client
    keys = await client.keys(f"{prefix}:*")
    if keys:
        await client.delete(*keys)
    await update_queue.close()


def _clear_text_in(items: list[str]) -> bool:
    return any("Dra. Ana" in item for item in items)


@needs_redis
async def test_an_enqueued_payload_holds_no_clear_text(redis_queue):
    assert await update_queue.enqueue_update(UPDATE, deferred=False)
    items = await redis_queue.lrange(settings.queue_active_name, 0, -1)
    assert len(items) == 1 and not _clear_text_in(items)
    assert update_queue.decode_update(items[0]) == UPDATE


def test_an_old_json_payload_is_still_read():
    assert update_queue.decode_update(json.dumps(UPDATE)) == UPDATE


@pytest.mark.parametrize("payload, reason", [
    ("not json and not an envelope", "ilegível"),
    ("{not json", "json inválido"),
    ("[1, 2]", "não é um objeto"),
])
def test_an_unreadable_item_raises_with_a_reason_and_no_content(payload, reason):
    with pytest.raises(QueueItemError) as caught:
        update_queue.decode_update(payload)
    assert reason in caught.value.reason and payload not in str(caught.value)


def test_a_tampered_envelope_is_unreadable():
    sealed = update_queue.encode_update(UPDATE)
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(QueueItemError):
        update_queue.decode_update(tampered)


@needs_redis
async def test_quarantine_moves_the_item_sealed_and_atomically(redis_queue):
    original = "{invalid json with Dra. Ana"
    await redis_queue.lpush(settings.queue_processing_name, original)
    assert await update_queue.quarantine(original, "json inválido")
    assert await redis_queue.llen(settings.queue_processing_name) == 0
    held = await redis_queue.lrange(settings.queue_quarantine_name, 0, -1)
    assert len(held) == 1 and not _clear_text_in(held)
    opened = json.loads(crypto.open_text("queue.quarantine", held[0]))
    assert opened["payload"] == original and opened["reason"] == "json inválido"


@needs_redis
async def test_a_failed_quarantine_write_keeps_the_item_and_is_retried(redis_queue, monkeypatch):
    original = update_queue.encode_update(UPDATE)[:-4] + "AAAA"
    await redis_queue.lpush(settings.queue_processing_name, original)
    real_eval = redis_queue.eval

    async def out_of_memory(*args, **kwargs):
        from redis.exceptions import ResponseError
        raise ResponseError("OOM command not allowed when used memory > 'maxmemory'.")

    monkeypatch.setattr(redis_queue, "eval", out_of_memory)
    await update_queue.hold_for_quarantine(original, "ilegível")
    assert await redis_queue.llen(settings.queue_processing_name) == 1
    assert update_queue.pending_quarantine() == 1

    monkeypatch.setattr(redis_queue, "eval", real_eval)
    assert await update_queue.retry_pending_quarantine() == 1
    assert update_queue.pending_quarantine() == 0
    assert await redis_queue.llen(settings.queue_processing_name) == 0
    assert await redis_queue.llen(settings.queue_quarantine_name) == 1


@needs_redis
async def test_stored_lists_are_sealed_and_a_concurrent_change_is_retried(redis_queue,
                                                                          monkeypatch):
    for list_name in (settings.queue_deferred_name, settings.queue_dead_letter_name):
        await redis_queue.lpush(list_name, json.dumps(UPDATE), json.dumps({**UPDATE,
                                                                          "update_id": 8}))
    interfered = {"done": False}
    original_pipeline = redis_queue.pipeline

    def pipeline(*args, **kwargs):
        """The first transaction finds the list changed under it."""
        pipe = original_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        async def execute(*a, **k):
            if not interfered["done"]:
                interfered["done"] = True
                await update_queue._redis().lpush(settings.queue_deferred_name,
                                                  json.dumps({**UPDATE, "update_id": 9}))
            return await real_execute(*a, **k)

        pipe.execute = execute
        return pipe

    monkeypatch.setattr(redis_queue, "pipeline", pipeline)
    await update_queue.seal_stored_lists()
    deferred = await redis_queue.lrange(settings.queue_deferred_name, 0, -1)
    assert len(deferred) == 3 and not _clear_text_in(deferred)
    assert {update_queue.decode_update(i)["update_id"] for i in deferred} == {7, 8, 9}
    dead = await redis_queue.lrange(settings.queue_dead_letter_name, 0, -1)
    assert len(dead) == 2 and not _clear_text_in(dead)


@needs_redis
async def test_check_lists_reports_clear_text_and_wrong_key_versions(redis_queue, monkeypatch):
    await redis_queue.lpush(settings.queue_deferred_name, json.dumps(UPDATE))
    problems = await update_queue.check_lists(settle_seconds=0)
    assert any("deferred" in p and "JSON puro" in p for p in problems)
    assert not any("Dra. Ana" in p for p in problems)
    await update_queue.seal_stored_lists()
    assert await update_queue.check_lists(settle_seconds=0) == []
    assert any("versão" in p for p in await update_queue.check_lists(key_version=2,
                                                                      settle_seconds=0))


@needs_redis
async def test_rekey_reseals_the_lists_and_the_quarantine(redis_queue, monkeypatch):
    import base64

    await redis_queue.lpush(settings.queue_deferred_name, update_queue.encode_update(UPDATE))
    await redis_queue.lpush(settings.queue_processing_name, "{bad")
    assert await update_queue.quarantine("{bad", "json inválido")
    second = base64.b64encode(bytes(range(64, 96))).decode()
    monkeypatch.setattr(settings, "data_keys", f"2:{second},{settings.data_keys}")
    crypto.reset_keys()
    assert await update_queue.seal_stored_lists(rekey=True) == 2
    assert await update_queue.check_lists(key_version=2, settle_seconds=0) == []


@needs_redis
async def test_the_quarantine_listing_shows_reason_and_size_never_content(redis_queue, capsys):
    from cli import queue_quarantine

    await redis_queue.lpush(settings.queue_processing_name, "{Dra. Ana")
    assert await update_queue.quarantine("{Dra. Ana", "json inválido")
    assert await queue_quarantine.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "json inválido" in out and "Dra. Ana" not in out
    first_id = (await update_queue.quarantine_items())[0]["id"]
    # Something else is quarantined between the listing and the drop: the
    # position of the first item changes, its id does not.
    await redis_queue.lpush(settings.queue_processing_name, "{outro")
    assert await update_queue.quarantine("{outro", "json inválido")
    assert await queue_quarantine.main(["drop", first_id]) == 0
    left = await update_queue.quarantine_items()
    assert len(left) == 1 and left[0]["id"] != first_id


# ── The worker and /ready (no Redis) ─────────────────────────────────────────

async def test_an_unreadable_item_goes_to_quarantine_and_the_worker_goes_on(monkeypatch):
    held, handled = [], []

    async def hold(payload, reason):
        held.append(reason)

    async def handle(conn, update):
        handled.append(update["update_id"])

    async def ack(payload):
        pass

    monkeypatch.setattr(update_queue, "hold_for_quarantine", hold)
    monkeypatch.setattr(update_queue, "acknowledge_update", ack)
    monkeypatch.setattr(agent, "process_message", handle)

    async def note(conn, update):
        pass

    from core.handlers import message as handler
    monkeypatch.setattr(handler, "note_inbound", note)

    from tests.test_agent import Pool
    pool = Pool()
    await agent._handle_one(pool, "garbage that is no envelope")
    await agent._handle_one(pool, update_queue.encode_update(UPDATE))
    assert len(held) == 1 and held[0].startswith("ilegível") and handled == [7]


async def test_ready_is_not_ready_when_the_worker_died_or_a_quarantine_is_pending(monkeypatch):
    from fastapi import HTTPException

    from core.web import app as web

    async def ok():
        return True

    class Pool:
        def connection(self):
            class Conn:
                async def __aenter__(self):
                    class C:
                        async def execute(self, sql):
                            return None
                    return C()

                async def __aexit__(self, *exc):
                    return False
            return Conn()

    async def pool():
        return Pool()

    monkeypatch.setattr(update_queue, "ping", ok)
    monkeypatch.setattr(web, "get_pool", pool)
    monkeypatch.setattr(agent, "worker_alive", lambda: False)
    with pytest.raises(HTTPException) as caught:
        await web.ready()
    assert "worker" in caught.value.detail

    monkeypatch.setattr(agent, "worker_alive", lambda: True)
    monkeypatch.setattr(update_queue, "pending_quarantine", lambda: 1)
    with pytest.raises(HTTPException) as caught:
        await web.ready()
    assert "quarentena pendente" in caught.value.detail

    monkeypatch.setattr(update_queue, "pending_quarantine", lambda: 0)
    assert (await web.ready())["status"] == "ready"


async def test_worker_alive_follows_the_update_worker_task(monkeypatch):
    async def never(*, deferred):
        await asyncio.sleep(3600)

    async def recover():
        return 0

    async def pool():
        return object()

    monkeypatch.setattr(update_queue, "reserve_update", never)
    monkeypatch.setattr(update_queue, "recover_processing", recover)
    monkeypatch.setattr(agent, "get_pool", pool)
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run_update_worker(stop))
    await asyncio.sleep(0.05)
    assert agent.worker_alive()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not agent.worker_alive()
