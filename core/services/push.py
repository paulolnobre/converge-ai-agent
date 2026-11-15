"""Push notifications to the panel app (docs/PUSH.md).

The turn only reads who gets the notice (`recipients`); `schedule` sends in
the background and returns at once, so a slow push service never holds the
customer's reply. pywebpush encrypts (RFC 8291) and py_vapid signs (RFC 8292);
the POST goes through httpx, without following redirects, with a short
timeout and no database connection held. The notice says who, never what the
customer wrote; logs carry ids, never a name or an endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
from py_vapid import Vapid
from pywebpush import WebPusher

from core.config import settings
from core.repositories import push as push_repo
from core.repositories.connection import get_pool
from core.services import push_endpoint

logger = logging.getLogger(__name__)

TTL_SECONDS = 3600
TIMEOUT_SECONDS = 10
MAX_CONCURRENT = 4
MAX_PENDING = 50
MAX_LOOKUPS = 10

_pending: set[asyncio.Task] = set()   # sends
_lookups: set[asyncio.Task] = set()   # team notices reading who gets them
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a fake service
_limit: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    global _limit
    loop = asyncio.get_running_loop()
    if _limit is None or _limit[0] is not loop:
        _limit = (loop, asyncio.Semaphore(MAX_CONCURRENT))
    return _limit[1]


async def recipients(conn, user_id: int) -> list[dict]:
    """The subscriptions of `user_id` whose session would still sign in."""
    if not push_endpoint.push_configured():
        return []
    # In a savepoint: a failed lookup must not abort the customer's turn,
    # whose work shares this transaction.
    try:
        async with conn.transaction():
            return await push_repo.active_for_user(
                conn, user_id, idle_minutes=settings.panel_idle_minutes,
                device_idle_days=settings.panel_device_idle_days)
    except Exception as exc:
        logger.warning("Push devices of panel user %s not read (%s)", user_id, type(exc).__name__)
        return []


def schedule(subs: list[dict], *, title: str, url: str, tag: str) -> None:
    """Send in the background; never raises and never waits."""
    if not subs or not push_endpoint.push_configured():
        return
    payload = json.dumps({"title": title, "url": url, "tag": tag}).encode()
    for sub in subs:
        if len(_pending) >= MAX_PENDING:
            logger.warning("Push queue full; notice to subscription %s dropped", sub.get("id"))
            continue
        task = asyncio.ensure_future(_send(sub, payload))
        _pending.add(task)
        task.add_done_callback(_pending.discard)


def schedule_team(*, title: str, url: str, tag: str, chat_id: str | None = None) -> None:
    """The whole team, read and sent in the background on a connection of its
    own: the turn that calls this neither waits nor shares a transaction.
    Nothing about the team's own chat (`chat_id`), as the Inbox hides it.
    The lookups have their own limit, so they never fill the send queue."""
    if not push_endpoint.push_configured():
        return
    if len(_lookups) >= MAX_LOOKUPS:
        logger.warning("Push lookups full; a team notice was dropped")
        return
    task = asyncio.ensure_future(_to_team(title, url, tag, chat_id))
    _lookups.add(task)
    task.add_done_callback(_lookups.discard)


async def _to_team(title: str, url: str, tag: str, chat_id: str | None) -> None:
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            if chat_id and await push_repo.is_team_chat(conn, chat_id):
                return
            subs = await push_repo.active_for_all(
                conn, idle_minutes=settings.panel_idle_minutes,
                device_idle_days=settings.panel_device_idle_days)
    except Exception as exc:
        logger.warning("Push devices of the team not read (%s)", type(exc).__name__)
        return
    schedule(subs, title=title, url=url, tag=tag)


async def notify_user(conn, user_id: int, *, title: str, url: str, tag: str) -> bool:
    subs = await recipients(conn, user_id)
    schedule(subs, title=title, url=url, tag=tag)
    return bool(subs)


def _request(sub: dict, payload: bytes) -> tuple[bytes, dict[str, str]]:
    encoded = WebPusher({"endpoint": sub["endpoint"],
                         "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
                        ).encode(payload, "aes128gcm")
    host = urlsplit(sub["endpoint"]).hostname
    claims = {"sub": settings.push_vapid_subject, "aud": f"https://{host}",
              "exp": int(time.time()) + 12 * 3600}
    headers = dict(Vapid.from_string(private_key=settings.push_vapid_private_key).sign(claims))
    headers.update({"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                    "TTL": str(TTL_SECONDS), "Urgency": "high"})
    return encoded["body"], headers


async def _post(sub: dict, payload: bytes) -> int | None:
    """The push service's status, or None when it could not be reached."""
    try:
        body, headers = _request(sub, payload)
        # One budget for the whole exchange (httpx's timeout is per step), and
        # the answer's body is never read: only its status matters.
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with httpx.AsyncClient(transport=_transport, timeout=TIMEOUT_SECONDS,
                                         follow_redirects=False) as client:
                async with client.stream("POST", sub["endpoint"], content=body,
                                         headers=headers) as response:
                    return response.status_code
    except Exception as exc:  # any failure here is one lost notice, never the turn
        logger.warning("Push to subscription %s failed (%s)", sub.get("id"), type(exc).__name__)
        return None


async def _send(sub: dict, payload: bytes) -> bool:
    try:
        if not push_endpoint.valid(sub.get("endpoint", "")):
            status: int | None = 410  # not ours to contact any more: drop it
        else:
            async with _semaphore():
                status = await _post(sub, payload)
        pool = await get_pool()
        async with pool.connection() as conn:
            if status in (200, 201, 202):
                await push_repo.mark_ok(conn, sub["id"])
            elif status in (404, 410):
                await push_repo.delete(conn, sub["id"])
            else:
                if status is not None:
                    logger.warning("Push to subscription %s answered %s", sub["id"], status)
                await push_repo.mark_failed(conn, sub["id"])
        return status in (200, 201, 202, 404, 410)
    except Exception as exc:
        logger.warning("Push result for subscription %s not recorded (%s)",
                       sub.get("id"), type(exc).__name__)
        return False


async def send_now(sub: dict, *, title: str, url: str, tag: str) -> bool:
    """One bounded delivery, awaited by the monitor; no background task survives it."""
    if not push_endpoint.push_configured():
        return False
    return await _send(sub, json.dumps({'title': title, 'url': url, 'tag': tag}).encode())
