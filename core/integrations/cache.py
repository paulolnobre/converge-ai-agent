"""Redis cache and shared rate limiting, optional by design.

Redis is a convenience here, never a dependency: if it is unreachable the cache
degrades to a no-op and the bot keeps answering. The one thing that changes is
that a limit becomes per-replica instead of shared, which is the safe direction
to fail.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import unicodedata

import redis.asyncio as redis
from redis.exceptions import RedisError

from core.config import settings
from core.metrics import CACHE_REQUESTS

logger = logging.getLogger(__name__)

_RETRY_AFTER = 60  # seconds offline after a failure, before trying Redis again

_client: redis.Redis | None = None
_offline_until: float = 0.0


def available() -> bool:
    """Whether Redis is expected to answer right now.

    Callers use this to decide whether a shared limit is in force or whether
    they must fall back to their own in-process one.
    """
    return settings.cache_enabled and time.monotonic() >= _offline_until


def _available() -> bool:
    return available()


def _mark_offline(operation: str, exc: Exception) -> None:
    global _offline_until
    _offline_until = time.monotonic() + _RETRY_AFTER
    logger.warning("Cache %s failed, degrading to no-op for %ss: %s", operation, _RETRY_AFTER, exc)


def _redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            health_check_interval=30,
        )
    return _client


def normalize(text: str) -> str:
    """Fold casing, accents and spacing so two phrasings share one entry."""
    folded = unicodedata.normalize("NFKD", text.strip().casefold())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(stripped.split())


def key(namespace: str, text: str, identity: str = "") -> str:
    digest = hashlib.sha256(f"{identity}|{normalize(text)}".encode()).hexdigest()
    return f"{settings.cache_prefix}:{namespace}:{digest}"


async def get_json(namespace: str, text: str, identity: str = ""):
    if not _available():
        return None
    try:
        raw = await _redis().get(key(namespace, text, identity))
    except (RedisError, OSError) as exc:
        _mark_offline("get", exc)
        return None
    CACHE_REQUESTS.labels(namespace, "hit" if raw else "miss").inc()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_json(namespace: str, text: str, value, ttl: int, identity: str = "") -> None:
    if not _available():
        return
    try:
        await _redis().setex(
            key(namespace, text, identity),
            ttl,
            json.dumps(value, ensure_ascii=False, default=str),
        )
    except (RedisError, OSError) as exc:
        _mark_offline("set", exc)


async def invalidate(namespace: str) -> int:
    """Drop a whole namespace — used when the underlying source changed."""
    if not _available():
        return 0
    removed = 0
    try:
        client = _redis()
        async for found in client.scan_iter(f"{settings.cache_prefix}:{namespace}:*", count=500):
            removed += await client.delete(found)
    except (RedisError, OSError) as exc:
        _mark_offline("invalidate", exc)
    return removed


async def rate_limit_hit(identifier, max_events: int, window_seconds: int) -> tuple[bool, bool]:
    """Sliding window shared by every replica.

    Returns (allowed, should_notify); should_notify is true only on the first
    blocked hit, so a flood gets one warning instead of an echo per message.
    When Redis is unavailable the caller falls back to its in-process limiter.
    """
    if not _available():
        return True, False
    bucket = f"{settings.cache_prefix}:ratelimit:{identifier}"
    try:
        client = _redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_seconds, nx=True)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        _mark_offline("rate_limit", exc)
        return True, False
    hits = int(count)
    if hits <= max_events:
        return True, False
    return False, hits == max_events + 1


async def ping() -> bool:
    return bool(await _redis().ping())


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError):
            pass
        _client = None
