"""HTTP surface: the Telegram and WhatsApp webhooks, health, readiness and metrics.

The webhook does as little as possible — validate the secret, persist the update,
answer 200 — because Telegram's timeout is short and its response to a slow reply
is to send the same update again. Everything else runs in the worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core import agent, channels
from core.agent import run_deferred_worker, run_update_worker
from core.config import settings, validate_startup_config
from core.integrations import (
    cache,
    llm,
    telegram,
    update_queue,
    whatsapp,
    whatsapp_updates,
    workspace,
)
from core.logger import setup_logging
from core.metrics import (
    DEPENDENCY_READY,
    HTTP_REQUEST_SECONDS,
    HTTP_REQUESTS,
    QUEUE_DEPTH,
)
from core.observability import bind_context, new_correlation_id
from core.repositories import contacts as contacts_repo
from core.repositories.connection import close_pool, get_pool
from core.schedule import is_open, out_of_hours_message
from core.services import audio, contract_signed, data_format, error_alerts
from core.services import billing as billing_service
from core.services.calendar_sync import run_calendar_sync_worker
from core.services.handoff import run_handoff_timeout_worker
from core.services.mail_alerts import run_mail_alert_worker
from core.services.media_cleanup import run_media_cleanup_worker
from core.services.proposal_briefing import run_proposal_worker
from core.services.reminders import run_reminder_worker
from core.services.secretarial import run_briefing_worker
from core.web import panel

setup_logging()
# Errors of the bot (and of the web server around it) become admin alerts.
error_alerts.install(logging.getLogger("core"))
error_alerts.install(logging.getLogger("uvicorn.error"))
logger = logging.getLogger(__name__)


def _chat_id(update: dict):
    message = update.get("message") or update.get("edited_message")
    if message is None:
        message = (update.get("callback_query") or {}).get("message") or {}
    return (message.get("chat") or {}).get("id")


def _is_private(update: dict) -> bool:
    """Groups get no automatic replies: the bot talks to customers one to one."""
    message = update.get("message") or update.get("edited_message") or {}
    return (message.get("chat") or {}).get("type") in (None, "private")


def _valid_secret(received: str | None) -> bool:
    expected = settings.telegram_webhook_secret
    if not expected:
        # Refused outright in production by the settings guardrails; allowed in
        # development so a local tunnel works without ceremony.
        return settings.app_env == "development"
    return bool(received) and hmac.compare_digest(received, expected)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # A model pointed at the wrong provider is silently swapped at request time,
    # so the deployment would run a model nobody chose. Refuse instead.
    validate_startup_config()
    report = workspace.configuration_report()
    if report["status"] != "configured":
        logger.warning(
            "Workspace integration is incomplete: missing credentials for %s",
            ", ".join(report["missing"]) or "an unknown provider",
        )

    await get_pool()
    # Before anything can write: this image writes encrypted data, and an
    # older image must not start on it (deploy/deploy.sh reads these markers).
    await data_format.raise_markers()
    # Before the first request: restarted at night, a /teste must not be
    # deferred because the team is not known yet.
    await refresh_never_deferred(required=True)
    stop_event = asyncio.Event()
    workers = [
        asyncio.create_task(run_update_worker(stop_event), name="update-worker"),
        asyncio.create_task(
            run_deferred_worker(stop_event, is_open), name="deferred-worker"
        ),
        asyncio.create_task(run_reminder_worker(stop_event), name="reminder-worker"),
        asyncio.create_task(
            run_handoff_timeout_worker(stop_event), name="handoff-timeout-worker"
        ),
        asyncio.create_task(run_briefing_worker(stop_event), name="briefing-worker"),
        asyncio.create_task(run_proposal_worker(stop_event), name="proposal-worker"),
        asyncio.create_task(run_never_deferred_worker(stop_event), name="after-hours-list"),
        # Always, capture on or off: a file kept once must still meet its deadline.
        asyncio.create_task(run_media_cleanup_worker(stop_event), name="media-cleanup"),
        asyncio.create_task(run_calendar_sync_worker(stop_event), name="calendar-sync"),
        # Idles unless SPACEMAIL_ALERTS is on and Spacemail is configured.
        asyncio.create_task(run_mail_alert_worker(stop_event), name="mail-alerts"),
        asyncio.create_task(error_alerts.run_error_alert_worker(stop_event), name="error-alerts"),
    ]
    try:
        yield
    finally:
        stop_event.set()
        for task in workers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await close_pool()
        await update_queue.close()
        await cache.close()
        await llm.close()
        await telegram.close()
        await whatsapp.close()
        await audio.close()
        await workspace.close()


app = FastAPI(
    title="Reception Assistant",
    version=settings.app_version,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None,
    lifespan=lifespan,
)
panel.install(app)


# Labels from a bounded set: a raw path or method from the internet would be a
# new time series kept in memory forever (core/metrics.py).
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _route_label(request: Request) -> str:
    """The matched route's template ("/painel/api/inbox/{contact_id}"), or
    "unmatched" for anything no route answers."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


@app.middleware("http")
async def observe_http(request: Request, call_next):
    correlation = request.headers.get("x-request-id") or new_correlation_id()
    started = time.perf_counter()
    status_code = 500
    with bind_context(correlation=correlation):
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = correlation
            return response
        finally:
            path = _route_label(request)
            method = request.method if request.method in _METHODS else "OTHER"
            HTTP_REQUESTS.labels(method, path, str(status_code)).inc()
            HTTP_REQUEST_SECONDS.labels(method, path).observe(
                time.perf_counter() - started
            )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "version": settings.app_version,
        "desk_open": is_open(),
    }


@app.get("/ready")
async def ready() -> dict:
    unavailable: list[str] = []
    dependencies: dict[str, str] = {}

    try:
        await update_queue.ping()
        DEPENDENCY_READY.labels("redis").set(1)
        dependencies["redis"] = "ready"
    except Exception:
        DEPENDENCY_READY.labels("redis").set(0)
        dependencies["redis"] = "unavailable"
        unavailable.append("queue")
        logger.exception("Redis readiness check failed")

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        DEPENDENCY_READY.labels("postgres").set(1)
        dependencies["postgres"] = "ready"
    except Exception:
        DEPENDENCY_READY.labels("postgres").set(0)
        dependencies["postgres"] = "unavailable"
        unavailable.append("database")
        logger.exception("PostgreSQL readiness check failed")

    # A worker that died leaves the queue filling up behind a healthy-looking
    # container; an item stuck before its quarantine is one nobody will answer.
    if not agent.worker_alive():
        unavailable.append("worker")
    if update_queue.pending_quarantine():
        unavailable.append("quarentena pendente")

    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"unavailable dependencies: {', '.join(unavailable)}",
        )
    # The workspace suites are reported, not required: the desk can still answer
    # and take a message while a calendar credential is being rotated.
    return {
        "status": "ready",
        "dependencies": dependencies,
        "workspace": workspace.configuration_report(),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        for queue, depth in (await update_queue.queue_depths()).items():
            QUEUE_DEPTH.labels(queue).set(depth)
    except Exception:
        DEPENDENCY_READY.labels("redis").set(0)
        logger.exception("Could not collect Redis queue depths")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if not _valid_secret(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook")

    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON"
        ) from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid update")

    # A tapped button keeps a loading shimmer until the tap is acknowledged.
    # Done here, not by the worker: behind a slow model call in the queue the
    # button shimmered for seconds. The worker sees `_acked` and skips it.
    callback = update.get("callback_query")
    if isinstance(callback, dict) and callback.get("id"):
        await telegram.answer_callback_query(callback["id"])
        callback["_acked"] = True

    return await _accept(update)


# The team's chats and chats in /teste, answered after hours too. Kept in
# memory and refreshed in the background: the webhook must answer Telegram and
# Meta within seconds, and a database lookup there could make them wait — and
# send the update again.
_never_deferred: set[str] = set()
_NEVER_DEFERRED_SECONDS = 60


async def refresh_never_deferred(*, required: bool = False) -> None:
    """A failed refresh keeps the last set: better slightly stale than empty.
    `required`, at start-up: there is no last set, so the failure is raised —
    the app does not serve, and the deploy keeps the previous image."""
    global _never_deferred
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            _never_deferred = await contacts_repo.never_deferred_chats(conn)
    except Exception:
        if required:
            raise
        logger.warning("Could not refresh the after-hours list; keeping the last one")


async def run_never_deferred_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await refresh_never_deferred()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), _NEVER_DEFERRED_SECONDS)


async def _accept(update: dict) -> dict:
    """Queue one update from either channel, deferred out of office hours.

    A failure to persist becomes a 503, which makes Telegram and Meta both send
    the update again: answering 200 would silently lose a message someone sent
    to a business that promises to answer it.
    """
    chat_id = _chat_id(update)
    # The team's chats and chats in /teste are never held until the morning:
    # /teste, /sair and the test itself must work at night too.
    bypassed = chat_id is not None and (
        str(chat_id) in settings.hours_bypass_chats() or str(chat_id) in _never_deferred
    )
    deferred = not is_open() and not bypassed
    try:
        inserted = await update_queue.enqueue_update(update, deferred=deferred)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not persist update",
        ) from exc

    item = update.get("message") or update.get("callback_query") or {}
    if (inserted and deferred and chat_id is not None and _is_private(update)
            and not channels.expired(chat_id, item) and not await contacts_repo.is_blocked_now(chat_id)):
        await channels.send(chat_id, out_of_hours_message(), parse_mode=None)

    return {"ok": True, "queued": inserted, "deferred": deferred}


@app.post("/asaas/webhook")
async def asaas_webhook(request: Request) -> dict:
    expected = settings.asaas_webhook_token
    if not expected:
        raise HTTPException(status_code=404)
    token = request.headers.get("asaas-access-token") or ""
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if (not isinstance(payload, dict) or not isinstance(payload.get("event"), str)
            or not payload["event"].strip()):
        raise HTTPException(status_code=400, detail="invalid event")
    key = billing_service.event_id(payload)
    if key is None:
        raise HTTPException(status_code=400, detail="invalid event identity")
    try:
        inserted = await update_queue.enqueue_update(
            {"update_id": f"asaas:{key}", "asaas_event": payload}, deferred=False)
    except (RuntimeError, ValueError):
        logger.error("Asaas event could not be queued")
        raise HTTPException(status_code=503, detail="queue unavailable") from None
    return {"ok": True, "queued": inserted}


@app.post("/autentique/webhook")
async def autentique_webhook(request: Request) -> dict:
    """The contract signed: checked by its signature, queued, acknowledged."""
    secret = settings.autentique_webhook_secret
    if not secret:
        raise HTTPException(status_code=404)
    body = await request.body()
    signature = request.headers.get("x-autentique-signature") or ""
    if not signature.isascii() or not contract_signed.verify(body, signature, secret):
        raise HTTPException(status_code=401, detail="invalid webhook")
    try:
        event = contract_signed.slim(json.loads(body))
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid event") from None
    if event is None:
        return {"ok": True, "queued": False}
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            wait = await contract_signed.must_wait(conn, event["document"])
    except Exception:
        logger.error("Autentique event could not be checked")
        raise HTTPException(status_code=503, detail="database unavailable") from None
    if wait:
        raise HTTPException(status_code=503, detail="contract still being sent")
    try:
        inserted = await update_queue.enqueue_update(
            {"update_id": f"autentique:{event['id']}", "autentique_event": event},
            deferred=False)
    except (RuntimeError, ValueError):
        logger.error("Autentique event could not be queued")
        raise HTTPException(status_code=503, detail="queue unavailable") from None
    return {"ok": True, "queued": inserted}


@app.get("/whatsapp/webhook")
async def whatsapp_verify(request: Request) -> Response:
    """Meta's one-time check when the webhook is set up: echo the challenge
    only to someone who knows the verify token."""
    if not settings.whatsapp_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="whatsapp not configured")
    params = request.query_params
    token = params.get("hub.verify_token") or ""
    expected = settings.whatsapp_webhook_verify_token or ""
    # Compared as bytes: a str with accents makes compare_digest raise.
    if params.get("hub.mode") != "subscribe" or not hmac.compare_digest(
        token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid verify token")
    return PlainTextResponse(params.get("hub.challenge") or "")


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> dict:
    """Meta's calls, checked against the app secret over the exact bytes it
    signed, then queued like any Telegram update."""
    if not settings.whatsapp_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="whatsapp not configured")
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid payload")

    for message_id, code in whatsapp_updates.delivery_failures(payload):
        logger.warning("WhatsApp could not deliver %s (error code %s)", message_id, code)
    queued = 0
    for update in whatsapp_updates.to_updates(payload, settings.whatsapp_phone_number_id or ""):
        if (await _accept(update))["queued"]:
            queued += 1
    return {"ok": True, "queued": queued}
