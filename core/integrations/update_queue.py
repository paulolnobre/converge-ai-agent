"""Durable Redis queues for Telegram updates.

The webhook acknowledges Telegram only after an update is stored. A processing
list keeps reserved items recoverable if the worker process is interrupted.

Every payload is an envelope (core/crypto.py, context "queue"), base64 text:
the deferred list and the dead letters can hold a message for days in a Redis
with AOF, and none of it is in clear. A payload in plain JSON (written before
this) is still read; cli.seal_data seals the lists that keep items.
"""
import asyncio
import base64
import hashlib
import json
import logging
from datetime import UTC, datetime

import redis.asyncio as redis
from redis.exceptions import RedisError, WatchError

from core.config import MEDIA_EVENT_DAYS, settings
from core.crypto import SealedDataError, current_key_version, key_version_of, open_text, seal_text
from core.metrics import QUEUE_EVENTS

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None

_ENQUEUE_SCRIPT = """
if redis.call('SET', KEYS[1], '1', 'EX', ARGV[1], 'NX') then
  redis.call('LPUSH', KEYS[2], ARGV[2])
  return 1
end
return 0
"""


# LPUSH into the quarantine before the LREM from the processing list, with no
# one in between: the item is never in neither. A refused LPUSH (no memory,
# noeviction) aborts the script before anything is removed.
_QUARANTINE_SCRIPT = """
local pushed = redis.call('LPUSH', KEYS[1], ARGV[1])
redis.call('LREM', KEYS[2], 1, ARGV[2])
return pushed
"""

_CONTEXT = "queue"
_QUARANTINE_CONTEXT = "queue.quarantine"

# Items whose move to the quarantine failed: still in the processing list,
# retried by the worker every 30 s. /ready says so while any is here.
_pending: list[tuple[str, str]] = []


class QueueItemError(Exception):
    """An item that cannot be read. `reason` never quotes the item."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=max(10, settings.queue_block_seconds + 5),
            health_check_interval=30,
        )
    return _client


def _queue_name(deferred: bool) -> str:
    return settings.queue_deferred_name if deferred else settings.queue_active_name


async def enqueue_update(update: dict, *, deferred: bool) -> bool:
    """Persist an update exactly once. Returns False for a Telegram retry."""
    update_id = update.get("update_id")
    if update_id is None:
        raise ValueError("Telegram update without update_id")
    payload = encode_update(update)
    dedupe_key = f"{settings.cache_prefix}:update:{update_id}"
    try:
        inserted = await _redis().eval(
            _ENQUEUE_SCRIPT,
            2,
            dedupe_key,
            _queue_name(deferred),
            settings.queue_dedupe_ttl,
            payload,
        )
    except (RedisError, OSError) as exc:
        logger.error("Could not durably enqueue Telegram update: %s", exc)
        raise RuntimeError("Update queue unavailable") from exc
    QUEUE_EVENTS.labels("enqueued" if inserted else "duplicate").inc()
    return bool(inserted)


async def recover_processing() -> int:
    """Return work reserved by a replica that stopped before acknowledging it."""
    recovered = 0
    client = _redis()
    while True:
        payload = await client.rpoplpush(
            settings.queue_processing_name,
            settings.queue_active_name,
        )
        if payload is None:
            break
        recovered += 1
    if recovered:
        QUEUE_EVENTS.labels("recovered").inc(recovered)
        logger.warning("Recovered %d interrupted Telegram updates", recovered)
    return recovered


async def promote_deferred(limit: int = 200) -> int:
    """Move after-hours updates onto the active queue, oldest first.

    Deliberately not a retry: a deferred message never failed, so it must not
    consume an attempt or risk being dead-lettered for having waited overnight.
    """
    moved = 0
    client = _redis()
    while moved < limit:
        payload = await client.rpoplpush(
            settings.queue_deferred_name, settings.queue_active_name
        )
        if payload is None:
            break
        moved += 1
    if moved:
        QUEUE_EVENTS.labels("promoted").inc(moved)
        logger.info("Promoted %d deferred updates onto the active queue", moved)
    return moved


async def reserve_update(*, deferred: bool) -> str | None:
    """Atomically reserve one item so it survives a worker interruption."""
    payload = await _redis().brpoplpush(
        _queue_name(deferred),
        settings.queue_processing_name,
        timeout=settings.queue_block_seconds,
    )
    if payload is None:
        return None
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return str(payload)


async def acknowledge_update(payload: str) -> None:
    await _redis().lrem(settings.queue_processing_name, 1, payload)
    QUEUE_EVENTS.labels("acknowledged").inc()


async def retry_update(payload: str, update_id: int | str | None) -> bool:
    """Requeue a failed item, or dead-letter it after the configured limit."""
    client = _redis()
    attempts_key = f"{settings.cache_prefix}:update-attempts:{update_id or 'unknown'}"
    async with client.pipeline(transaction=True) as pipe:
        pipe.incr(attempts_key)
        pipe.expire(attempts_key, settings.queue_dedupe_ttl)
        attempts, _ = await pipe.execute()

    destination = (
        settings.queue_dead_letter_name
        if int(attempts) >= settings.queue_max_attempts
        else settings.queue_active_name
    )
    async with client.pipeline(transaction=True) as pipe:
        pipe.lrem(settings.queue_processing_name, 1, payload)
        pipe.lpush(destination, payload)
        await pipe.execute()
    QUEUE_EVENTS.labels(
        "retried" if destination == settings.queue_active_name else "dead_lettered"
    ).inc()
    return destination != settings.queue_dead_letter_name


def encode_update(update: dict) -> str:
    return seal_text(_CONTEXT, json.dumps(update, ensure_ascii=False, separators=(",", ":")))


def decode_update(payload: str) -> dict:
    """An envelope, or a plain-JSON payload from before. QueueItemError when
    neither opens."""
    if payload.startswith("{") or payload.startswith("["):
        text = payload
    else:
        try:
            text = open_text(_CONTEXT, payload)
        except SealedDataError:
            raise QueueItemError("ilegível (adulterado ou chave ausente)") from None
    try:
        value = json.loads(text)
    except ValueError:
        raise QueueItemError("json inválido") from None
    if not isinstance(value, dict):
        raise QueueItemError("não é um objeto")
    return value


async def quarantine(payload: str, reason: str) -> bool:
    """Move an unreadable item from the processing list to the quarantine,
    sealed with its reason and time. False if the move did not happen; the
    item is then still in the processing list."""
    try:
        envelope = _quarantine_envelope(payload, reason)
    except SealedDataError:
        return False
    try:
        await _redis().eval(_QUARANTINE_SCRIPT, 2, settings.queue_quarantine_name,
                            settings.queue_processing_name, envelope, payload)
    except (RedisError, OSError):
        logger.error("Could not quarantine an unreadable queue item (%s)", reason)
        return False
    QUEUE_EVENTS.labels("quarantined").inc()
    logger.error("Quarantined an unreadable queue item (%s)", reason)
    return True


async def hold_for_quarantine(payload: str, reason: str) -> None:
    """Quarantine now, or keep it to retry while the worker lives."""
    if not await quarantine(payload, reason):
        _pending.append((payload, reason))


def pending_quarantine() -> int:
    return len(_pending)


async def retry_pending_quarantine() -> int:
    moved = 0
    for item in list(_pending):
        if await quarantine(*item):
            _pending.remove(item)
            moved += 1
    if _pending:
        logger.error("%d queue item(s) still waiting to be quarantined", len(_pending))
    return moved


def _stored_lists() -> tuple[str, ...]:
    """The lists that keep items for long: these are converted in place."""
    return (settings.queue_deferred_name, settings.queue_dead_letter_name,
            settings.queue_quarantine_name)


def _resealed(name: str, item: str, rekey: bool) -> str:
    if name == settings.queue_quarantine_name:
        if rekey and key_version_of(_raw(item)) != current_key_version():
            return seal_text(_QUARANTINE_CONTEXT, open_text(_QUARANTINE_CONTEXT, item))
        return item
    if item.startswith("{") or item.startswith("["):
        return seal_text(_CONTEXT, item)
    if rekey and key_version_of(_raw(item)) != current_key_version():
        return seal_text(_CONTEXT, open_text(_CONTEXT, item))
    return item


def _raw(item: str) -> bytes:
    return base64.b64decode(item)


async def _rewrite(name: str, transform, attempts: int = 10, on_attempt=None) -> int:
    """Rewrite one list item by item, in a transaction watched on the list: a
    list that changes meanwhile is read again, so a concurrent push is never
    lost. Returns how many items changed. `on_attempt` runs before each read,
    for a transform that counts: only the last attempt's counts are true."""
    client = _redis()
    for _ in range(attempts):
        if on_attempt is not None:
            on_attempt()
        async with client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(name)
                items = [str(i) for i in await pipe.lrange(name, 0, -1)]
                # A transform may return None: that item leaves the list.
                changed = [transform(item) for item in items]
                if changed == items:
                    await pipe.unwatch()
                    return 0
                new = [item for item in changed if item is not None]
                pipe.multi()
                pipe.delete(name)
                if new:
                    pipe.rpush(name, *new)
                await pipe.execute()
                return sum(a != b for a, b in zip(items, changed, strict=True))
            except WatchError:
                continue
    raise RuntimeError(f"{name} kept changing; run the command again")


async def seal_stored_lists(rekey: bool = False) -> int:
    """Seal the plain-JSON items of the lists that keep items (re-seal into the
    current key with `rekey`)."""
    return sum([await _rewrite(name, lambda item, n=name: _resealed(n, item, rekey))
                for name in _stored_lists()])


def _quarantine_envelope(payload: str, reason: str) -> str:
    return seal_text(_QUARANTINE_CONTEXT, json.dumps(
        {"reason": reason, "at": datetime.now(UTC).isoformat(), "payload": payload},
        ensure_ascii=False))


# Rewrite one list for format 0 in one step, losing nothing whatever fails:
# - only if the list is still what was read (else 0: the caller reads again);
# - the new list is built in a temporary key, in batches (Lua's unpack has a
#   stack limit), and the quarantine written, before the original is touched;
# - then RENAME swaps it in. A write that fails (wrong type, no memory) stops
#   the script with the original list intact.
_UNSEAL_SCRIPT = """
local expected = cjson.decode(ARGV[1])
local current = redis.call('LRANGE', KEYS[1], 0, -1)
if #current ~= #expected then return 0 end
for i = 1, #current do
  if current[i] ~= expected[i] then return 0 end
end
local function push(command, key, values)
  for i = 1, #values, 1000 do
    redis.call(command, key, unpack(values, i, math.min(i + 999, #values)))
  end
end
local held = cjson.decode(ARGV[3])
if #held > 0 then
  local kind = redis.call('TYPE', KEYS[2])['ok']
  if kind ~= 'list' and kind ~= 'none' then
    return redis.error_reply('the quarantine key is not a list')
  end
end
redis.call('DEL', KEYS[3])
local kept = cjson.decode(ARGV[2])
push('RPUSH', KEYS[3], kept)
push('LPUSH', KEYS[2], held)
if #kept > 0 then
  redis.call('RENAME', KEYS[3], KEYS[1])
else
  redis.call('DEL', KEYS[1])
end
return 1
"""


async def unseal_lists(attempts: int = 10) -> int:
    """Back to plain JSON, for an image of format 0 after a restore. What that
    image's worker cannot take (an envelope that does not open, broken JSON,
    not an object) goes to the quarantine, which stays sealed."""
    client, changed = _redis(), 0
    for name in (settings.queue_active_name, settings.queue_deferred_name,
                 settings.queue_dead_letter_name):
        for _ in range(attempts):
            items = [str(i) for i in await client.lrange(name, 0, -1)]
            kept, held = [], []
            for item in items:
                # The old worker parses outside its per-item handling, so
                # anything but a JSON object would stop its loop.
                try:
                    text = item if item.startswith(("{", "[")) else open_text(_CONTEXT, item)
                    if not isinstance(json.loads(text), dict):
                        raise ValueError("not an object")
                    kept.append(text)
                except (SealedDataError, ValueError):
                    held.append(_quarantine_envelope(item, "ilegível ao voltar ao formato 0"))
            if kept == items:
                break
            done = await client.eval(_UNSEAL_SCRIPT, 3, name, settings.queue_quarantine_name,
                                     f"{name}:unseal-tmp",
                                     json.dumps(items), json.dumps(kept), json.dumps(held))
            if int(done):
                changed += len(items)
                break
        else:
            raise RuntimeError(f"{name} kept changing; run the command again")
    return changed


def _heartbeat_key() -> str:
    return f"{settings.cache_prefix}:worker-heartbeat"


async def heartbeat() -> None:
    """The update worker says it is alive (60 s): cli.queue_unseal refuses to
    run while it does."""
    await _redis().set(_heartbeat_key(), "1", ex=60)


async def worker_heartbeat_seen() -> bool:
    return bool(await _redis().exists(_heartbeat_key()))


async def _list_problems(name: str, key_version: int | None) -> list[str]:
    context = _QUARANTINE_CONTEXT if name == settings.queue_quarantine_name else _CONTEXT
    problems: list[str] = []
    for position, item in enumerate(await _redis().lrange(name, 0, -1)):
        item, where = str(item), f"{name} posição {position}"
        if item.startswith(("{", "[")):
            problems.append(f"item 6: {where}: JSON puro")
            continue
        try:
            opened = open_text(context, item)
        except SealedDataError:
            problems.append(f"item 6: {where}: envelope não abre")
            continue
        if key_version is None:
            continue
        if key_version_of(_raw(item)) != key_version:
            problems.append(f"item 6: {where}: envelope em outra versão de chave")
        if name == settings.queue_quarantine_name:
            inner = str(json.loads(opened).get("payload", ""))
            try:
                if not inner.startswith(("{", "[")) and key_version_of(_raw(inner)) != key_version:
                    problems.append(f"item 7: {where}: guarda um payload de outra versão de chave")
            except (SealedDataError, ValueError):
                pass  # an original that was never an envelope: no key to lose
    return problems


async def check_lists(key_version: int | None = None, settle_seconds: float = 60) -> list[str]:
    """Problems in the queues, never quoting an item: plain JSON, an envelope
    that does not open, or (with key_version) one in another key. The active
    and processing lists empty themselves in seconds, so they get up to
    `settle_seconds` to do it before being blamed."""
    deadline = asyncio.get_running_loop().time() + settle_seconds
    transient = (settings.queue_active_name, settings.queue_processing_name)
    while True:
        moving = [p for name in transient for p in await _list_problems(name, key_version)]
        if not moving or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(1)
    return moving + [p for name in _stored_lists() for p in await _list_problems(name, key_version)]


async def quarantine_items() -> list[dict]:
    """What the quarantine holds — reason, time, size — never the content."""
    items = []
    for item in await _redis().lrange(settings.queue_quarantine_name, 0, -1):
        item = str(item)
        entry = {"id": _item_id(item), "reason": "ilegível", "at": None, "size": None}
        try:
            opened = json.loads(open_text(_QUARANTINE_CONTEXT, item))
            entry.update(reason=opened.get("reason"), at=opened.get("at"),
                         size=len(opened.get("payload", "")))
        except (SealedDataError, ValueError):
            pass
        items.append(entry)
    return items


def _item_id(item: str) -> str:
    """A stable name for a quarantine item: a position shifts when another item
    arrives, and dropping by position would delete the wrong one."""
    return hashlib.sha256(item.encode()).hexdigest()[:12]


async def drop_quarantine(ids: list[str]) -> int:
    """Delete the quarantine items with these ids (from `quarantine_items`)."""
    client, wanted, dropped = _redis(), set(ids), 0
    for item in await client.lrange(settings.queue_quarantine_name, 0, -1):
        if _item_id(str(item)) in wanted:
            dropped += await client.lrem(settings.queue_quarantine_name, 1, str(item))
    return dropped


async def ping() -> bool:
    return bool(await _redis().ping())


async def queue_depths() -> dict[str, int]:
    """Return bounded operational queue sizes for the Prometheus scrape."""
    names = {
        "active": settings.queue_active_name,
        "deferred": settings.queue_deferred_name,
        "processing": settings.queue_processing_name,
        "dead_letter": settings.queue_dead_letter_name,
    }
    async with _redis().pipeline(transaction=False) as pipe:
        for redis_name in names.values():
            pipe.llen(redis_name)
        values = await pipe.execute()
    return {label: int(value) for label, value in zip(names, values, strict=True)}


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError):
            pass
        _client = None


# ── Media payloads, for going back to an image without media ────────────────
# (docs/CRIPTOGRAFIA.md, "Voltar a uma imagem sem mídia")

_MEDIA_EVENT_SECONDS = MEDIA_EVENT_DAYS * 24 * 3600


def _media_lists() -> dict[str, str]:
    return {"active": settings.queue_active_name, "deferred": settings.queue_deferred_name,
            "processing": settings.queue_processing_name,
            "dead_letter": settings.queue_dead_letter_name,
            "quarantine": settings.queue_quarantine_name}


def _unpack(name: str, item: str) -> tuple[dict, object]:
    """The update an item holds, and how to pack a changed one back the way it
    was (sealed or not; inside its quarantine envelope). Raises when the item
    cannot be read."""
    if name == settings.queue_quarantine_name:
        envelope = json.loads(open_text(_QUARANTINE_CONTEXT, item))
        inner = str(envelope.get("payload", ""))
        update, pack_inner = _unpack(settings.queue_active_name, inner)

        def pack(changed: dict) -> str:
            return seal_text(_QUARANTINE_CONTEXT, json.dumps(
                {**envelope, "payload": pack_inner(changed)}, ensure_ascii=False))  # type: ignore[operator]
        return update, pack
    plain = item.startswith(("{", "["))
    update = json.loads(item if plain else open_text(_CONTEXT, item))
    if not isinstance(update, dict):
        raise ValueError("not an object")
    if plain:
        return update, lambda changed: json.dumps(changed, ensure_ascii=False)
    return update, encode_update


def _message_of(update: dict) -> dict:
    return update.get("message") or update.get("edited_message") or {}


async def media_payload_counts() -> dict[str, int]:
    """Payloads carrying `_media`, per list. One that cannot be read counts:
    it cannot be shown to hold none."""
    client, counts = _redis(), {}
    for key, name in _media_lists().items():
        count = 0
        for item in await client.lrange(name, 0, -1):
            try:
                update, _ = _unpack(name, str(item))
            except (SealedDataError, ValueError, TypeError):
                count += 1
                continue
            count += "_media" in _message_of(update)
        counts[key] = count
    return counts


async def normalize_media_payloads(seen) -> dict[str, int]:
    """The dead-letter and the quarantine, which do not drain on their own,
    back to the contract without media — in the order of the turn's guard:
    older than 90 days, out; already handled (`seen(update_id)`), out; else
    without `_media` and without the caption the WhatsApp conversion added.
    An item that cannot be read stays (and keeps counting)."""
    totals = {"dropped": 0, "stripped": 0, "unreadable": 0}
    now = datetime.now(UTC).timestamp()
    for name in (settings.queue_dead_letter_name, settings.queue_quarantine_name):
        done: set[str] = set()
        for item in await _redis().lrange(name, 0, -1):
            try:
                update, _ = _unpack(name, str(item))
            except (SealedDataError, ValueError, TypeError):
                continue
            if "_media" in _message_of(update) and await seen(str(update.get("update_id"))):
                done.add(str(update.get("update_id")))

        counted = {"dropped": 0, "stripped": 0, "unreadable": 0}

        def transform(item: str, name: str = name, done: set[str] = done,
                      counted: dict[str, int] = counted) -> str | None:
            try:
                update, pack = _unpack(name, item)
            except (SealedDataError, ValueError, TypeError):
                counted["unreadable"] += 1
                return item
            message = _message_of(update)
            if "_media" not in message:
                return item
            written = message.get("edit_date") or message.get("date")
            if isinstance(written, int | float) and now - written > _MEDIA_EVENT_SECONDS:
                counted["dropped"] += 1
                return None
            if str(update.get("update_id")) in done:
                counted["dropped"] += 1
                return None
            stripped = {k: v for k, v in message.items() if k not in ("_media", "caption")}
            field = "message" if "message" in update else "edited_message"
            counted["stripped"] += 1
            return pack({**update, field: stripped})  # type: ignore[operator]

        def restart(counted: dict[str, int] = counted) -> None:
            counted.update(dropped=0, stripped=0, unreadable=0)

        await _rewrite(name, transform, on_attempt=restart)
        for key in totals:
            totals[key] += counted[key]
    return totals
