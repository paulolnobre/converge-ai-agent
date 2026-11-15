"""The worker that drains the update queue.

The webhook's only job is to persist an update and answer Telegram inside its
timeout. Everything slow — transcription, the model, a calendar round-trip —
happens here, where a failure can be retried instead of turning into a Telegram
redelivery storm.

Each update is reserved (not popped), so a replica killed mid-message leaves the
work recoverable rather than lost.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from core.handlers import message as handler
from core.handlers.message import process_callback, process_message
from core.integrations import update_queue
from core.metrics import UPDATE_SECONDS
from core.observability import bind_context, new_correlation_id
from core.repositories.connection import get_pool
from core.services import billing, contract_signed

logger = logging.getLogger(__name__)

_UPDATE_TIMEOUT = 180  # hard cap per update, seconds
_QUARANTINE_RETRY = 30  # seconds between retries of a quarantine that failed
_HEARTBEAT = 15  # seconds between "still alive" marks in Redis (cli.queue_unseal)

_worker: asyncio.Task | None = None


def worker_alive() -> bool:
    """Whether the update worker is running — /ready is not ready without it."""
    return _worker is not None and not _worker.done()


async def _handle_one(pool, payload: str) -> None:
    try:
        update = update_queue.decode_update(payload)
    except update_queue.QueueItemError as exc:
        # Not the next item's problem: set this one aside, sealed, and go on.
        await update_queue.hold_for_quarantine(payload, exc.reason)
        return
    update_id = update.get("update_id")
    started = time.perf_counter()
    result = "error"
    with bind_context(correlation=new_correlation_id("upd"), update=update_id):
        try:
            if "asaas_event" in update:
                async with pool.connection() as conn:
                    outcome = await asyncio.wait_for(
                        billing.handle_event(conn, update["asaas_event"]), timeout=_UPDATE_TIMEOUT)
                logger.info("Asaas event handled: %s", outcome)
            elif "autentique_event" in update:
                async with pool.connection() as conn:
                    outcome = await asyncio.wait_for(contract_signed.handle_event(
                        conn, update["autentique_event"]), timeout=_UPDATE_TIMEOUT)
                logger.info("Autentique event handled: %s", outcome)
            else:
                handle = process_callback if "callback_query" in update else process_message
                # Committed on its own: a failed turn below rolls back, but the
                # customer did write, and WhatsApp's 24h window reopened.
                async with pool.connection() as conn:
                    await handler.note_inbound(conn, update)
                async with pool.connection() as conn:
                    await asyncio.wait_for(handle(conn, update), timeout=_UPDATE_TIMEOUT)
            result = "success"
            await update_queue.acknowledge_update(payload)
        except TimeoutError:
            logger.error("Update handling timed out")
            result = "timeout"
            await update_queue.retry_update(payload, update_id)
        except Exception:
            logger.exception("Update handling failed")
            await update_queue.retry_update(payload, update_id)
        finally:
            UPDATE_SECONDS.labels(result).observe(time.perf_counter() - started)


async def run_update_worker(stop_event: asyncio.Event) -> None:
    """Drain the active queue until asked to stop."""
    global _worker
    _worker = asyncio.current_task()
    pool = await get_pool()
    with contextlib.suppress(Exception):
        # Work a previous replica reserved but never acknowledged.
        await update_queue.recover_processing()

    # Its own task: a turn can take three times the heartbeat's 60 s, and the
    # loop below is busy for all of it.
    beat = asyncio.create_task(_beat(), name="worker-heartbeat")
    try:
        await _drain(stop_event, pool)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _beat() -> None:
    while True:
        with contextlib.suppress(Exception):
            await update_queue.heartbeat()
        await asyncio.sleep(_HEARTBEAT)


async def _drain(stop_event: asyncio.Event, pool) -> None:
    last_retry = time.monotonic()
    while not stop_event.is_set():
        if update_queue.pending_quarantine() and \
                time.monotonic() - last_retry >= _QUARANTINE_RETRY:
            last_retry = time.monotonic()
            with contextlib.suppress(Exception):
                await update_queue.retry_pending_quarantine()
        try:
            payload = await update_queue.reserve_update(deferred=False)
        except Exception:
            logger.exception("Could not reserve an update; backing off")
            await asyncio.sleep(5)
            continue
        if payload is None:
            continue
        await _handle_one(pool, payload)


async def run_deferred_worker(stop_event: asyncio.Event, is_open) -> None:
    """Release messages that arrived after hours, once the desk opens again.

    `is_open` is injected rather than imported so the schedule can be tested
    without moving the clock.
    """
    while not stop_event.is_set():
        if not is_open():
            await asyncio.sleep(60)
            continue
        try:
            await update_queue.promote_deferred()
        except Exception:
            logger.exception("Could not promote deferred updates; backing off")
        # One sweep a minute is enough: these messages already waited all night.
        await asyncio.sleep(60)
