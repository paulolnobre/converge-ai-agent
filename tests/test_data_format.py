"""The data format markers: written before the first worker, never lowered by
the app, and lowered only by cli.queue_unseal (Redis) after a restore."""
import asyncio
import json
import os
import uuid

import psycopg
import pytest

from core.config import settings
from core.integrations import update_queue
from core.services import data_format

DB = os.environ.get("TEST_DATABASE_URL")
REDIS = os.environ.get("TEST_REDIS_URL")


async def test_markers_are_raised_before_the_workers_start(monkeypatch):
    from core.web import app as web

    order = []

    async def nothing(*args, **kwargs):
        return None

    async def raise_markers():
        order.append("format")

    async def worker(*args, **kwargs):
        order.append("worker")

    monkeypatch.setattr(web, "validate_startup_config", lambda: None)
    monkeypatch.setattr(web.workspace, "configuration_report", lambda: {"status": "configured"})
    monkeypatch.setattr(web, "get_pool", nothing)
    monkeypatch.setattr(web, "refresh_never_deferred", nothing)
    monkeypatch.setattr(web.data_format, "raise_markers", raise_markers)
    for name in ("run_update_worker", "run_deferred_worker", "run_reminder_worker",
                 "run_handoff_timeout_worker", "run_briefing_worker", "run_proposal_worker",
                 "run_never_deferred_worker",
                 "run_media_cleanup_worker", "run_calendar_sync_worker"):
        monkeypatch.setattr(web, name, worker)
    monkeypatch.setattr(web, "close_pool", nothing)
    for module in (web.update_queue, web.cache, web.llm, web.telegram, web.whatsapp,
                   web.audio, web.workspace):
        monkeypatch.setattr(module, "close", nothing)

    async with web.lifespan(web.app):
        await asyncio.sleep(0)
        assert order[0] == "format" and order.count("worker") == 9


@pytest.mark.skipif(not (DB and REDIS), reason="TEST_DATABASE_URL and TEST_REDIS_URL not set")
async def test_markers_never_go_down(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", REDIS or "")
    await update_queue.close()
    client = update_queue._redis()
    key = f"t{uuid.uuid4().hex[:8]}:data_format"
    monkeypatch.setattr(data_format, "REDIS_KEY", key)
    conn = await psycopg.AsyncConnection.connect(DB or "")
    await conn.execute("SELECT 1")
    try:
        await data_format.raise_on(conn, client, 1)
        assert (await (await conn.execute("SELECT version FROM data_format")).fetchone()) == (1,)
        assert await client.get(key) == "1"
        await conn.execute("UPDATE data_format SET version = 2")
        await client.set(key, "2")
        await data_format.raise_on(conn, client, 1)
        assert (await (await conn.execute("SELECT version FROM data_format")).fetchone()) == (2,)
        assert await client.get(key) == "2"
        await data_format.raise_on(conn, client, 1)  # a second run adds no row
        assert (await (await conn.execute("SELECT count(*) FROM data_format")).fetchone()) == (1,)
    finally:
        await conn.rollback()
        await conn.close()
        await client.delete(key)
        await update_queue.close()


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def borrowed():
            yield self.conn
        return borrowed()


async def test_markers_go_to_2_with_capture_on_before_the_workers(monkeypatch):
    """Only capture creates media records, names and temporaries: with it on,
    format 2 is on record before anything can write one."""
    raised = []

    async def pool():
        return _Pool(object())

    async def raise_on(conn, client, version):
        raised.append(version)

    monkeypatch.setattr(data_format, "get_pool", pool)
    monkeypatch.setattr(data_format, "raise_on", raise_on)
    monkeypatch.setattr(data_format.update_queue, "_redis", lambda: object())
    monkeypatch.setattr(settings, "media_enabled", True)
    await data_format.raise_markers()
    monkeypatch.setattr(settings, "media_enabled", False)
    await data_format.raise_markers()
    assert raised == [2, 1]


def test_the_image_reads_format_2():
    assert data_format.IMAGE_FORMAT == 2


@pytest.mark.skipif(not (DB and REDIS), reason="TEST_DATABASE_URL and TEST_REDIS_URL not set")
async def test_markers_stay_at_least_1_and_keep_a_2_with_capture_off(monkeypatch):
    """Turning capture off never lowers a 2: the files and names it left are
    only gone after the procedure in docs/CRIPTOGRAFIA.md."""
    monkeypatch.setattr(settings, "redis_url", REDIS or "")
    monkeypatch.setattr(settings, "media_enabled", False)
    await update_queue.close()
    client = update_queue._redis()
    key = f"t{uuid.uuid4().hex[:8]}:data_format"
    monkeypatch.setattr(data_format, "REDIS_KEY", key)
    conn = await psycopg.AsyncConnection.connect(DB or "")
    monkeypatch.setattr(data_format, "get_pool", lambda: _ready(_Pool(conn)))
    try:
        await conn.execute("DELETE FROM data_format")
        await data_format.raise_markers()
        assert (await (await conn.execute("SELECT version FROM data_format")).fetchone()) == (1,)
        assert await client.get(key) == "1"
        monkeypatch.setattr(settings, "media_enabled", True)
        await data_format.raise_markers()
        monkeypatch.setattr(settings, "media_enabled", False)
        await data_format.raise_markers()
        assert (await (await conn.execute("SELECT version FROM data_format")).fetchone()) == (2,)
        assert await client.get(key) == "2"
    finally:
        await conn.rollback()
        await conn.close()
        await client.delete(key)
        await update_queue.close()


async def _ready(value):
    return value


async def test_the_worker_writes_its_heartbeat(monkeypatch):
    from core import agent

    beats = []

    async def heartbeat():
        beats.append(1)

    async def nothing(*, deferred):
        await asyncio.sleep(0.01)
        return None

    async def recover():
        return 0

    async def pool():
        return object()

    monkeypatch.setattr(update_queue, "heartbeat", heartbeat)
    monkeypatch.setattr(update_queue, "reserve_update", nothing)
    monkeypatch.setattr(update_queue, "recover_processing", recover)
    monkeypatch.setattr(agent, "get_pool", pool)
    monkeypatch.setattr(agent, "_HEARTBEAT", 0)
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run_update_worker(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert len(beats) >= 2


needs_redis = pytest.mark.skipif(not REDIS, reason="TEST_REDIS_URL not set")


@pytest.fixture
async def queues(monkeypatch):
    prefix = f"t{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "redis_url", REDIS or "")
    monkeypatch.setattr(settings, "cache_prefix", prefix)
    for name in ("active", "deferred", "processing", "dead_letter", "quarantine"):
        monkeypatch.setattr(settings, f"queue_{name}_name", f"{prefix}:queue:{name}")
    monkeypatch.setattr(data_format, "REDIS_KEY", f"{prefix}:data_format")
    await update_queue.close()
    client = update_queue._redis()
    yield client
    keys = await client.keys(f"{prefix}:*")
    if keys:
        await client.delete(*keys)
    await update_queue.close()


UPDATE = {"update_id": 5, "message": {"text": "oi"}}


@needs_redis
async def test_queue_unseal_rewrites_the_lists_as_json_and_then_drops_the_marker(queues):
    from cli import queue_unseal

    for name in (settings.queue_active_name, settings.queue_deferred_name,
                 settings.queue_dead_letter_name):
        await queues.lpush(name, update_queue.encode_update(UPDATE))
    await queues.set(data_format.REDIS_KEY, "1")
    assert await queue_unseal.main([]) == 0
    for name in (settings.queue_active_name, settings.queue_deferred_name,
                 settings.queue_dead_letter_name):
        assert [json.loads(i) for i in await queues.lrange(name, 0, -1)] == [UPDATE]
    assert await queues.get(data_format.REDIS_KEY) is None


@needs_redis
async def test_queue_unseal_refuses_while_the_bot_may_be_running(queues):
    from cli import queue_unseal

    await queues.lpush(settings.queue_deferred_name, update_queue.encode_update(UPDATE))
    await queues.set(data_format.REDIS_KEY, "1")
    await update_queue.heartbeat()
    assert await queue_unseal.main([]) != 0
    assert await queues.get(data_format.REDIS_KEY) == "1"
    item = (await queues.lrange(settings.queue_deferred_name, 0, -1))[0]
    assert not item.startswith("{")


@needs_redis
async def test_queue_unseal_recovers_what_a_stopped_bot_left_in_processing(queues):
    """Stopping the bot mid-turn leaves the item reserved; with no heartbeat it
    goes back to the active list, in JSON, instead of blocking the way back."""
    from cli import queue_unseal

    await queues.lpush(settings.queue_processing_name, update_queue.encode_update(UPDATE))
    await queues.set(data_format.REDIS_KEY, "1")
    assert await queue_unseal.main([]) == 0
    assert await queues.llen(settings.queue_processing_name) == 0
    assert [json.loads(i) for i in await queues.lrange(settings.queue_active_name, 0, -1)] == [UPDATE]
    assert await queues.get(data_format.REDIS_KEY) is None


@needs_redis
async def test_queue_unseal_quarantines_an_envelope_that_does_not_open(queues):
    from cli import queue_unseal

    good = update_queue.encode_update(UPDATE)
    await queues.lpush(settings.queue_deferred_name, good, good[:-4] + "AAAA")
    await queues.set(data_format.REDIS_KEY, "1")
    assert await queue_unseal.main([]) == 0
    assert [json.loads(i) for i in await queues.lrange(settings.queue_deferred_name, 0, -1)] == [UPDATE]
    held = await queues.lrange(settings.queue_quarantine_name, 0, -1)
    assert len(held) == 1 and not held[0].startswith("{")
    assert await queues.get(data_format.REDIS_KEY) is None


@needs_redis
async def test_queue_unseal_quarantines_broken_json_in_clear_or_sealed(queues):
    """The old worker parses JSON outside its per-item handling: a broken item
    it receives stops its loop. Those go to the quarantine instead."""
    from cli import queue_unseal

    sealed_broken = update_queue.seal_text("queue", "{broken")
    await queues.lpush(settings.queue_deferred_name, "{broken", sealed_broken, "[1, 2]",
                       update_queue.encode_update(UPDATE))
    await queues.set(data_format.REDIS_KEY, "1")
    assert await queue_unseal.main([]) == 0
    assert [json.loads(i) for i in await queues.lrange(settings.queue_deferred_name, 0, -1)] == [UPDATE]
    assert await queues.llen(settings.queue_quarantine_name) == 3


async def test_the_heartbeat_keeps_beating_during_a_long_turn(monkeypatch):
    """A turn can take 180 s and the heartbeat lasts 60: it is renewed by a
    task of its own, not between messages."""
    from core import agent

    beats = []

    async def heartbeat():
        beats.append(1)

    async def long_turn(*, deferred):
        await asyncio.sleep(0.2)   # the loop is busy for the whole turn
        return None

    async def recover():
        return 0

    async def pool():
        return object()

    monkeypatch.setattr(update_queue, "heartbeat", heartbeat)
    monkeypatch.setattr(update_queue, "reserve_update", long_turn)
    monkeypatch.setattr(update_queue, "recover_processing", recover)
    monkeypatch.setattr(agent, "get_pool", pool)
    monkeypatch.setattr(agent, "_HEARTBEAT", 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run_update_worker(stop))
    await asyncio.sleep(0.15)
    assert len(beats) >= 5
    stop.set()
    await task
    count = len(beats)
    await asyncio.sleep(0.05)
    assert len(beats) == count  # stops with the worker


@needs_redis
async def test_queue_unseal_loses_nothing_when_the_quarantine_write_fails(queues):
    """A quarantine key of the wrong type: the move fails, and the original
    items are still in their list."""
    from cli import queue_unseal

    good = update_queue.encode_update(UPDATE)
    await queues.lpush(settings.queue_deferred_name, good, "{broken")
    await queues.set(settings.queue_quarantine_name, "not a list")
    await queues.set(data_format.REDIS_KEY, "1")
    with pytest.raises(Exception):
        await queue_unseal.main([])
    assert await queues.lrange(settings.queue_deferred_name, 0, -1) == ["{broken", good]
    assert await queues.get(data_format.REDIS_KEY) == "1"


@needs_redis
async def test_queue_unseal_handles_a_large_list(queues):
    """Thousands of items: Lua's unpack has a stack limit, and a script that
    fails after removing the list would lose all of them."""
    from cli import queue_unseal

    items = [update_queue.encode_update({**UPDATE, "update_id": i}) for i in range(12_000)]
    for start in range(0, len(items), 1000):
        await queues.rpush(settings.queue_deferred_name, *items[start:start + 1000])
    await queues.rpush(settings.queue_deferred_name, "{broken")
    await queues.set(data_format.REDIS_KEY, "1")
    assert await queue_unseal.main([]) == 0
    kept = await queues.lrange(settings.queue_deferred_name, 0, -1)
    assert len(kept) == 12_000 and json.loads(kept[-1])["update_id"] == 11_999
    assert await queues.llen(settings.queue_quarantine_name) == 1


async def test_staff_uploads_also_require_media_cleanup_format(monkeypatch):
    monkeypatch.setattr(settings,'media_enabled',False)
    monkeypatch.setattr(settings,'panel_uploads_enabled',True)
    assert data_format.startup_format()==2
