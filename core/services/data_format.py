"""The newest format of stored data this image writes, recorded where the data is.

Postgres (table data_format) and Redis (key data_format) each keep the highest
format ever written to them. deploy/deploy.sh refuses to start an image whose
label (org.converge.data-format) is below either one: it would run on data it
cannot read. Format 1: encrypted fields and queue envelopes. Format 2: kept
media for the Inbox — records, file names, files and `_media` in the queues,
which only an image with the media cleanup may run on.

This image reads both. It records 2 only with capture on (MEDIA_ENABLED):
only capture creates media data, so with it off a release can still go back
to a format-1 image.

Both markers are raised before the first worker or webhook can write, and the
app never lowers them. Restoring Postgres from a backup lowers its own; Redis's
is dropped only by cli.queue_unseal (docs/CRIPTOGRAFIA.md).
"""
from __future__ import annotations

from core.config import settings
from core.integrations import update_queue
from core.repositories.connection import get_pool

IMAGE_FORMAT = 2
REDIS_KEY = "data_format"  # read by deploy/deploy.sh as is: no prefix

# Raise, never lower: GET, compare, SET only when higher, all in one step.
_RAISE = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current < tonumber(ARGV[1]) then redis.call('SET', KEYS[1], ARGV[1]) end
return redis.call('GET', KEYS[1])
"""


def startup_format() -> int:
    """The format this run may write: 2 with capture on, else 1."""
    return 2 if settings.media_enabled or settings.panel_uploads_enabled else 1


async def raise_on(conn, client, version: int) -> None:
    await conn.execute(
        "INSERT INTO data_format (version) SELECT %s WHERE NOT EXISTS (SELECT 1 FROM data_format)",
        (version,),
    )
    await conn.execute("UPDATE data_format SET version = GREATEST(version, %s)", (version,))
    await client.eval(_RAISE, 1, REDIS_KEY, version)


async def raise_markers() -> None:
    """At startup, before anything can write: both markers to startup_format()
    (never lower: turning capture off keeps a 2)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await raise_on(conn, update_queue._redis(), startup_format())
