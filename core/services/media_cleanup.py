"""The sweep that keeps the privacy policy's promises about kept files (spec §7).

Audio leaves after 48 h, or while the Inbox has a request open for that
conversation, at most 168 h; photos, videos and files after 90 days. A file's
name leaves with its file. Orphans, temporaries and old event marks go too.

It runs always — capture on or off — so turning capture off never leaves a
file without a deadline. Each file with a record is handled under
`pg_advisory_xact_lock(7411302, id)`, re-reading its status under it, in a
transaction of its own: the rekey takes the same lock, so "the rekey reads,
the sweep deletes, the rekey renames" cannot happen. An error on one item is
logged by id or storage key — never a name — and the others go on.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from uuid import UUID

from core import crypto
from core.config import MEDIA_EVENT_DAYS, settings
from core.integrations import update_queue
from core.metrics import MEDIA_CLEANUP_LAST_SUCCESS, MEDIA_CLEANUP_RUNS
from core.repositories.connection import get_pool
from core.repositories.media import AVAILABLE_SQL
from core.services import media_store

logger = logging.getLogger(__name__)

LOCK = 7411302
_BATCH = 200
_STALE_SECONDS = 3600   # orphans and temporaries younger than this may still be in a turn
_SILENT_SECONDS = 24 * 3600


async def _batches(conn, sql: str, params: tuple = ()) -> AsyncIterator[list[tuple]]:
    """Rows of `sql` (its first column the id, `%s` the last id seen, ordered
    by id, LIMIT left to us), 200 at a time, each read in its own short
    transaction so no lock outlives its item."""
    last = 0
    while True:
        async with conn.transaction():
            cur = await conn.execute(f"{sql} LIMIT {_BATCH}", (*params, last))
            rows = await cur.fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1][0]


async def _lock(conn, media_id: int) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK, media_id))


async def _expire(conn, media_id: int) -> bool:
    """Item 1: a stored file no longer available goes, with its name."""
    async with conn.transaction():
        await _lock(conn, media_id)
        cur = await conn.execute(
            f"SELECT m.storage_key FROM media m WHERE m.id = %s AND m.status = 'stored' "
            f"AND NOT {AVAILABLE_SQL}", (media_id,))
        row = await cur.fetchone()
        if row is None:
            return False
        if row[0] is not None:
            media_store.delete(row[0])
        await conn.execute(
            "UPDATE media SET status = 'expired', deleted_at = NOW(), filename_sealed = NULL "
            "WHERE id = %s", (media_id,))
        return True


async def _vanished(conn, media_id: int) -> bool:
    """Item 2: a stored record whose file is not on disk."""
    async with conn.transaction():
        await _lock(conn, media_id)
        cur = await conn.execute(
            "SELECT storage_key FROM media WHERE id = %s AND status = 'stored'", (media_id,))
        row = await cur.fetchone()
        if row is None or (row[0] is not None and media_store.path_for(row[0]).exists()):
            return False
        await conn.execute(
            "UPDATE media SET status = 'missing', filename_sealed = NULL WHERE id = %s",
            (media_id,))
        return True


async def _drop_unrecorded(conn, key: UUID, age: float) -> bool:
    """Item 4: a file with no record (older than an hour), or whose record is
    not stored (under that record's lock)."""
    async with conn.transaction():
        cur = await conn.execute("SELECT id FROM media WHERE storage_key = %s", (key,))
        row = await cur.fetchone()
        if row is None:
            return age > _STALE_SECONDS and media_store.delete(key)
        await _lock(conn, row[0])
        cur = await conn.execute("SELECT status FROM media WHERE id = %s", (row[0],))
        status = await cur.fetchone()
        if status is not None and status[0] == "stored":
            return False
        return media_store.delete(key)


async def sweep(conn) -> dict[str, int]:
    """One pass of items 1 to 5. Returns what it did, and `errors`."""
    done: dict[str, int] = dict.fromkeys(
        ("expired", "missing", "names", "orphans", "events", "temporaries", "errors"), 0)

    async for rows in _batches(
            conn, f"SELECT m.id FROM media m WHERE m.status = 'stored' AND NOT {AVAILABLE_SQL} "
                  f"AND m.id > %s ORDER BY m.id"):
        for (media_id,) in rows:
            try:
                done["expired"] += await _expire(conn, media_id)
            except Exception:
                done["errors"] += 1
                logger.exception("Media cleanup: could not expire media %s", media_id)

    async for rows in _batches(
            conn, "SELECT id, storage_key FROM media WHERE status = 'stored' AND id > %s "
                  "ORDER BY id"):
        for media_id, key in rows:
            if key is not None and media_store.path_for(key).exists():
                continue
            try:
                done["missing"] += await _vanished(conn, media_id)
            except Exception:
                done["errors"] += 1
                logger.exception("Media cleanup: could not mark media %s missing", media_id)

    try:
        while True:
            async with conn.transaction():
                cur = await conn.execute(
                    "UPDATE media SET filename_sealed = NULL WHERE id IN ("
                    "SELECT id FROM media WHERE filename_sealed IS NOT NULL "
                    "AND status <> 'stored' AND (status IN ('expired', 'missing') "
                    f"OR expires_at <= NOW()) LIMIT {_BATCH})")
            if not cur.rowcount:
                break
            done["names"] += cur.rowcount
    except Exception:
        done["errors"] += 1
        logger.exception("Media cleanup: could not clear the names")

    stale: list[media_store.Entry] = []
    try:
        for entry in media_store.entries():
            if entry.temporary:
                if entry.age_seconds > _STALE_SECONDS:
                    stale.append(entry)
                continue
            assert entry.key is not None
            try:
                done["orphans"] += await _drop_unrecorded(conn, entry.key, entry.age_seconds)
            except Exception:
                done["errors"] += 1
                logger.exception("Media cleanup: could not check file %s", entry.key)
    except OSError:
        done["errors"] += 1
        logger.exception("Media cleanup: could not list the media folder")

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "DELETE FROM media_events WHERE created_at < NOW() - make_interval(days => %s)",
                (MEDIA_EVENT_DAYS,))
        done["events"] = cur.rowcount
    except Exception:
        done["errors"] += 1
        logger.exception("Media cleanup: could not remove the old event marks")

    for entry in stale:
        try:
            entry.path.unlink(missing_ok=True)
            done["temporaries"] += 1
        except OSError:
            done["errors"] += 1
            logger.exception("Media cleanup: could not remove a temporary")
    return done


async def run_media_cleanup_worker(stop_event: asyncio.Event) -> None:
    """Every MEDIA_CLEANUP_MINUTES, capture on or off. A pass is good when
    every item went through; 24 h without one is logged CRITICAL every pass."""
    pool = await get_pool()
    last_good = time.time()
    while not stop_event.is_set():
        try:
            async with pool.connection() as conn:
                done = await sweep(conn)
            result = "partial" if done["errors"] else "ok"
            if any(count for name, count in done.items() if name != "errors"):
                logger.info("Media cleanup: %s", done)
        except Exception:
            result = "error"
            logger.exception("Media cleanup pass failed")
        MEDIA_CLEANUP_RUNS.labels(result).inc()
        if result == "ok":
            last_good = time.time()
            MEDIA_CLEANUP_LAST_SUCCESS.set(last_good)
        elif time.time() - last_good > _SILENT_SECONDS:
            logger.critical("Media cleanup: no good pass for more than 24 h; kept files may "
                            "be outliving the privacy policy")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.media_cleanup_minutes * 60)
        except TimeoutError:
            pass


async def rekey_files(conn) -> int:
    """Re-seal every stored file not in the current key (same storage key, same
    AAD), under its lock and only while still stored. Returns how many moved."""
    current, moved = crypto.current_key_version(), 0
    async for rows in _batches(
            conn, "SELECT id, storage_key FROM media WHERE status = 'stored' "
                  "AND storage_key IS NOT NULL AND id > %s ORDER BY id"):
        for media_id, key in rows:
            try:
                if media_store.key_version(key) == current:
                    continue
                async with conn.transaction():
                    await _lock(conn, media_id)
                    cur = await conn.execute(
                        "SELECT 1 FROM media WHERE id = %s AND status = 'stored'", (media_id,))
                    if await cur.fetchone() is None:
                        continue
                    media_store.write(key, media_store.read(key))  # renamed under the lock
                moved += 1
            except FileNotFoundError:
                continue  # the sweep marks it missing
            except crypto.SealedDataError:
                logger.error("Media rekey: media %s does not open; left as it is", media_id)
    return moved


async def purge(conn) -> dict[str, int]:
    """Every file and temporary gone, every record expired, every name gone —
    for a restore or a rollback, with the service stopped. The records first:
    a file left behind by a failure here is an orphan for the next sweep."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE media SET status = 'expired', filename_sealed = NULL, "
            "deleted_at = CASE WHEN storage_key IS NOT NULL THEN COALESCE(deleted_at, NOW()) END "
            "WHERE status <> 'expired' OR filename_sealed IS NOT NULL")
        records = cur.rowcount
    files = temporaries = 0
    for entry in list(media_store.entries()):
        entry.path.unlink(missing_ok=True)
        if entry.temporary:
            temporaries += 1
        else:
            files += 1
    return {"records": records, "files": files, "temporaries": temporaries}


async def inventory(conn) -> dict[str, object]:
    """What `cli.media status` shows: records (count and bytes) by status, files
    and bytes by key version (orphans and temporaries included), orphans,
    temporaries, envelopes whose header does not read, and media payloads
    still in the queues."""
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(stored_bytes), 0) FROM media "
            "GROUP BY status ORDER BY status")
        records = {status: {"count": count, "bytes": int(size)}
                   for status, count, size in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT storage_key FROM media WHERE status = 'stored' AND storage_key IS NOT NULL")
        stored = {row[0] for row in await cur.fetchall()}
    by_version: dict[int, int] = {}
    bytes_by_version: dict[int, int] = {}
    orphans = temporaries = unreadable = 0
    for entry in media_store.entries():
        if entry.temporary:
            temporaries += 1
        elif entry.key not in stored:
            orphans += 1
        try:
            version = media_store.version_at(entry.path)
            size = entry.path.stat().st_size
        except (OSError, crypto.SealedDataError):
            unreadable += 1
            continue
        by_version[version] = by_version.get(version, 0) + 1
        bytes_by_version[version] = bytes_by_version.get(version, 0) + size
    queues = await update_queue.media_payload_counts()
    return {"records": records, "files_by_key_version": by_version,
            "bytes_by_key_version": bytes_by_version, "orphans": orphans,
            "temporaries": temporaries, "unreadable": unreadable, "queues": queues,
            "queued_media_updates": sum(queues.values())}
