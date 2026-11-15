"""The media cleanup worker: it runs with capture off, records each pass in
/metrics, and after 24 h without a good pass says so at CRITICAL."""
import asyncio
import contextlib
import logging

from core.config import settings
from core.metrics import MEDIA_CLEANUP_LAST_SUCCESS, MEDIA_CLEANUP_RUNS
from core.services import media_cleanup


class _Pool:
    @contextlib.asynccontextmanager
    async def connection(self):
        yield object()


def _worker_with(monkeypatch, sweep):
    async def pool():
        return _Pool()

    monkeypatch.setattr(media_cleanup, "get_pool", pool)
    monkeypatch.setattr(media_cleanup, "sweep", sweep)


async def test_the_worker_runs_with_capture_off(monkeypatch):
    monkeypatch.setattr(settings, "media_enabled", False)
    stop = asyncio.Event()
    before = MEDIA_CLEANUP_RUNS.labels("ok")._value.get()

    async def sweep(conn):
        stop.set()
        return {"expired": 1, "errors": 0}

    _worker_with(monkeypatch, sweep)
    await asyncio.wait_for(media_cleanup.run_media_cleanup_worker(stop), timeout=2)
    assert MEDIA_CLEANUP_RUNS.labels("ok")._value.get() == before + 1
    assert MEDIA_CLEANUP_LAST_SUCCESS._value.get() > 0


async def test_a_day_without_a_good_pass_is_critical(monkeypatch, caplog):
    stop, now = asyncio.Event(), [1000.0]
    before = MEDIA_CLEANUP_RUNS.labels("error")._value.get()

    async def sweep(conn):
        stop.set()
        now[0] += 25 * 3600  # the worker started a day and an hour ago
        raise RuntimeError("disk")

    _worker_with(monkeypatch, sweep)
    monkeypatch.setattr(media_cleanup.time, "time", lambda: now[0])
    with caplog.at_level(logging.INFO, logger="core.services.media_cleanup"):
        await asyncio.wait_for(media_cleanup.run_media_cleanup_worker(stop), timeout=2)
    assert MEDIA_CLEANUP_RUNS.labels("error")._value.get() == before + 1
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


async def test_a_partial_pass_is_not_a_good_one(monkeypatch):
    stop = asyncio.Event()
    MEDIA_CLEANUP_LAST_SUCCESS.set(0)
    before = MEDIA_CLEANUP_RUNS.labels("partial")._value.get()

    async def sweep(conn):
        stop.set()
        return {"expired": 0, "errors": 1}

    _worker_with(monkeypatch, sweep)
    await asyncio.wait_for(media_cleanup.run_media_cleanup_worker(stop), timeout=2)
    assert MEDIA_CLEANUP_RUNS.labels("partial")._value.get() == before + 1
    assert MEDIA_CLEANUP_LAST_SUCCESS._value.get() == 0
