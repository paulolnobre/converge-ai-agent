"""Errors in the log become alerts for the admins, as they happen.

A logging handler counts every ERROR of the bot by kind: the logger, the
message as written in the code (its template, never the values filled in) and
the exception type. A worker turns each kind into at most one alert per
ERROR_ALERT_MINUTES, with how many times it happened, into the admin room and
its outbox; the monitor (cli/monitor.py) delivers it by push, as it does
with "bot down", and by Telegram only to ADMIN_ALERT_CHAT_ID when one is set.

No customer data: the template carries no values, and the logs carry none
either (docs/OBSERVABILIDADE.md).

The counts and the window live in this process: the bot runs as one uvicorn
process (Dockerfile). Running several would need them in the database.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 60
MAX_KINDS = 50          # past this, new kinds are counted together, not one by one
_TEXT_MAX = 300
_OTHERS = "others"
# One alert is also a push title, sent as ASCII-escaped JSON (an "é" takes 6
# bytes): 4 kinds of up to 100 characters stay under 3.5 KB even all accented,
# inside the 4 KB Web Push limit. The rest is summed up.
_LINES = 4
_WHAT_MAX = 100
_WHERE_MAX = 40
_PUSH_BYTES = 3000      # the title as core.services.push serializes it


@dataclass
class Seen:
    where: str
    what: str
    count: int
    first: datetime


_lock = threading.Lock()
_pending: dict[str, Seen] = {}
_last_alert: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def kind_of(record: logging.LogRecord) -> tuple[str, str, str]:
    """(key, where, what): the same mistake in the same place is one kind."""
    template = " ".join(str(record.msg).split())[:_TEXT_MAX]
    exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
    what = f"{template} ({exc})" if exc else template
    key = hashlib.sha256(f"{record.name}\n{template}\n{exc}".encode()).hexdigest()[:16]
    return key, record.name, what


def note(record: logging.LogRecord, now: datetime | None = None) -> None:
    from core.metrics import LOG_ERRORS

    LOG_ERRORS.labels(record.name).inc()
    key, where, what = kind_of(record)
    now = now or _now()
    with _lock:
        if key not in _pending and len(_pending) >= MAX_KINDS:
            key, where, what = _OTHERS, "core", "Outros erros (muitos tipos ao mesmo tempo)"
        seen = _pending.get(key)
        if seen is None:
            _pending[key] = Seen(where, what, 1, now)
        else:
            seen.count += 1


class AlertHandler(logging.Handler):
    """Counts ERROR and above. Never raises, never blocks on the network."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return  # an alert that fails is not itself an alert
        try:
            note(record)
        except Exception:
            self.handleError(record)


def install(target: logging.Logger) -> None:
    if not any(isinstance(h, AlertHandler) for h in target.handlers):
        target.addHandler(AlertHandler())


def take_due(now: datetime | None = None) -> list[tuple[str, Seen]]:
    """The kinds whose last alert is older than the window, taken out."""
    now = now or _now()
    window = timedelta(minutes=settings.error_alert_minutes)
    due: list[tuple[str, Seen]] = []
    with _lock:
        for key in list(_pending):
            last = _last_alert.get(key)
            if last is None or now - last >= window:
                due.append((key, _pending.pop(key)))
                _last_alert[key] = now
    return due


def give_back(items: list[tuple[str, Seen]]) -> None:
    """Not stored: counted again in the next pass, nothing lost."""
    with _lock:
        for key, seen in items:
            _last_alert.pop(key, None)
            current = _pending.get(key)
            if current is None:
                _pending[key] = seen
            else:
                current.count += seen.count
                current.first = min(current.first, seen.first)


def _line(seen: Seen) -> str:
    since = seen.first.astimezone(ZoneInfo(settings.office_timezone))
    times = "1 vez" if seen.count == 1 else f"{seen.count} vezes"
    what = seen.what if len(seen.what) <= _WHAT_MAX else seen.what[:_WHAT_MAX - 1] + "…"
    return f"• {what}\n  em {seen.where[-_WHERE_MAX:]}, {times} desde {since:%H:%M}"


def text(items: list[Seen]) -> str:
    """One alert for every kind due in a pass: a burst is one message, never
    dozens competing for delivery."""
    ordered = sorted(items, key=lambda seen: -seen.count)
    head = "⚠️ Erro no agente" if len(ordered) == 1 else f"⚠️ {len(ordered)} tipos de erro no agente"
    lines = [_line(seen) for seen in ordered[:_LINES]]
    rest = ordered[_LINES:]
    if rest:
        lines.append(f"• e mais {len(rest)} tipos ({sum(s.count for s in rest)} erros)")
    return fit(f"{head} (horário de Belém):\n" + "\n".join(lines))


def fit(body: str) -> str:
    """Cut by bytes as sent: any character (an emoji is 12) stays inside the limit."""
    if len(json.dumps(body)) <= _PUSH_BYTES:
        return body
    while len(json.dumps(body + "…")) > _PUSH_BYTES:
        body = body[:-max(1, len(body) // 20)]
    return body + "…"


async def store(conn, items: list[tuple[str, Seen]], now: datetime) -> None:
    from core.repositories import admin_room

    body = text([seen for _, seen in items])[:4000]  # the admin room limit
    message_id = await admin_room.insert(conn, body, kind="alert",
                                         event_key=f"errors:{now:%Y%m%d%H%M%S%f}")
    if message_id is not None:
        # Telegram only to a chat of the admins (ADMIN_ALERT_CHAT_ID), never the
        # team group: without one, the push and the admin room carry it.
        await conn.execute(
            "INSERT INTO admin_alert_outbox (message_id, title, telegram_done) VALUES (%s, %s, %s)",
            (message_id, body, not settings.admin_alert_chat_id))


async def flush(now: datetime | None = None) -> int:
    from core.repositories.connection import get_pool

    now = now or _now()
    due = take_due(now)
    if not due:
        return 0
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await store(conn, due, now)
    except Exception as exc:
        give_back(due)
        # WARNING, not ERROR: a broken alert path must not alert about itself.
        logger.warning("Error alerts not stored (%s); kept for the next pass",
                       type(exc).__name__)
        return 0
    return len(due)


async def run_error_alert_worker(stop_event: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), FLUSH_SECONDS)
            return  # shutting down: no database call while the pool closes
        except TimeoutError:
            await flush()


def reset() -> None:
    with _lock:
        _pending.clear()
        _last_alert.clear()
